#!/usr/bin/env python3
"""GitHub Pull Request monitor and state classifier for automated PR review.

Uses the `gh` CLI (GraphQL API) with Python 3 standard library only.
Evaluates open pull requests for:
  - Unreviewed HEAD commits (missing `auto-pr-review` marker for current HEAD)
  - Blocking automated review verdicts (`verdict=CHANGES_REQUESTED`)
  - New human/external review feedback (comments, reviews, unresolved threads)
  - Failing or pending CI status checks (`statusCheckRollup`)
  - Merge conflicts (`mergeable == CONFLICTING` or `mergeStateStatus == DIRTY`)

Exit codes:
  0 - Clean (or monitored PR closed/merged)
  1 - Runtime / CLI error
  2 - Action required (one or more PRs in NEEDS_REVIEW or NEEDS_FIX)
  3 - Waiting on CI (in `check` mode when no PR needs immediate action)
  4 - Timeout reached in `watch` mode without actionable events
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import subprocess
import sys
import time
from typing import Any


REVIEW_MARKER_RE = re.compile(r"<!--\s*auto-pr-review:([^>]+?)-->", re.IGNORECASE)
FIX_MARKER_RE = re.compile(r"<!--\s*auto-pr-fix:([^>]+?)-->", re.IGNORECASE)
KV_RE = re.compile(r"([a-zA-Z0-9_]+)=([^\s]+)")

PASSING_CHECK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILING_CHECK_CONCLUSIONS = {
    "FAILURE",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "STALE",
}

PR_FIELDS_FRAGMENT = """
  number
  title
  url
  isDraft
  state
  headRefName
  baseRefName
  headRefOid
  mergeable
  mergeStateStatus
  reviewDecision
  updatedAt
  commits(last: 1) {
    nodes {
      commit {
        oid
        committedDate
        authoredDate
        statusCheckRollup {
          state
          contexts(last: 100) {
            nodes {
              __typename
              ... on CheckRun {
                name
                status
                conclusion
                detailsUrl
                completedAt
                startedAt
              }
              ... on StatusContext {
                context
                state
                targetUrl
                createdAt
              }
            }
          }
        }
      }
    }
  }
  comments(last: 50) {
    nodes {
      id
      databaseId
      author { login }
      body
      createdAt
      updatedAt
    }
  }
  reviews(last: 50) {
    nodes {
      id
      databaseId
      author { login }
      state
      body
      submittedAt
      commit { oid }
    }
  }
  reviewThreads(first: 50) {
    nodes {
      id
      isResolved
      isOutdated
      path
      line
      comments(last: 10) {
        nodes {
          id
          databaseId
          author { login }
          body
          createdAt
        }
      }
    }
  }
"""

LIST_OPEN_PRS_QUERY = f"""
query($owner: String!, $name: String!, $limit: Int!) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(states: OPEN, first: $limit, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      nodes {{{PR_FIELDS_FRAGMENT}}}
    }}
  }}
}}
"""

SINGLE_PR_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{{PR_FIELDS_FRAGMENT}}}
  }}
}}
"""


def parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp into a timezone-aware datetime."""
    if not value:
        return None
    try:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def parse_kv_pairs(raw: str) -> dict[str, str]:
    """Parse space-separated key=value pairs from an HTML comment marker."""
    return {k.lower(): v for k, v in KV_RE.findall(raw)}


def extract_review_markers(body: str | None) -> list[dict[str, Any]]:
    """Extract all `auto-pr-review` markers from a comment or review body."""
    if not body:
        return []
    markers: list[dict[str, Any]] = []
    for match in REVIEW_MARKER_RE.finditer(body):
        kv = parse_kv_pairs(match.group(1))
        sha = kv.get("sha", "").strip()
        verdict = kv.get("verdict", "").strip().upper()
        if not sha or verdict not in {"APPROVE", "CHANGES_REQUESTED"}:
            continue
        round_num = 1
        if "round" in kv:
            try:
                round_num = max(1, int(kv["round"]))
            except ValueError:
                round_num = 1
        markers.append({"sha": sha, "verdict": verdict, "round": round_num})
    return markers


def extract_fix_markers(body: str | None) -> list[dict[str, str]]:
    """Extract all `auto-pr-fix` markers from a comment body."""
    if not body:
        return []
    markers: list[dict[str, str]] = []
    for match in FIX_MARKER_RE.finditer(body):
        kv = parse_kv_pairs(match.group(1))
        if kv.get("sha"):
            markers.append(
                {
                    "sha": kv["sha"].strip(),
                    "addressed_through": kv.get("addressed_through", "").strip(),
                }
            )
    return markers


def sha_matches(marker_sha: str, head_sha: str) -> bool:
    """Match full SHA or unambiguous >= 7-char prefix."""
    m = marker_sha.lower().strip()
    h = head_sha.lower().strip()
    if not m or not h:
        return False
    if m == h:
        return True
    return len(m) >= 7 and h.startswith(m)


def classify_checks(status_rollup: dict[str, Any] | None) -> dict[str, Any]:
    """Summarize CheckRun and StatusContext nodes into passed/failed/pending lists."""
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    if not status_rollup:
        return {"passed": passed, "failed": failed, "pending": pending, "total": 0}

    raw_nodes = (status_rollup.get("contexts") or {}).get("nodes") or []
    # Keep the latest status per unique check name/context.
    by_name: dict[str, dict[str, Any]] = {}
    for node in raw_nodes:
        if not isinstance(node, dict):
            continue
        typename = node.get("__typename")
        if typename == "CheckRun":
            name = node.get("name") or "unnamed-check"
            by_name[name] = {
                "type": "CheckRun",
                "name": name,
                "status": (node.get("status") or "").upper(),
                "conclusion": (node.get("conclusion") or "").upper() or None,
                "url": node.get("detailsUrl"),
            }
        elif typename == "StatusContext":
            name = node.get("context") or "unnamed-status"
            by_name[name] = {
                "type": "StatusContext",
                "name": name,
                "status": (node.get("state") or "").upper(),
                "conclusion": (node.get("state") or "").upper() or None,
                "url": node.get("targetUrl"),
            }

    for item in by_name.values():
        if item["type"] == "CheckRun":
            if item["status"] != "COMPLETED":
                pending.append(item)
            elif item["conclusion"] in PASSING_CHECK_CONCLUSIONS:
                passed.append(item)
            elif item["conclusion"] in FAILING_CHECK_CONCLUSIONS:
                failed.append(item)
            else:
                # Unknown conclusion on a completed check: treat conservatively as failed.
                failed.append(item)
        else:
            state = item["status"]
            if state == "SUCCESS":
                passed.append(item)
            elif state in {"PENDING", "EXPECTED"}:
                pending.append(item)
            else:
                failed.append(item)

    return {
        "passed": passed,
        "failed": failed,
        "pending": pending,
        "total": len(by_name),
    }


def evaluate_pr(pr_data: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a single GraphQL PR node into an actionable status report."""
    head_sha = (pr_data.get("headRefOid") or "").strip()
    commit_nodes = (pr_data.get("commits") or {}).get("nodes") or []
    last_commit = (commit_nodes[-1].get("commit") if commit_nodes else None) or {}
    head_committed_at_str = last_commit.get("committedDate") or last_commit.get("authoredDate")
    head_committed_at = parse_iso_timestamp(head_committed_at_str)

    comments = (pr_data.get("comments") or {}).get("nodes") or []
    reviews = (pr_data.get("reviews") or {}).get("nodes") or []
    threads = (pr_data.get("reviewThreads") or {}).get("nodes") or []

    # 1. Collect all auto-review markers across both reviews and issue comments.
    all_auto_reviews: list[dict[str, Any]] = []
    for rev in reviews:
        body = rev.get("body") or ""
        ts = rev.get("submittedAt")
        for m in extract_review_markers(body):
            all_auto_reviews.append(
                {
                    **m,
                    "timestamp": ts,
                    "dt": parse_iso_timestamp(ts),
                    "source": "review",
                    "author": ((rev.get("author") or {}).get("login")) or "unknown",
                }
            )
    for cmt in comments:
        body = cmt.get("body") or ""
        ts = cmt.get("createdAt")
        for m in extract_review_markers(body):
            all_auto_reviews.append(
                {
                    **m,
                    "timestamp": ts,
                    "dt": parse_iso_timestamp(ts),
                    "source": "comment",
                    "author": ((cmt.get("author") or {}).get("login")) or "unknown",
                }
            )

    all_auto_reviews.sort(
        key=lambda item: (
            item["dt"] or datetime.min.replace(tzinfo=timezone.utc),
            item["round"],
        )
    )

    head_reviews = [r for r in all_auto_reviews if sha_matches(r["sha"], head_sha)]
    latest_auto_review_for_head = head_reviews[-1] if head_reviews else None
    max_review_round = max(
        [r["round"] for r in all_auto_reviews] + [len(all_auto_reviews)],
        default=0,
    )

    # 2. Compute feedback cutoff from head commit timestamp and any auto-fix markers for HEAD.
    feedback_cutoff = head_committed_at
    for cmt in comments:
        cmt_dt = parse_iso_timestamp(cmt.get("createdAt"))
        for fm in extract_fix_markers(cmt.get("body")):
            if sha_matches(fm["sha"], head_sha):
                ack_dt = parse_iso_timestamp(fm.get("addressed_through")) or cmt_dt
                if ack_dt and (feedback_cutoff is None or ack_dt > feedback_cutoff):
                    feedback_cutoff = ack_dt

    # 3. Inspect CI checks.
    checks = classify_checks(last_commit.get("statusCheckRollup"))

    # 4. Inspect merge conflicts.
    mergeable = (pr_data.get("mergeable") or "UNKNOWN").upper()
    merge_state = (pr_data.get("mergeStateStatus") or "UNKNOWN").upper()
    has_merge_conflict = mergeable == "CONFLICTING" or merge_state == "DIRTY"

    # 5. Inspect unresolved, non-outdated inline review threads.
    unresolved_threads: list[dict[str, Any]] = []
    for thread in threads:
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        t_comments = (thread.get("comments") or {}).get("nodes") or []
        if not t_comments:
            continue
        last_t_comment = t_comments[-1]
        # If the latest reply in this thread is an auto-fix marker, treat as addressed
        # until a human posts a follow-up reply in the thread.
        if extract_fix_markers(last_t_comment.get("body")):
            continue
        unresolved_threads.append(
            {
                "id": thread.get("id"),
                "path": thread.get("path"),
                "line": thread.get("line"),
                "last_author": ((last_t_comment.get("author") or {}).get("login")) or "unknown",
                "last_created_at": last_t_comment.get("createdAt"),
                "excerpt": (last_t_comment.get("body") or "").strip()[:280],
            }
        )

    # 6. Inspect new human/external comments & reviews posted after feedback_cutoff.
    new_feedback_comments: list[dict[str, Any]] = []
    human_changes_requested = False

    for cmt in comments:
        body = (cmt.get("body") or "").strip()
        if not body:
            continue
        if extract_review_markers(body) or extract_fix_markers(body):
            continue
        cmt_dt = parse_iso_timestamp(cmt.get("createdAt"))
        if feedback_cutoff is None or (cmt_dt is not None and cmt_dt > feedback_cutoff):
            new_feedback_comments.append(
                {
                    "source": "comment",
                    "author": ((cmt.get("author") or {}).get("login")) or "unknown",
                    "created_at": cmt.get("createdAt"),
                    "excerpt": body[:280],
                }
            )

    for rev in reviews:
        body = (rev.get("body") or "").strip()
        state = (rev.get("state") or "").upper()
        if extract_review_markers(body) or extract_fix_markers(body):
            continue
        rev_dt = parse_iso_timestamp(rev.get("submittedAt"))
        is_after_cutoff = feedback_cutoff is None or (
            rev_dt is not None and rev_dt > feedback_cutoff
        )
        if state == "CHANGES_REQUESTED" and is_after_cutoff:
            human_changes_requested = True
            new_feedback_comments.append(
                {
                    "source": "review",
                    "state": state,
                    "author": ((rev.get("author") or {}).get("login")) or "unknown",
                    "created_at": rev.get("submittedAt"),
                    "excerpt": body[:280],
                }
            )
        elif body and is_after_cutoff and state in {"COMMENTED", "APPROVED"}:
            if state == "COMMENTED":
                new_feedback_comments.append(
                    {
                        "source": "review",
                        "state": state,
                        "author": ((rev.get("author") or {}).get("login")) or "unknown",
                        "created_at": rev.get("submittedAt"),
                        "excerpt": body[:280],
                    }
                )

    # 7. Assemble reasons and overall status.
    fix_reasons: list[str] = []
    review_reasons: list[str] = []
    wait_reasons: list[str] = []

    if latest_auto_review_for_head is None:
        review_reasons.append("UNREVIEWED_HEAD")
    elif latest_auto_review_for_head["verdict"] == "CHANGES_REQUESTED":
        fix_reasons.append("AUTO_REVIEW_CHANGES_REQUESTED")

    if human_changes_requested:
        fix_reasons.append("HUMAN_CHANGES_REQUESTED")
    if unresolved_threads:
        fix_reasons.append("UNRESOLVED_REVIEW_THREADS")
    if new_feedback_comments:
        fix_reasons.append("NEW_FEEDBACK_COMMENTS")
    if checks["failed"]:
        fix_reasons.append("CI_FAILED")
    if has_merge_conflict:
        fix_reasons.append("MERGE_CONFLICT")
    if checks["pending"]:
        wait_reasons.append("CI_PENDING")

    if fix_reasons:
        status = "NEEDS_FIX"
    elif review_reasons:
        status = "NEEDS_REVIEW"
    elif wait_reasons:
        status = "WAITING_CI"
    else:
        status = "CLEAN"

    latest_summary = None
    if latest_auto_review_for_head:
        latest_summary = {
            "sha": latest_auto_review_for_head["sha"],
            "verdict": latest_auto_review_for_head["verdict"],
            "round": latest_auto_review_for_head["round"],
            "timestamp": latest_auto_review_for_head["timestamp"],
            "source": latest_auto_review_for_head["source"],
        }

    return {
        "number": pr_data.get("number"),
        "title": pr_data.get("title"),
        "url": pr_data.get("url"),
        "state": pr_data.get("state"),
        "is_draft": bool(pr_data.get("isDraft")),
        "head_branch": pr_data.get("headRefName"),
        "base_branch": pr_data.get("baseRefName"),
        "head_sha": head_sha,
        "head_committed_at": head_committed_at_str,
        "mergeable": mergeable,
        "merge_state": merge_state,
        "status": status,
        "reasons": fix_reasons + review_reasons + wait_reasons,
        "latest_auto_review": latest_summary,
        "auto_review_rounds": max_review_round,
        "checks": checks,
        "unresolved_threads": unresolved_threads,
        "new_feedback": new_feedback_comments,
    }


