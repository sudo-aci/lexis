#!/usr/bin/env python3
"""Ingest Tatoeba English-Polish sentence pairs (.tsv / .tsv.gz) to enrich seed items or extract examples.

Download source (CC BY 2.0 FR / CC0):
  https://tatoeba.org/en/downloads  (Sentence pairs: English -> Polish TSV)
  Format supported:
    - 4-column Tatoeba export: eng_id <TAB> eng_text <TAB> pol_id <TAB> pol_text
    - 2-column TSV export:     eng_text <TAB> pol_text
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.schema import build_default_cloze, make_stable_id, normalize_item


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_tatoeba_pairs(tsv_path: Path) -> Iterator[tuple[str, str]]:
    """Yield (english_sentence, polish_sentence) pairs from a Tatoeba TSV file."""
    with open_text(tsv_path) as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                eng, pol = parts[1].strip(), parts[3].strip()
            elif len(parts) >= 2:
                eng, pol = parts[0].strip(), parts[1].strip()
            else:
                continue
            if eng and pol:
                yield eng, pol


def enrich_seed_with_tatoeba(
    seed_path: Path,
    tatoeba_path: Path,
    output_path: Path,
    *,
    max_examples_per_phrase: int = 2,
    min_sentence_len: int = 25,
    max_sentence_len: int = 160,
) -> dict[str, int]:
    """Attach natural Tatoeba EN-PL example sentences & cloze prompts to matching seed items."""
    items: list[dict[str, Any]] = []
    with open_text(seed_path) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))

    # Build token -> item indices map for fast candidate lookup across large TSV files
    anchor_index: dict[str, list[int]] = {}
    patterns: list[re.Pattern[str]] = []
    matches: list[list[tuple[str, str]]] = [[] for _ in items]

    for idx, item in enumerate(items):
        phrase = str(item.get("phrase") or "").strip()
        patterns.append(re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE))
        words = sorted(re.findall(r"[a-z]{3,}", phrase.lower()), key=len, reverse=True)
        anchor = words[0] if words else phrase.lower()
        anchor_index.setdefault(anchor, []).append(idx)

    pairs_scanned = 0
    matched_items = 0

    for eng, pol in iter_tatoeba_pairs(tatoeba_path):
        pairs_scanned += 1
        if not (min_sentence_len <= len(eng) <= max_sentence_len):
            continue
        tokens = set(re.findall(r"[a-z]{3,}", eng.lower()))
        candidate_indices: set[int] = set()
        for tok in tokens:
            if tok in anchor_index:
                candidate_indices.update(anchor_index[tok])

        for idx in candidate_indices:
            if len(matches[idx]) >= max_examples_per_phrase:
                continue
            if patterns[idx].search(eng):
                if all(existing_eng != eng for existing_eng, _ in matches[idx]):
                    matches[idx].append((eng, pol))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fout:
        for idx, item in enumerate(items):
            found = matches[idx]
            if found:
                matched_items += 1
                best_eng, best_pol = found[0]
                # Upgrade placeholder sentence with real Tatoeba example
                if item.get("upgraded") == item.get("phrase") or not item.get("upgraded"):
                    item["upgraded"] = best_eng
                if not item.get("polish") or item.get("polish") == "—":
                    item["polish"] = best_pol
                cloze_s, cloze_a = build_default_cloze(best_eng, item["phrase"])
                item["clozeSentence"] = cloze_s
                item["clozeAnswer"] = cloze_a
                item["clozeAccepted"] = [cloze_a]

                item["dialogues"] = [
                    {
                        "context": f"Tatoeba EN–PL Example {i + 1}",
                        "lines": [
                            {"speaker": "EN", "text": eng},
                            {"speaker": "PL", "text": pol},
                        ],
                    }
                    for i, (eng, pol) in enumerate(found)
                ]
                item = normalize_item(item, default_category=item.get("category", "Everyday")) or item
            fout.write(json.dumps(item, ensure_ascii=False) + "\n")

    return {"seed_items": len(items), "pairs_scanned": pairs_scanned, "enriched": matched_items}


def extract_tatoeba_standalone(
    tatoeba_path: Path,
    output_path: Path,
    *,
    min_words: int = 4,
    max_words: int = 12,
    limit: int = 500,
    id_prefix: str = "tat",
) -> dict[str, int]:
    """Extract clean conversational EN-PL sentences directly as Lexis seed items."""
    seen: set[str] = set()
    scanned = 0
    written = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as fout:
        for eng, pol in iter_tatoeba_pairs(tatoeba_path):
            scanned += 1
            wc = len(eng.split())
            if wc < min_words or wc > max_words:
                continue
            key = eng.lower()
            if key in seen:
                continue
            seen.add(key)

            item = normalize_item(
                {
                    "id": make_stable_id(id_prefix, eng),
                    "phrase": eng,
                    "category": "Everyday",
                    "register": "Conversational",
                    "cefr": "B1-B2",
                    "flat": eng,
                    "upgraded": eng,
                    "polish": pol,
                    "dialogues": [
                        {
                            "context": "Tatoeba Sentence Pair",
                            "lines": [
                                {"speaker": "EN", "text": eng},
                                {"speaker": "PL", "text": pol},
                            ],
                        }
                    ],
                    "nuance": "Authentic bilingual sentence pair from the Tatoeba corpus (CC BY 2.0 FR).",
                    "source": "Tatoeba (CC BY 2.0 FR)",
                },
                default_category="Everyday",
                id_prefix=id_prefix,
            )
            if item:
                fout.write(json.dumps(item, ensure_ascii=False) + "\n")
                written += 1
                if limit and written >= limit:
                    break

    return {"pairs_scanned": scanned, "written": written}


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich Lexis seed items or extract EN-PL pairs from Tatoeba TSV.")
    parser.add_argument("--tatoeba", "-t", type=Path, required=True, help="Path to Tatoeba EN-PL .tsv or .tsv.gz file.")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output .jsonl file.")
    parser.add_argument("--seed", "-s", type=Path, default=None, help="Optional seed .jsonl (e.g. from ingest_kaikki.py) to enrich.")
    parser.add_argument("--max-examples", type=int, default=2, help="Max Tatoeba examples per phrase in enrich mode.")
    parser.add_argument("--limit", type=int, default=500, help="Max items in standalone extract mode.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.seed:
        stats = enrich_seed_with_tatoeba(
            args.seed,
            args.tatoeba,
            args.output,
            max_examples_per_phrase=args.max_examples,
        )
        print(
            f"Tatoeba enrichment complete: scanned {stats['pairs_scanned']} pairs, "
            f"enriched {stats['enriched']}/{stats['seed_items']} seed items -> {args.output}"
        )
    else:
        stats = extract_tatoeba_standalone(args.tatoeba, args.output, limit=args.limit)
        print(f"Tatoeba extract complete: scanned {stats['pairs_scanned']} pairs -> wrote {stats['written']} items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
