---
name: pr-auto-review
description: Automatically reviews every created pull request in a separate reviewer subagent thread, monitors open pull requests for review feedback, CI failures, and merge conflicts, and fixes issues automatically until approved and green.
---

# Automatic PR Review & Open-PR Monitor (`/pr-auto-review`)

Use this skill in **two situations**:

1. **Mandatory after every `gh pr create` (or after pushing fixes to an open PR):**
   Always run an independent code review in a **separate subagent thread**, wait
   for both the review and CI checks, and automatically fix any blocking findings
   or CI failures until the PR is clean and approved.
2. **When asked to monitor, audit, or auto-fix open pull requests:**
   Use `.agents/scripts/pr_monitor.py` (when present in the repo, or `gh` CLI
   directly otherwise) to watch open PRs for new review comments, unresolved
   inline threads, unreviewed HEAD commits, failing CI checks, or merge
   conflicts, and automatically fix and re-review them.

> **Never auto-merge.** Stop once the PR has an `APPROVE` verdict from the
> separate reviewer thread and all CI checks pass, then present the PR URL and
> summary to the user for final merge.

---

## Constants & Markers

- `MAX_REVIEW_ROUNDS = 3` per fix cycle (prevents infinite review/fix loops).
- **Review marker** (must appear at the bottom of every automated review posted
  to GitHub):
  ```html
  <!-- auto-pr-review:sha=<40-char-head-sha> verdict=<APPROVE|CHANGES_REQUESTED> round=<N> -->
  ```
- **Fix acknowledgment marker** (append when replying to a PR comment or inline
  review thread to confirm it was addressed):
  ```html
  <!-- auto-pr-fix:sha=<40-char-head-sha> addressed_through=<ISO-8601-UTC> -->
  ```
- Generate markers without typos using:
  ```bash
  python3 .agents/scripts/pr_monitor.py format-marker --kind review --sha "$HEAD_SHA" --verdict APPROVE --round 1
  ```

---

## Workflow 1: Separate-Thread PR Review & Auto-Fix Loop

Run this immediately after `gh pr create` (and after pushing fix commits to an
open PR):

### Step 1: Capture PR Context

Record:
- Repository (`owner/repo`)
- PR number (`<PR_NUM>`) and URL
- Head branch, base branch, and exact 40-char HEAD SHA (`git rev-parse HEAD`)
- Current review `round` (starts at `1`)

### Step 2: Define the `pr-reviewer` Subagent (once per conversation)

Call `define_subagent` with:
- `name`: `pr-reviewer`
- `description`: `Independent read-only PR code reviewer that inspects diffs, runs tests, posts structured GitHub PR reviews with machine-readable verdict markers, and never edits source files.`
- `enable_write_tools`: `true` (required solely so the reviewer can run `gh`, `git diff`, and verification commands)
- `system_prompt`:

```text
You are an independent, thorough Pull Request Code Reviewer running in a separate
thread from the PR author. You do NOT share the author's assumptions—verify
everything directly from the diff, source files, and test output.

STRICT RULES:
1. NEVER modify repository files, create commits, push branches, or merge PRs.
   Your only write action is posting a single structured review comment to the
   GitHub PR.
2. Always inspect:
   - PR metadata: `gh pr view <PR> --json title,body,baseRefName,headRefName,headRefOid,files,statusCheckRollup`
   - Changed files: `gh pr diff <PR> --name-only`
   - Full patch: `gh pr diff <PR>` (inspect in full; if huge, inspect file-by-file)
   - Surrounding context of modified files using `view_file`
   - Verification commands: run `python3 .agents/scripts/verify_repo.py --all`
     (if present) or the repository's test suite/linters.
3. Evaluate for:
   - Correctness, logic bugs, and edge cases
   - Regressions and backwards compatibility
   - Security issues (injection, XSS, unsafe shell execution, leaked secrets)
   - Data/schema integrity and error handling
   - Test coverage for new/changed behavior
   - Scope discipline (no unintended edits or leftover debug artifacts)
4. Classify findings strictly:
   - **Blocking (`MUST FIX`)**: bugs, failing checks/tests, security issues,
     data corruption, missing critical edge cases, broken specs/contracts.
   - **Non-blocking (`NIT` / `SUGGESTION`)**: style preferences or optional
     future enhancements that do NOT block merge.
5. Determine verdict:
   - `CHANGES_REQUESTED` if there is 1 or more Blocking finding.
   - `APPROVE` if there are 0 Blocking findings.
6. Post your review to GitHub using:
   `gh pr review <PR> --comment --body "..."`
   (Use `--comment`, because GitHub rejects `--approve`/`--request-changes` on
   the same user's own PR. Fall back to `gh pr comment <PR> --body "..."` only
   if `gh pr review` fails.)
   Include:
   - Header: `## Automated PR Review (Round <N>)`
   - Verdict badge: `**Verdict:** APPROVE` or `**Verdict:** CHANGES_REQUESTED`
   - `### Blocking Findings` (with `file:line`, impact, and concrete fix) or `None.`
   - `### Non-Blocking Suggestions` or `None.`
   - `### Verification Run` (commands executed and pass/fail results)
   - Final line MUST be the exact marker for the reviewed HEAD SHA:
     `<!-- auto-pr-review:sha=<40-char-head-sha> verdict=<APPROVE|CHANGES_REQUESTED> round=<N> -->`
7. Return a concise structured report to the parent agent with the verdict,
   blocking findings, non-blocking suggestions, and verification output.