def run_gh_command(args: list[str]) -> str:
    """Run a `gh` command and return stdout, raising RuntimeError on failure."""
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"gh {' '.join(args[:3])} failed (exit {proc.returncode}): {err}")
    return proc.stdout


def resolve_repo(repo_arg: str | None) -> tuple[str, str]:
    """Resolve owner/name from --repo or current git checkout via `gh repo view`."""
    raw = repo_arg
    if not raw:
        raw = run_gh_command(
            ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
        ).strip()
    if "/" not in raw:
        raise RuntimeError(f"Invalid repository specifier '{raw}'; expected 'owner/name'.")
    owner, name = raw.split("/", 1)
    return owner.strip(), name.strip()


def fetch_prs(
    owner: str,
    name: str,
    pr_number: int | None = None,
    include_drafts: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Fetch open PR(s) or a specific PR via GitHub GraphQL API."""
    if pr_number is not None:
        stdout = run_gh_command(
            [
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={pr_number}",
                "-f",
                f"query={SINGLE_PR_QUERY}",
            ]
        )
        payload = json.loads(stdout)
        pr_node = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
        if pr_node is None:
            raise RuntimeError(f"PR #{pr_number} not found in {owner}/{name}.")
        if pr_node.get("state") != "OPEN":
            return []
        return [pr_node]

    stdout = run_gh_command(
        [
            "api",
            "graphql",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"limit={limit}",
            "-f",
            f"query={LIST_OPEN_PRS_QUERY}",
        ]
    )
    payload = json.loads(stdout)
    nodes = (
        ((payload.get("data") or {}).get("repository") or {})
        .get("pullRequests", {})
        .get("nodes")
        or []
    )
    if include_drafts:
        return nodes
    return [n for n in nodes if not n.get("isDraft")]


def summarize_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Build top-level summary across evaluated PR reports."""
    actionable = [r for r in reports if r["status"] in {"NEEDS_FIX", "NEEDS_REVIEW"}]
    waiting = [r for r in reports if r["status"] == "WAITING_CI"]
    clean = [r for r in reports if r["status"] == "CLEAN"]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "open_pr_count": len(reports),
        "actionable_count": len(actionable),
        "waiting_ci_count": len(waiting),
        "clean_count": len(clean),
        "prs": reports,
    }


