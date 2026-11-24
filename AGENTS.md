# Lexis Agent Guidelines

## Repository Overview

`lexis` is a zero-build static web app (`index.html`) backed by IndexedDB +
sharded JSON vocabulary packs (`data/manifest.json`, `data/packs/*.json`) and a
zero-dependency Python 3 data pipeline (`pipeline/`).

## Verification

Before pushing commits or approving pull requests, always run:

```bash
python3 .agents/scripts/verify_repo.py --all
```

This checks:
- Inline JavaScript syntax across all `<script>` blocks in `index.html` (`node --check`)
- JSON validity for `.agents/environment.json`, `data/manifest.json`, and `data/packs/*.json`
- Python unit test suites in `pipeline/tests/` and `.agents/tests/`

## Mandatory Pull Request Workflow

1. **Always use feature branches and pull requests.** Do not push directly to `main`.
2. **Automatic Separate-Thread Review on Every PR:**
   Every time you create a pull request (`gh pr create`) or push fixes to an
   open PR, you **MUST** follow the `pr-auto-review` skill
   (`.agents/skills/pr-auto-review/SKILL.md`):
   - Spawn an independent `pr-reviewer` subagent in a **separate thread**
     (`define_subagent` + `invoke_subagent`) to review the PR diff, run
     `python3 .agents/scripts/verify_repo.py --all`, and post a structured
     review comment on GitHub containing the
     `<!-- auto-pr-review:sha=<HEAD_SHA> verdict=<APPROVE|CHANGES_REQUESTED> round=<N> -->`
     marker.
   - Monitor the PR's review status and GitHub Actions CI checks using
     `python3 .agents/scripts/pr_monitor.py`.
   - If the reviewer reports blocking issues (`CHANGES_REQUESTED`), CI fails,
     merge conflicts arise, or human feedback arrives, automatically fix the
     issues, verify locally, push, and trigger a fresh separate-thread review
     (up to 3 rounds).
   - **Do not auto-merge.** Once the reviewer verdict is `APPROVE` and all CI
     checks pass, report the PR URL and summary to the user for final merge.
3. **Monitoring Open PRs:**
   Use `python3 .agents/scripts/pr_monitor.py check --json` (one-shot) or
   `python3 .agents/scripts/pr_monitor.py watch --interval 30 --json`
   (reactive background watcher) to detect and auto-fix issues on open PRs.
