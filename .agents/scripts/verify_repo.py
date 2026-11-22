#!/usr/bin/env python3
"""Repository verification checks for lexis.

Runs zero-dependency syntax and integrity checks used by both GitHub Actions CI
and local PR review / auto-fix agents:
1. Validates all inline JavaScript <script> blocks in index.html with `node --check`.
2. Validates JSON syntax of `.agents/environment.json`, `data/manifest.json`,
   and `data/packs/*.json`.
3. Optionally (`--all`) runs the Python unit test suites in `pipeline/tests`
   and `.agents/tests`.
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import tempfile


JS_MIME_TYPES = {
    "",
    "text/javascript",
    "application/javascript",
    "text/ecmascript",
    "application/ecmascript",
    "module",
}


class InlineScriptExtractor(HTMLParser):
    """Extracts inline JavaScript blocks from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[tuple[int, bool, str]] = []
        self._in_script = False
        self._is_module = False
        self._start_line = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs}
        if "src" in attr_map and attr_map["src"]:
            return
        script_type = attr_map.get("type", "").lower().split(";")[0].strip()
        if script_type not in JS_MIME_TYPES:
            return
        self._in_script = True
        self._is_module = script_type == "module"
        self._start_line = self.getpos()[0]
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            code = "".join(self._chunks)
            if code.strip():
                self.blocks.append((self._start_line, self._is_module, code))
            self._in_script = False
            self._is_module = False
            self._chunks = []


def check_index_html_js(repo_root: Path) -> list[str]:
    """Validate inline JavaScript syntax in index.html using `node --check`."""
    index_path = repo_root / "index.html"
    if not index_path.is_file():
        return [f"Missing required file: {index_path}"]

    parser = InlineScriptExtractor()
    parser.feed(index_path.read_text(encoding="utf-8"))
    parser.close()

    if not parser.blocks:
        return ["No inline JavaScript <script> blocks found in index.html"]

    errors: list[str] = []
    for idx, (start_line, is_module, code) in enumerate(parser.blocks, start=1):
        suffix = ".mjs" if is_module else ".js"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, encoding="utf-8", delete=True
        ) as tmp:
            tmp.write(code)
            tmp.flush()
            proc = subprocess.run(
                ["node", "--check", tmp.name],
                capture_output=True,
                text=True,
                check=False,
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            errors.append(
                f"index.html script block #{idx} (line {start_line}) failed node --check:\n{detail}"
            )

    if not errors:
        print(f"OK: {len(parser.blocks)} inline JS block(s) in index.html passed node --check")
    return errors


def check_json_files(repo_root: Path) -> list[str]:
    """Validate JSON syntax of repository config and data files."""
    targets: list[Path] = []
    for rel in (".agents/environment.json", "data/manifest.json"):
        path = repo_root / rel
        if path.exists():
            targets.append(path)
        else:
            return [f"Missing expected JSON file: {rel}"]

    packs_dir = repo_root / "data" / "packs"
    if packs_dir.is_dir():
        targets.extend(sorted(packs_dir.glob("*.json")))

    errors: list[str] = []
    for path in targets:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Invalid JSON in {path.relative_to(repo_root)}: {exc}")

    if not errors:
        print(f"OK: {len(targets)} JSON file(s) parsed cleanly")
    return errors


def run_unittest_suite(repo_root: Path, suite_dir: str) -> list[str]:
    """Run unittest discovery for a given directory."""
    target = repo_root / suite_dir
    if not target.is_dir():
        return []
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", suite_dir, "-v"],
        cwd=str(repo_root),
        check=False,
    )
    if proc.returncode != 0:
        return [f"Unit test suite failed: {suite_dir}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify lexis repository health.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Path to the repository root (default: auto-detected).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also run pipeline/tests and .agents/tests unittest suites.",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    errors: list[str] = []
    errors.extend(check_index_html_js(repo_root))
    errors.extend(check_json_files(repo_root))

    if args.all:
        errors.extend(run_unittest_suite(repo_root, "pipeline/tests"))
        errors.extend(run_unittest_suite(repo_root, ".agents/tests"))

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print("ALL REPO CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