def print_report(summary: dict[str, Any], as_json: bool) -> None:
    """Output report in JSON or human-readable format."""
    if as_json:
        print(json.dumps(summary, indent=2))
        return

    print(
        f"PR Monitor @ {summary['checked_at']} — "
        f"{summary['open_pr_count']} open, "
        f"{summary['actionable_count']} actionable, "
        f"{summary['waiting_ci_count']} waiting CI, "
        f"{summary['clean_count']} clean"
    )
    for pr in summary["prs"]:
        reasons = ", ".join(pr["reasons"]) if pr["reasons"] else "none"
        checks = pr["checks"]
        print(
            f"  #{pr['number']} [{pr['status']}] {pr['title']} "
            f"(branch={pr['head_branch']} sha={pr['head_sha'][:7]} "
            f"checks={len(checks['passed'])}✓/{len(checks['failed'])}✗/{len(checks['pending'])}… "
            f"reasons={reasons})"
        )


def cmd_check(args: argparse.Namespace) -> int:
    owner, name = resolve_repo(args.repo)
    raw_prs = fetch_prs(
        owner,
        name,
        pr_number=args.pr,
        include_drafts=args.include_drafts,
        limit=args.limit,
    )
    reports = [evaluate_pr(p) for p in raw_prs]
    summary = summarize_reports(reports)
    print_report(summary, as_json=args.json)

    if summary["actionable_count"] > 0:
        return 2
    if summary["waiting_ci_count"] > 0:
        return 3
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    owner, name = resolve_repo(args.repo)
    start = time.monotonic()
    grace = args.grace_seconds if args.grace_seconds is not None else (15 if args.wait_clean else 0)

    while True:
        raw_prs = fetch_prs(
            owner,
            name,
            pr_number=args.pr,
            include_drafts=args.include_drafts,
            limit=args.limit,
        )
        reports = [evaluate_pr(p) for p in raw_prs]
        summary = summarize_reports(reports)

        if summary["actionable_count"] > 0:
            print_report(summary, as_json=args.json)
            return 2

        if args.pr is not None and not reports:
            print_report(summary, as_json=args.json)
            return 0

        elapsed = time.monotonic() - start
        if args.wait_clean:
            all_have_checks_or_past_grace = elapsed >= grace or all(
                r["checks"]["total"] > 0 for r in reports
            )
            if summary["waiting_ci_count"] == 0 and all_have_checks_or_past_grace:
                print_report(summary, as_json=args.json)
                return 0

        if elapsed >= args.max_wait:
            print_report(summary, as_json=args.json)
            return 4

        sleep_for = min(args.interval, max(1.0, args.max_wait - elapsed))
        time.sleep(sleep_for)