```

### Step 3: Launch the Reviewer in a Separate Thread

Call `invoke_subagent` with:
- `TypeName`: `pr-reviewer`
- `Role`: `PR #<PR_NUM> Reviewer (Round <N>)`
- `Model`: `inherit`
- `Prompt`: Provide the repo path, `owner/repo`, PR number, base/head branch,
  exact 40-char `HEAD_SHA`, and `round` number.

After invoking the subagent, **do not poll in a loop**. Stop calling tools (or
simultaneously start a background CI check watch via `pr_monitor.py`) and let
Antigravity wake you when the reviewer thread finishes.

### Step 4: Check CI & Process the Review Result

Once the reviewer thread finishes:

1. Check CI and PR status:
   - If `.agents/scripts/pr_monitor.py` exists, run:
     ```bash
     python3 .agents/scripts/pr_monitor.py watch --pr <PR_NUM> --wait-clean --interval 15 --max-wait 600 --json
     ```
     (Run in background with small `WaitMsBeforeAsync` if CI is still pending,
     and let Antigravity wake you on exit.)
   - Otherwise inspect with `gh pr checks <PR_NUM>`.
2. Evaluate outcome:
   - **If verdict is `APPROVE` AND no blocking issues, no CI failures, and no
     merge conflicts (`status == CLEAN`):**
     - Done! Report the PR URL, review round count, review summary, and green
       CI status to the user. Leave final merge to the user.
   - **If verdict is `CHANGES_REQUESTED` OR CI failed OR merge conflict
     detected:**
     - If `round >= MAX_REVIEW_ROUNDS`: stop looping and report the remaining
       blocking issues and logs clearly to the user.
     - Otherwise:
       1. Fix every blocking review finding and/or CI failure cleanly.
       2. Add or update unit tests covering the fix.
       3. Run local verification (`python3 .agents/scripts/verify_repo.py --all`
          or project tests) and confirm 100% pass.
       4. Commit with a clear message (e.g. `fix: address PR review findings (round <N>)`)
          and `git push`.
       5. Compute the new `HEAD_SHA` (`git rev-parse HEAD`), increment `round`,
          and **repeat from Step 3 with a fresh `pr-reviewer` subagent thread**.

---

## Workflow 2: Monitor Open Pull Requests & Auto-Fix

Use this workflow when asked to monitor open PRs or check/fix any open PRs in
the repository.

### Step 1: Audit Open PRs

Run:
```bash
python3 .agents/scripts/pr_monitor.py check --json
```
Exit codes:
- `0`: All open PRs are `CLEAN`.
- `2`: Action needed (`NEEDS_REVIEW` or `NEEDS_FIX`).
- `3`: Only `WAITING_CI`.

### Step 2: Handle Each Actionable PR

For every PR with `status` in `{"NEEDS_REVIEW", "NEEDS_FIX"}`:

1. **`NEEDS_REVIEW` (`UNREVIEWED_HEAD`)**:
   - Run Workflow 1 (Steps 2–4) to review that PR in a separate `pr-reviewer`
     thread and auto-fix any blocking findings.
2. **`NEEDS_FIX`**:
   - Ensure the working tree is clean, then check out the PR branch:
     ```bash
     git fetch origin <head_branch>
     git checkout <head_branch>
     git pull --ff-only origin <head_branch>
     ```
   - Address every reason listed in `reasons`:
     - **`CI_FAILED`**: Inspect failing checks (`gh pr checks <PR_NUM>` ->
       `gh run view <run-id> --log-failed | tail -200`), reproduce locally, and
       fix.
     - **`AUTO_REVIEW_CHANGES_REQUESTED` / `HUMAN_CHANGES_REQUESTED` /
       `NEW_FEEDBACK_COMMENTS` / `UNRESOLVED_REVIEW_THREADS`**: Read full
       review comments (`gh pr view <PR_NUM> --json reviews,comments` and
       `gh api repos/<owner>/<repo>/pulls/<PR_NUM>/comments`), implement the
       requested fixes, and add/run tests.
     - **`MERGE_CONFLICT`**: Fetch `origin/<base_branch>`, merge
       `origin/<base_branch>` into `<head_branch>` (or rebase if preferred),
       resolve conflicts carefully, and verify full test suite passes.
   - Run full local verification (`python3 .agents/scripts/verify_repo.py --all`).
   - Commit and push changes to `<head_branch>`.
   - If addressing human comments or inline review threads, post a brief
     reply summarizing the fix with the fix marker:
     `<!-- auto-pr-fix:sha=<NEW_HEAD_SHA> addressed_through=<ISO-8601-UTC> -->`
     (For inline review threads, reply via
     `gh api repos/<owner>/<repo>/pulls/<PR_NUM>/comments/<comment_id>/replies -f body="..."`.)
   - Immediately trigger **Workflow 1** (separate `pr-reviewer` subagent thread)
     on the newly pushed `HEAD_SHA` and wait for CI to go green.

### Step 3: Continuous Background Monitoring (Within an Active Session)

When the user asks to keep monitoring open PRs continuously:

1. Start the monitor in the background via `run_command` (with
   `WaitMsBeforeAsync: 2000`, `IsDaemon: false`):
   ```bash
   python3 .agents/scripts/pr_monitor.py watch --interval 30 --max-wait 1800 --json
   ```
2. Stop calling tools so Antigravity sleeps until the background watcher exits.
3. When Antigravity wakes you:
   - Exit code `2`: An actionable PR event occurred (new comment, review,
     unreviewed commit, CI failure, or merge conflict). Execute Step 2 to fix
     and re-review, then restart the watcher if open PRs remain.
   - Exit code `0`: All monitored PRs were closed/merged or finished cleanly.
   - Exit code `4`: Max wait window elapsed with no new events; report status
     to the user and ask if they want to continue monitoring.
