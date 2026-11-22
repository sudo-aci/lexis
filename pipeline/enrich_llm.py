#!/usr/bin/env python3
"""Optional batch LLM enricher that upgrades raw seed entries (or plain phrase lists) into full Lexis cards.

Works with any OpenAI-compatible Chat Completions API (OpenAI, Gemini OpenAI-compat, OpenRouter, Ollama):
  export OPENAI_API_KEY="sk-..."
  export OPENAI_BASE_URL="https://api.openai.com/v1"   # or Gemini/Ollama base URL
  python3 pipeline/enrich_llm.py --input seed.jsonl --output enriched.jsonl --model gpt-4o-mini

Supports:
  - Resumable checkpointing (skips phrases already written to --output)
  - --dry-run mode to inspect batches & prompts without making API calls
  - Automatic schema normalization & validation on every generated card
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.schema import normalize_item, validate_item

SYSTEM_PROMPT = (
    "You are an expert English lexicographer helping fluent Polish speakers achieve natural, "
    "idiomatic everyday and modern workplace fluency. Always respond with ONLY a valid JSON array "
    "(no markdown fences, no commentary)."
)


def load_input_seeds(input_path: Path) -> list[dict[str, Any]]:
    text = input_path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if input_path.suffix == ".json":
        parsed = json.loads(text)
        items = parsed if isinstance(parsed, list) else parsed.get("items", [])
        return [x if isinstance(x, dict) else {"phrase": str(x)} for x in items]
    if input_path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    # Plain text file: one phrase per line
    return [{"phrase": line.strip()} for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def build_batch_prompt(batch: list[dict[str, Any]]) -> str:
    seed_summary = []
    for item in batch:
        seed_summary.append({
            "id": item.get("id"),
            "phrase": item.get("phrase"),
            "ipa": item.get("ipa"),
            "category": item.get("category"),
            "register": item.get("register"),
            "cefr": item.get("cefr"),
            "polish_hint": item.get("polish"),
            "definition_hint": item.get("flat") or item.get("nuance"),
        })

    return (
        "Enrich each seed entry below into a complete, natural Lexis flashcard JSON object. "
        "Keep the exact `id` and `phrase` from each seed.\n\n"
        "Required JSON fields per object:\n"
        "- id, phrase, ipa, speechText, category ('Everyday'|'Workplace'|'Syntax'), "
        "register ('Casual'|'Conversational'|'Workplace'|'Diplomatic'|'Nuanced'), cefr ('B2'|'B2-C1'|'C1'|'C2')\n"
        "- flat (functional/textbook baseline), upgraded (idiomatic native sentence)\n"
        "- dialogues (array of 2 realistic 2-line exchanges with context + lines[{speaker, text}])\n"
        "- nuance (usage guidance + Polish speaker pitfalls/calques)\n"
        "- polish (natural idiomatic Polish equivalent)\n"
        "- clozeSentence (with '___'), clozeAnswer, clozeAccepted (array), clozeHint\n"
        "- quizPrompt, quizOptions (4 options), quizCorrectIndex (0-3), quizExplanations (4 explanations)\n"
        "- sandboxPrompt, sandboxSample, sandboxRubric (3 bullets)\n\n"
        f"Seeds:\n{json.dumps(seed_summary, ensure_ascii=False, indent=2)}"
    )


def strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else cleaned


def call_openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int = 90,
) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def enrich_seeds(
    seeds: list[dict[str, Any]],
    output_path: Path,
    *,
    batch_size: int = 5,
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1",
    api_key: str = "",
    dry_run: bool = False,
    max_retries: int = 3,
    caller: Callable[[str], str] | None = None,
) -> dict[str, int]:
    done_phrases: set[str] = set()
    if output_path.exists():
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
                if existing.get("phrase"):
                    done_phrases.add(existing["phrase"].strip().lower())
            except json.JSONDecodeError:
                continue

    pending = [s for s in seeds if str(s.get("phrase") or "").strip().lower() not in done_phrases]
    if dry_run:
        batches = (len(pending) + batch_size - 1) // batch_size if pending else 0
        sample_prompt = build_batch_prompt(pending[:batch_size]) if pending else "(nothing pending)"
        print(f"[DRY RUN] Total seeds: {len(seeds)}, already done: {len(done_phrases)}, pending: {len(pending)}, batches: {batches}")
        print("--- SAMPLE PROMPT (batch 1) ---")
        print(sample_prompt[:1500])
        return {"total": len(seeds), "skipped": len(done_phrases), "written": 0, "failed": 0}

    if caller is None and not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable (or pass --dry-run to preview).")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    failed = 0

    with output_path.open("a", encoding="utf-8") as fout:
        for i in range(0, len(pending), batch_size):
            batch = pending[i : i + batch_size]
            prompt = build_batch_prompt(batch)
            seed_by_phrase = {str(s.get("phrase") or "").strip().lower(): s for s in batch}

            response_text = None
            for attempt in range(1, max_retries + 1):
                try:
                    if caller is not None:
                        response_text = caller(prompt)
                    else:
                        response_text = call_openai_compatible(
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            prompt=prompt,
                        )
                    break
                except (urllib.error.URLError, TimeoutError, KeyError, RuntimeError) as err:
                    if attempt == max_retries:
                        print(f"Batch {i // batch_size + 1} failed after {max_retries} attempts: {err}", file=sys.stderr)
                        failed += len(batch)
                    else:
                        time.sleep(2 ** attempt)

            if not response_text:
                continue

            try:
                parsed = json.loads(strip_code_fences(response_text))
                if not isinstance(parsed, list):
                    raise ValueError("Model response was not a JSON array")
            except (json.JSONDecodeError, ValueError) as err:
                print(f"Batch {i // batch_size + 1} JSON parse failed: {err}", file=sys.stderr)
                failed += len(batch)
                continue

            for entry in parsed:
                if not isinstance(entry, dict):
                    failed += 1
                    continue
                seed_match = seed_by_phrase.get(str(entry.get("phrase") or "").strip().lower(), {})
                merged = {**seed_match, **entry}
                norm = normalize_item(merged, default_category=merged.get("category", "Everyday"))
                if not norm:
                    failed += 1
                    continue
                errs = validate_item(norm)
                if errs:
                    print(f"Validation failed for '{norm.get('phrase')}': {errs}", file=sys.stderr)
                    failed += 1
                    continue
                fout.write(json.dumps(norm, ensure_ascii=False) + "\n")
                fout.flush()
                written += 1

    return {"total": len(seeds), "skipped": len(done_phrases), "written": written, "failed": failed}


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-enrich Lexis seed JSONL/JSON/TXT into full cards via OpenAI-compatible API.")
    parser.add_argument("--input", "-i", type=Path, required=True, help="Input seed file (.jsonl, .json, or .txt).")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output enriched .jsonl file (resumable).")
    parser.add_argument("--model", default=os.environ.get("LEXIS_LLM_MODEL", "gpt-4o-mini"), help="Model name.")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), help="OpenAI-compatible API base URL.")
    parser.add_argument("--batch-size", type=int, default=5, help="Phrases per API call (default: 5).")
    parser.add_argument("--dry-run", action="store_true", help="Preview pending count and sample prompt without calling API.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    seeds = load_input_seeds(args.input)
    stats = enrich_seeds(
        seeds,
        args.output,
        batch_size=args.batch_size,
        model=args.model,
        base_url=args.base_url,
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        dry_run=args.dry_run,
    )
    print(f"Enrichment summary: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