def cmd_format_marker(args: argparse.Namespace) -> int:
    if args.kind == "review":
        if not args.verdict:
            print("ERROR: --verdict is required for kind=review", file=sys.stderr)
            return 1
        print(
            f"<!-- auto-pr-review:sha={args.sha} verdict={args.verdict.upper()} round={args.round} -->"
        )
    else:
        ts = args.addressed_through or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        print(f"<!-- auto-pr-fix:sha={args.sha} addressed_through={ts} -->")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and monitor GitHub PRs for automated review & auto-fix."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("check", "Run a one-shot inspection of open PR(s)."),
        ("watch", "Poll open PR(s) until review/fix action is needed or timeout."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", help="Repository in owner/name format (default: current repo).")
        p.add_argument("--pr", type=int, help="Inspect/watch a specific PR number.")
        p.add_argument(
            "--include-drafts",
            action="store_true",
            help="Include draft PRs when listing open PRs.",
        )
        p.add_argument("--limit", type=int, default=20, help="Max open PRs to inspect.")
        p.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        if name == "watch":
            p.add_argument(
                "--interval",
                type=float,
                default=30.0,
                help="Polling interval in seconds (default: 30).",
            )
            p.add_argument(
                "--max-wait",
                type=float,
                default=1800.0,
                help="Maximum wait duration in seconds before exiting with code 4 (default: 1800).",
            )
            p.add_argument(
                "--wait-clean",
                action="store_true",
                help="Exit 0 as soon as monitored PR(s) are CLEAN and CI checks finish.",
            )
            p.add_argument(
                "--grace-seconds",
                type=float,
                default=None,
                help="Seconds to wait for newly triggered CI checks to register when using --wait-clean.",
            )

    fm = sub.add_parser("format-marker", help="Print a canonical HTML review/fix marker.")
    fm.add_argument("--kind", choices=("review", "fix"), default="review")
    fm.add_argument("--sha", required=True, help="Full 40-char commit SHA.")
    fm.add_argument("--verdict", choices=("APPROVE", "CHANGES_REQUESTED"))
    fm.add_argument("--round", type=int, default=1)
    fm.add_argument("--addressed-through", help="ISO-8601 UTC timestamp for fix markers.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args)
        if args.command == "watch":
            return cmd_watch(args)
        if args.command == "format-marker":
            return cmd_format_marker(args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
