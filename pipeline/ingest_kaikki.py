#!/usr/bin/env python3
"""Stream Kaikki.org Wiktextract JSONL (.jsonl / .jsonl.gz) into Lexis seed JSONL.

Download source (CC BY-SA):
  https://kaikki.org/dictionary/rawdata.html
  (English edition JSONL or raw-wiktextract-data.jsonl.gz)

Optional CEFR vocabulary CSV (e.g., CEFR-J / Words-CEFR-Dataset):
  https://github.com/openlanguageprofiles/olp-en-cefrj
  https://github.com/Maximax67/Words-CEFR-Dataset
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, TextIO

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.schema import make_stable_id, normalize_item

DEFAULT_POS = {"phrase", "idiom", "proverb", "verb", "prep_phrase", "adv", "adj", "intj"}

WORKPLACE_KEYWORDS = {
    "business", "corporate", "finance", "economics", "management", "computing",
    "software", "law", "legal", "commerce", "marketing", "accounting", "workplace",
}

SYNTAX_POS = {"conj", "prep", "particle"}

REGISTER_TAG_MAP = {
    "slang": "Slang",
    "informal": "Casual",
    "colloquial": "Conversational",
    "idiomatic": "Conversational",
    "figurative": "Conversational",
    "business": "Workplace",
    "formal": "Diplomatic",
    "euphemistic": "Diplomatic",
    "literary": "Nuanced",
    "academic": "Nuanced",
}


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def load_cefr_map(csv_path: Path | None) -> dict[str, str]:
    """Load a word -> CEFR mapping from CEFR-J or Words-CEFR-Dataset CSV files."""
    if not csv_path:
        return {}
    cefr_map: dict[str, str] = {}
    with open_text(csv_path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return {}
        fields = {name.lower().strip(): name for name in reader.fieldnames}
        word_col = next((fields[k] for k in ("headword", "word", "lemma", "phrase", "term") if k in fields), None)
        cefr_col = next((fields[k] for k in ("cefr", "level", "cefr_level", "grade") if k in fields), None)
        if not word_col or not cefr_col:
            raise ValueError(f"Could not detect word/CEFR columns in {csv_path} (found {reader.fieldnames})")
        for row in reader:
            word = (row.get(word_col) or "").strip().lower()
            level = (row.get(cefr_col) or "").strip().upper()
            if word and level:
                cefr_map[word] = level
    return cefr_map


def extract_ipa(entry: dict[str, Any]) -> str:
    sounds = entry.get("sounds") or []
    preferred = ""
    fallback = ""
    for s in sounds:
        if not isinstance(s, dict):
            continue
        ipa = (s.get("ipa") or "").strip()
        if not ipa:
            continue
        tags = {str(t).lower() for t in (s.get("tags") or [])}
        if not fallback:
            fallback = ipa
        if tags & {"us", "general-american", "uk", "received-pronunciation", "rp"}:
            preferred = ipa
            break
    return preferred or fallback


def extract_polish_translations(entry: dict[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def collect(trans_list: Any) -> None:
        if not isinstance(trans_list, list):
            return
        for tr in trans_list:
            if not isinstance(tr, dict):
                continue
            code = str(tr.get("code") or tr.get("lang_code") or "").lower()
            lang = str(tr.get("lang") or "").lower()
            if code == "pl" or lang == "polish":
                w = str(tr.get("word") or "").strip()
                if w and w.lower() not in seen:
                    seen.add(w.lower())
                    found.append(w)

    collect(entry.get("translations"))
    for sense in entry.get("senses") or []:
        if isinstance(sense, dict):
            collect(sense.get("translations"))
    return found[:4]


def extract_senses_info(entry: dict[str, Any]) -> tuple[str, list[str], set[str]]:
    glosses: list[str] = []
    examples: list[str] = []
    tags: set[str] = set()

    for sense in entry.get("senses") or []:
        if not isinstance(sense, dict):
            continue
        for t in sense.get("tags") or []:
            tags.add(str(t).lower())
        for t in sense.get("topics") or []:
            tags.add(str(t).lower())
        for g in sense.get("glosses") or []:
            if isinstance(g, str) and g.strip():
                glosses.append(g.strip())
                break
        for ex in sense.get("examples") or []:
            if isinstance(ex, dict) and isinstance(ex.get("text"), str) and ex["text"].strip():
                examples.append(ex["text"].strip())
            elif isinstance(ex, str) and ex.strip():
                examples.append(ex.strip())

    definition = glosses[0] if glosses else ""
    return definition, examples[:3], tags


def infer_category(pos: str, tags: set[str], phrase: str) -> str:
    if tags & WORKPLACE_KEYWORDS:
        return "Workplace"
    if pos in SYNTAX_POS or phrase.endswith("..."):
        return "Syntax"
    return "Everyday"


def infer_register(tags: set[str], category: str) -> str:
    for tag, reg in REGISTER_TAG_MAP.items():
        if tag in tags:
            return reg
    return "Workplace" if category == "Workplace" else "Conversational"


def convert_kaikki_entry(
    entry: dict[str, Any],
    *,
    allowed_pos: set[str],
    cefr_map: dict[str, str],
    allowed_cefr: set[str] | None,
    min_words: int,
    max_words: int,
    require_polish: bool,
    require_ipa: bool,
    id_prefix: str = "wk",
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    lang_code = str(entry.get("lang_code") or "").lower()
    lang = str(entry.get("lang") or "").lower()
    if lang_code and lang_code != "en":
        return None
    if not lang_code and lang and lang != "english":
        return None

    phrase = str(entry.get("word") or "").strip()
    if not phrase or len(phrase) < 2 or len(phrase) > 90:
        return None
    if re.search(r"[\[\]{}|<>]", phrase):
        return None

    pos = str(entry.get("pos") or "").lower().strip()
    word_count = len(phrase.split())
    if word_count < min_words or word_count > max_words:
        return None

    definition, examples, tags = extract_senses_info(entry)
    is_idiomatic = bool(tags & {"idiomatic", "idiom", "colloquial", "informal"})
    if allowed_pos and pos not in allowed_pos and not (word_count >= 2 and is_idiomatic):
        return None

    if not definition:
        return None

    ipa = extract_ipa(entry)
    if require_ipa and not ipa:
        return None

    polish_list = extract_polish_translations(entry)
    if require_polish and not polish_list:
        return None

    cefr = cefr_map.get(phrase.lower(), "")
    if allowed_cefr and cefr and cefr not in allowed_cefr:
        return None
    if allowed_cefr and cefr_map and not cefr:
        return None

    category = infer_category(pos, tags, phrase)
    register = infer_register(tags, category)

    upgraded = examples[0] if examples else phrase
    dialogues = [
        {"context": f"Wiktionary Example {i + 1}", "lines": [{"speaker": "Example", "text": ex}]}
        for i, ex in enumerate(examples[:2])
    ]

    nuance_parts = [definition]
    notable_tags = sorted(tags & set(REGISTER_TAG_MAP.keys()))
    if notable_tags:
        nuance_parts.append(f"Usage tags: {', '.join(notable_tags)}.")

    seed = {
        "id": make_stable_id(id_prefix, phrase),
        "phrase": phrase,
        "ipa": ipa or "/.../",
        "category": category,
        "register": register,
        "cefr": cefr or "B2-C1",
        "flat": definition,
        "upgraded": upgraded,
        "dialogues": dialogues,
        "nuance": " ".join(nuance_parts),
        "polish": " / ".join(polish_list) if polish_list else "—",
        "source": "Wiktionary via Kaikki.org (CC BY-SA)",
    }
    return normalize_item(seed, default_category=category, id_prefix=id_prefix)


def ingest_kaikki_file(
    input_path: Path,
    output_path: Path,
    *,
    allowed_pos: set[str] | None = None,
    cefr_csv: Path | None = None,
    allowed_cefr: set[str] | None = None,
    min_words: int = 2,
    max_words: int = 7,
    require_polish: bool = False,
    require_ipa: bool = False,
    limit: int | None = None,
    id_prefix: str = "wk",
) -> dict[str, int]:
    pos_filter = allowed_pos if allowed_pos is not None else set(DEFAULT_POS)
    cefr_map = load_cefr_map(cefr_csv)

    seen_phrases: set[str] = set()
    scanned = 0
    written = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open_text(input_path) as fin, output_path.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            scanned += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            item = convert_kaikki_entry(
                raw,
                allowed_pos=pos_filter,
                cefr_map=cefr_map,
                allowed_cefr=allowed_cefr,
                min_words=min_words,
                max_words=max_words,
                require_polish=require_polish,
                require_ipa=require_ipa,
                id_prefix=id_prefix,
            )
            if not item:
                continue

            key = item["phrase"].lower()
            if key in seen_phrases:
                continue
            seen_phrases.add(key)

            fout.write(json.dumps(item, ensure_ascii=False) + "\n")
            written += 1
            if limit and written >= limit:
                break

    return {"scanned": scanned, "written": written}


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract English phrases/idioms/words from Kaikki.org JSONL into Lexis seed JSONL.")
    parser.add_argument("--input", "-i", type=Path, required=True, help="Path to Kaikki .jsonl or .jsonl.gz dump.")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output Lexis seed .jsonl path.")
    parser.add_argument("--pos", default=",".join(sorted(DEFAULT_POS)), help="Comma-separated Wiktionary POS values to include (empty = all).")
    parser.add_argument("--cefr-csv", type=Path, default=None, help="Optional CEFR-J / Words-CEFR-Dataset CSV file.")
    parser.add_argument("--cefr", default="", help="Comma-separated CEFR levels to keep (e.g. B2,C1,C2).")
    parser.add_argument("--min-words", type=int, default=2, help="Minimum word count in phrase (default: 2 for multi-word phrases).")
    parser.add_argument("--max-words", type=int, default=7, help="Maximum word count in phrase (default: 7).")
    parser.add_argument("--require-polish", action="store_true", help="Only keep entries with Polish translations in Wiktionary.")
    parser.add_argument("--require-ipa", action="store_true", help="Only keep entries with IPA pronunciation.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after writing N entries.")
    parser.add_argument("--id-prefix", default="wk", help="Deterministic ID prefix (default: wk).")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    allowed_pos = {p.strip().lower() for p in args.pos.split(",") if p.strip()} if args.pos else set()
    allowed_cefr = {c.strip().upper() for c in args.cefr.split(",") if c.strip()} if args.cefr else None

    stats = ingest_kaikki_file(
        args.input,
        args.output,
        allowed_pos=allowed_pos,
        cefr_csv=args.cefr_csv,
        allowed_cefr=allowed_cefr,
        min_words=args.min_words,
        max_words=args.max_words,
        require_polish=args.require_polish,
        require_ipa=args.require_ipa,
        limit=args.limit,
        id_prefix=args.id_prefix,
    )
    print(f"Kaikki ingest complete: scanned {stats['scanned']} lines -> wrote {stats['written']} seed items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
