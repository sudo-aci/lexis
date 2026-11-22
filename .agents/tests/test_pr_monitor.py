"""Unit tests for .agents/scripts/pr_monitor.py and verify_repo.py."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".agents" / "scripts"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr_monitor = _load_module("pr_monitor", "pr_monitor.py")
verify_repo = _load_module("verify_repo", "verify_repo.py")


def _make_pr(
    *,
    number: int = 10,
    head_sha: str = "1111111111111111111111111111111111111111",
    committed_at: str = "2026-11-22T18:00:00Z",
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    checks: list[dict] | None = None,
    comments: list[dict] | None = None,
    reviews: list[dict] | None = None,
    threads: list[dict] | None = None,
) -> dict:
    rollup = (
        {"state": "SUCCESS", "contexts": {"nodes": checks}}
        if checks is not None
        else None
    )
    return {
        "number": number,
        "title": "feat: example change",
        "url": f"https://github.com/sudo-aci/lexis/pull/{number}",
        "isDraft": False,
        "state": "OPEN",
        "headRefName": "feat/example",
        "baseRefName": "main",
        "headRefOid": head_sha,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "reviewDecision": None,
        "updatedAt": committed_at,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head_sha,
                        "committedDate": committed_at,
                        "authoredDate": committed_at,
                        "statusCheckRollup": rollup,
                    }
                }
            ]
        },
        "comments": {"nodes": comments or []},
        "reviews": {"nodes": reviews or []},
        "reviewThreads": {"nodes": threads or []},
    }


class TestMarkerParsing(unittest.TestCase):
    def test_extract_review_markers(self) -> None:
        body = (
            "Looks great!\n\n"
            "<!-- auto-pr-review:sha=abcdef1234567890 verdict=APPROVE round=2 -->"
        )
        markers = pr_monitor.extract_review_markers(body)
        self.assertEqual(
            markers,
            [{"sha": "abcdef1234567890", "verdict": "APPROVE", "round": 2}],
        )

    def test_extract_fix_markers(self) -> None:
        body = (
            "Fixed edge case.\n"
            "<!-- auto-pr-fix:sha=abcdef1234567890 addressed_through=2026-11-22T18:10:00Z -->"
        )
        markers = pr_monitor.extract_fix_markers(body)
        self.assertEqual(
            markers,
            [
                {
                    "sha": "abcdef1234567890",
                    "addressed_through": "2026-11-22T18:10:00Z",
                }
            ],
        )

    def test_sha_matches_full_and_prefix(self) -> None:
        full = "29a2bb5e0e231f2458adebab48fc7409fb13dc38"
        self.assertTrue(pr_monitor.sha_matches(full, full))
        self.assertTrue(pr_monitor.sha_matches("29a2bb5", full))
        self.assertFalse(pr_monitor.sha_matches("29a2bb", full))
        self.assertFalse(pr_monitor.sha_matches("deadbee", full))


class TestEvaluatePR(unittest.TestCase):
    SHA_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    SHA_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def test_new_pr_needs_review(self) -> None:
        pr = _make_pr(head_sha=self.SHA_A)
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIn("UNREVIEWED_HEAD", result["reasons"])

    def test_changes_requested_by_auto_reviewer(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            reviews=[
                {
                    "id": "r1",
                    "author": {"login": "sudo-aci"},
                    "state": "COMMENTED",
                    "body": f"Found bug.\n<!-- auto-pr-review:sha={self.SHA_A} verdict=CHANGES_REQUESTED round=1 -->",
                    "submittedAt": "2026-11-22T18:02:00Z",
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "NEEDS_FIX")
        self.assertIn("AUTO_REVIEW_CHANGES_REQUESTED", result["reasons"])
        self.assertEqual(result["auto_review_rounds"], 1)

    def test_approved_and_green_ci_is_clean(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            checks=[
                {
                    "__typename": "CheckRun",
                    "name": "verify",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://example.com/1",
                }
            ],
            reviews=[
                {
                    "id": "r1",
                    "author": {"login": "sudo-aci"},
                    "state": "COMMENTED",
                    "body": f"All good.\n<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "submittedAt": "2026-11-22T18:02:00Z",
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "CLEAN")
        self.assertEqual(result["reasons"], [])

    def test_approved_with_pending_ci_waits(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            checks=[
                {
                    "__typename": "CheckRun",
                    "name": "verify",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                    "detailsUrl": "https://example.com/1",
                }
            ],
            comments=[
                {
                    "id": "c1",
                    "author": {"login": "sudo-aci"},
                    "body": f"LGTM.\n<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "createdAt": "2026-11-22T18:02:00Z",
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "WAITING_CI")
        self.assertEqual(result["reasons"], ["CI_PENDING"])

    def test_failed_ci_triggers_fix(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            checks=[
                {
                    "__typename": "CheckRun",
                    "name": "verify",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://example.com/fail",
                }
            ],
            comments=[
                {
                    "id": "c1",
                    "author": {"login": "sudo-aci"},
                    "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "createdAt": "2026-11-22T18:02:00Z",
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "NEEDS_FIX")
        self.assertIn("CI_FAILED", result["reasons"])

    def test_merge_conflict_triggers_fix(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            mergeable="CONFLICTING",
            merge_state="DIRTY",
            comments=[
                {
                    "id": "c1",
                    "author": {"login": "sudo-aci"},
                    "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "createdAt": "2026-11-22T18:02:00Z",
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "NEEDS_FIX")
        self.assertIn("MERGE_CONFLICT", result["reasons"])

    def test_old_review_does_not_cover_new_commit(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_B,
            committed_at="2026-11-22T18:10:00Z",
            reviews=[
                {
                    "id": "r1",
                    "author": {"login": "sudo-aci"},
                    "state": "COMMENTED",
                    "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "submittedAt": "2026-11-22T18:02:00Z",
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["reasons"], ["UNREVIEWED_HEAD"])

    def test_new_human_comment_triggers_fix_until_acknowledged(self) -> None:
        base_comments = [
            {
                "id": "c1",
                "author": {"login": "sudo-aci"},
                "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                "createdAt": "2026-11-22T18:02:00Z",
            },
            {
                "id": "c2",
                "author": {"login": "reviewer-human"},
                "body": "Please rename this variable.",
                "createdAt": "2026-11-22T18:05:00Z",
            },
        ]
        pr_unack = _make_pr(head_sha=self.SHA_A, comments=base_comments)
        res_unack = pr_monitor.evaluate_pr(pr_unack)
        self.assertEqual(res_unack["status"], "NEEDS_FIX")
        self.assertIn("NEW_FEEDBACK_COMMENTS", res_unack["reasons"])

        pr_ack = _make_pr(
            head_sha=self.SHA_A,
            comments=base_comments
            + [
                {
                    "id": "c3",
                    "author": {"login": "sudo-aci"},
                    "body": f"Explained rationale.\n<!-- auto-pr-fix:sha={self.SHA_A} addressed_through=2026-11-22T18:06:00Z -->",
                    "createdAt": "2026-11-22T18:06:00Z",
                }
            ],
        )
        res_ack = pr_monitor.evaluate_pr(pr_ack)
        self.assertEqual(res_ack["status"], "CLEAN")

    def test_inline_thread_addressed_and_retriggered_on_followup(self) -> None:
        review_cmt = [
            {
                "id": "c1",
                "author": {"login": "sudo-aci"},
                "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                "createdAt": "2026-11-22T18:02:00Z",
            }
        ]
        thread_open = {
            "id": "t1",
            "isResolved": False,
            "isOutdated": False,
            "path": "index.html",
            "line": 42,
            "comments": {
                "nodes": [
                    {
                        "id": "tc1",
                        "author": {"login": "reviewer"},
                        "body": "Null guard needed here.",
                        "createdAt": "2026-11-22T18:03:00Z",
                    }
                ]
            },
        }
        res_open = pr_monitor.evaluate_pr(
            _make_pr(head_sha=self.SHA_A, comments=review_cmt, threads=[thread_open])
        )
        self.assertEqual(res_open["status"], "NEEDS_FIX")
        self.assertIn("UNRESOLVED_REVIEW_THREADS", res_open["reasons"])

        thread_replied = {
            **thread_open,
            "comments": {
                "nodes": thread_open["comments"]["nodes"]
                + [
                    {
                        "id": "tc2",
                        "author": {"login": "sudo-aci"},
                        "body": f"Added null guard.\n<!-- auto-pr-fix:sha={self.SHA_A} -->",
                        "createdAt": "2026-11-22T18:04:00Z",
                    }
                ]
            },
        }
        res_replied = pr_monitor.evaluate_pr(
            _make_pr(head_sha=self.SHA_A, comments=review_cmt, threads=[thread_replied])
        )
        self.assertEqual(res_replied["status"], "CLEAN")

        thread_followup = {
            **thread_replied,
            "comments": {
                "nodes": thread_replied["comments"]["nodes"]
                + [
                    {
                        "id": "tc3",
                        "author": {"login": "reviewer"},
                        "body": "Please also handle empty string.",
                        "createdAt": "2026-11-22T18:05:00Z",
                    }
                ]
            },
        }
        res_followup = pr_monitor.evaluate_pr(
            _make_pr(head_sha=self.SHA_A, comments=review_cmt, threads=[thread_followup])
        )
        self.assertEqual(res_followup["status"], "NEEDS_FIX")
        self.assertIn("UNRESOLVED_REVIEW_THREADS", res_followup["reasons"])

    def test_old_human_changes_requested_before_head_does_not_loop(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_B,
            committed_at="2026-11-22T18:10:00Z",
            reviews=[
                {
                    "id": "r1",
                    "author": {"login": "reviewer"},
                    "state": "CHANGES_REQUESTED",
                    "body": "Fix bug X.",
                    "submittedAt": "2026-11-22T18:05:00Z",
                },
                {
                    "id": "r2",
                    "author": {"login": "sudo-aci"},
                    "state": "COMMENTED",
                    "body": f"<!-- auto-pr-review:sha={self.SHA_B} verdict=APPROVE round=2 -->",
                    "submittedAt": "2026-11-22T18:12:00Z",
                },
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "CLEAN")

    def test_classify_checks_deduplicates_out_of_order_by_timestamp_and_type(self) -> None:
        rollup = {
            "contexts": {
                "nodes": [
                    {
                        "__typename": "CheckRun",
                        "name": "Verify Lexis",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "completedAt": "2026-11-22T18:10:00Z",
                        "startedAt": "2026-11-22T18:09:00Z",
                        "detailsUrl": "https://example.com/newer-pass",
                    },
                    {
                        "__typename": "CheckRun",
                        "name": "Verify Lexis",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "completedAt": "2026-11-22T18:05:00Z",
                        "startedAt": "2026-11-22T18:04:00Z",
                        "detailsUrl": "https://example.com/older-fail",
                    },
                    {
                        "__typename": "StatusContext",
                        "context": "Verify Lexis",
                        "state": "FAILURE",
                        "createdAt": "2026-11-22T18:08:00Z",
                        "targetUrl": "https://example.com/status-fail",
                    },
                    {
                        "__typename": "StatusContext",
                        "context": "Verify Lexis",
                        "state": "SUCCESS",
                        "createdAt": "2026-11-22T18:02:00Z",
                        "targetUrl": "https://example.com/status-older-pass",
                    },
                ]
            }
        }
        checks = pr_monitor.classify_checks(rollup)
        # CheckRun and StatusContext with identical names do not clobber each other.
        self.assertEqual(checks["total"], 2)
        self.assertEqual(len(checks["passed"]), 1)
        self.assertEqual(checks["passed"][0]["type"], "CheckRun")
        self.assertEqual(checks["passed"][0]["url"], "https://example.com/newer-pass")
        self.assertEqual(len(checks["failed"]), 1)
        self.assertEqual(checks["failed"][0]["type"], "StatusContext")
        self.assertEqual(checks["failed"][0]["url"], "https://example.com/status-fail")

    def test_human_approval_clears_earlier_changes_requested_same_reviewer(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            committed_at="2026-11-22T18:00:00Z",
            reviews=[
                {
                    "id": "r_auto",
                    "author": {"login": "sudo-aci"},
                    "state": "COMMENTED",
                    "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "submittedAt": "2026-11-22T18:01:00Z",
                },
                {
                    "id": "r_req",
                    "author": {"login": "alice"},
                    "state": "CHANGES_REQUESTED",
                    "body": "Wait, double check this.",
                    "submittedAt": "2026-11-22T18:02:00Z",
                },
                {
                    "id": "r_ok",
                    "author": {"login": "alice"},
                    "state": "APPROVED",
                    "body": "Never mind, looks good!",
                    "submittedAt": "2026-11-22T18:05:00Z",
                },
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["status"], "CLEAN")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["new_feedback"], [])

        # A second reviewer requesting changes still triggers NEEDS_FIX.
        pr_second_reviewer = {
            **pr,
            "reviews": {
                "nodes": pr["reviews"]["nodes"]
                + [
                    {
                        "id": "r_bob",
                        "author": {"login": "bob"},
                        "state": "CHANGES_REQUESTED",
                        "body": "Found a separate issue.",
                        "submittedAt": "2026-11-22T18:06:00Z",
                    }
                ]
            },
        }
        result_bob = pr_monitor.evaluate_pr(pr_second_reviewer)
        self.assertEqual(result_bob["status"], "NEEDS_FIX")
        self.assertIn("HUMAN_CHANGES_REQUESTED", result_bob["reasons"])

    def test_unresolved_thread_exposes_comment_database_ids(self) -> None:
        pr = _make_pr(
            head_sha=self.SHA_A,
            comments=[
                {
                    "id": "c1",
                    "author": {"login": "sudo-aci"},
                    "body": f"<!-- auto-pr-review:sha={self.SHA_A} verdict=APPROVE round=1 -->",
                    "createdAt": "2026-11-22T18:02:00Z",
                }
            ],
            threads=[
                {
                    "id": "t1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "AGENTS.md",
                    "line": 10,
                    "comments": {
                        "nodes": [
                            {
                                "id": "tc1",
                                "databaseId": 9001,
                                "author": {"login": "alice"},
                                "body": "Initial note",
                                "createdAt": "2026-11-22T18:03:00Z",
                            },
                            {
                                "id": "tc2",
                                "databaseId": 9002,
                                "author": {"login": "alice"},
                                "body": "Follow-up note",
                                "createdAt": "2026-11-22T18:04:00Z",
                            },
                        ]
                    },
                }
            ],
        )
        result = pr_monitor.evaluate_pr(pr)
        self.assertEqual(result["unresolved_threads"][0]["root_comment_database_id"], 9001)
        self.assertEqual(result["unresolved_threads"][0]["last_comment_database_id"], 9002)

    def test_fetch_prs_uses_string_flags_for_owner_and_name(self) -> None:
        fake_response = json.dumps(
            {"data": {"repository": {"pullRequests": {"nodes": []}}}}
        )
        with mock.patch.object(pr_monitor, "run_gh_command", return_value=fake_response) as m:
            pr_monitor.fetch_prs("12345", "true", limit=5)
            called_args = m.call_args[0][0]
            self.assertEqual(called_args[2:8], ["-f", "owner=12345", "-f", "name=true", "-F", "limit=5"])

        fake_single = json.dumps(
            {"data": {"repository": {"pullRequest": {"state": "CLOSED"}}}}
        )
        with mock.patch.object(pr_monitor, "run_gh_command", return_value=fake_single) as m:
            pr_monitor.fetch_prs("owner", "@not-a-file", pr_number=4)
            called_args = m.call_args[0][0]
            self.assertEqual(called_args[2:8], ["-f", "owner=owner", "-f", "name=@not-a-file", "-F", "number=4"])

    def test_cmd_watch_validates_interval_and_retries_transient_errors(self) -> None:
        bad_args = argparse.Namespace(
            repo="o/r",
            pr=None,
            include_drafts=False,
            limit=10,
            json=True,
            interval=0,
            max_wait=10,
            wait_clean=False,
            grace_seconds=0,
        )
        with self.assertRaises(RuntimeError):
            pr_monitor.cmd_watch(bad_args)

        retry_args = argparse.Namespace(
            repo="o/r",
            pr=4,
            include_drafts=False,
            limit=10,
            json=True,
            interval=0.01,
            max_wait=5,
            wait_clean=True,
            grace_seconds=0,
        )
        pr_unreviewed = _make_pr(number=4, head_sha=self.SHA_A)
        with mock.patch.object(
            pr_monitor,
            "fetch_prs",
            side_effect=[RuntimeError("network blip"), [pr_unreviewed]],
        ), mock.patch.object(pr_monitor, "print_report"):
            code = pr_monitor.cmd_watch(retry_args)
            self.assertEqual(code, 2)


class TestVerifyRepo(unittest.TestCase):
    def test_inline_script_extractor_filters_non_js_and_external(self) -> None:
        html = """
        <!doctype html>
        <html>
          <head>
            <script src="app.js"></script>
            <script type="application/json">{"not": "js"}</script>
            <script>const a = 1;</script>
            <script type="module">export const b = 2;</script>
          </head>
        </html>
        """
        parser = verify_repo.InlineScriptExtractor()
        parser.feed(html)
        parser.close()
        self.assertEqual(len(parser.blocks), 2)
        self.assertFalse(parser.blocks[0][1])
        self.assertTrue(parser.blocks[1][1])

    def test_check_index_html_js_detects_syntax_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text(
                "<html><script>const broken = (;</script></html>",
                encoding="utf-8",
            )
            errors = verify_repo.check_index_html_js(root)
            self.assertEqual(len(errors), 1)
            self.assertIn("failed node --check", errors[0])

    def test_check_json_files_accumulates_all_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            errors = verify_repo.check_json_files(Path(tmp))
            self.assertEqual(len(errors), 2)
            self.assertIn(".agents/environment.json", errors[0])
            self.assertIn("data/manifest.json", errors[1])


if __name__ == "__main__":
    unittest.main()
