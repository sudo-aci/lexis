#!/usr/bin/env python3
"""Validate, deduplicate, and shard Lexis phrase datasets into data/packs/*.json + data/manifest.json.

Examples:
  # Shard a large JSONL file into 250-item category packs and update data/manifest.json:
  python3 pipeline/build_packs.py \\
    --input enriched.jsonl \\
    --pack-prefix kaikki-b2c1 \\
    --pack-name "Wiktionary Core (B2–C1)" \\
    --description "High-frequency idioms and phrasal verbs from Wiktionary + Tatoeba." \\
    --source "Wiktionary / Kaikki.org (CC BY-SA)" \\
    --shard-size 250

  # Rebuild data/manifest.json cleanly from all existing files in data/packs/:
  python3 pipeline/build_packs.py --rebuild-manifest-only
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.schema import dedupe_items, normalize_item, validate_item


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "pack"


def load_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            if path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz"):
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            else:
                parsed = json.loads(f.read())
                if isinstance(parsed, list):
                    records.extend(parsed)
                elif isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                    records.extend(parsed["items"])
                else:
                    raise ValueError(f"Unsupported JSON structure in {path}")
    return records


def write_pack_file(
    packs_dir: Path,
    *,
    pack_id: str,
    name: str,
    description: str,
    category: str,
    cefr: str,
    version: str,
    source: str,
    items: list[dict[str, Any]],
) -> Path:
    packs_dir.mkdir(parents=True, exist_ok=True)
    stamped_items = [{**item, "packId": pack_id} for item in items]
    payload = {
        "schemaVersion": 1,
        "pack": {
            "id": pack_id,
            "name": name,
            "description": description,
            "category": category,
            "cefr": cefr,
            "version": version,
            "source": source,
        },
        "items": stamped_items,
    }
    out_path = packs_dir / f"{pack_id}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path


def rebuild_manifest(
    packs_dir: Path,
    manifest_path: Path,
    *,
    default_pack_ids: set[str] | None = None,
    web_prefix: str = "data/packs",
) -> dict[str, Any]:
    """Scan all *.json packs in packs_dir and write a fresh data/manifest.json preserving default flags."""
    existing_defaults: set[str] = set()
    existing_order: list[str] = []
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            for p in old.get("packs") or []:
                pid = p.get("id")
                if pid:
                    existing_order.append(pid)
                    if p.get("default"):
                        existing_defaults.add(pid)
        except json.JSONDecodeError:
            pass

    if default_pack_ids is not None:
        existing_defaults.update(default_pack_ids)

    pack_entries: dict[str, dict[str, Any]] = {}
    for pfile in sorted(packs_dir.glob("*.json")):
        raw = pfile.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        meta = doc.get("pack") or {}
        items = doc.get("items") or []
        pack_id = str(meta.get("id") or pfile.stem)
        is_default = pack_id in existing_defaults or pack_id.startswith(("starter-", "mastery-"))
        pack_entries[pack_id] = {
            "id": pack_id,
            "name": str(meta.get("name") or pack_id),
            "description": str(meta.get("description") or ""),
            "category": str(meta.get("category") or "Mixed"),
            "cefr": str(meta.get("cefr") or "B2-C1"),
            "version": str(meta.get("version") or "1.0.0"),
            "source": str(meta.get("source") or "Lexis"),
            "file": f"{web_prefix.rstrip('/')}/{pfile.name}",
            "itemCount": len(items),
            "sizeBytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()[:16],
            "default": is_default,
        }

    # Keep starter packs first, then previously ordered packs, then newly added packs alphabetically
    ordered_ids: list[str] = []
    for pid in existing_order:
        if pid in pack_entries and pid not in ordered_ids:
            ordered_ids.append(pid)
    for pid in sorted(pack_entries.keys()):
        if pid not in ordered_ids:
            ordered_ids.append(pid)

    ordered_packs = [pack_entries[pid] for pid in ordered_ids]
    manifest = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalItems": sum(p["itemCount"] for p in ordered_packs),
        "packs": ordered_packs,
    }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def build_sharded_packs(
    input_paths: list[Path],
    packs_dir: Path,
    manifest_path: Path,
    *,
    pack_prefix: str,
    pack_name: str,
    description: str = "",
    cefr: str = "B2-C1",
    version: str = "1.0.0",
    source: str = "Lexis Pipeline",
    group_by: str = "category",
    shard_size: int = 250,
    is_default: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    raw_records = load_records(input_paths)
    normalized: list[dict[str, Any]] = []
    validation_errors: list[tuple[str, list[str]]] = []

    for rec in raw_records:
        item = normalize_item(rec, id_prefix=slugify(pack_prefix)[:6] or "lex")
        if not item:
            continue
        errs = validate_item(item)
        if errs:
            validation_errors.append((item.get("phrase", "<unknown>"), errs))
            if strict:
                continue
        normalized.append(item)

    deduped = dedupe_items(normalized)

    buckets: dict[str, list[dict[str, Any]]] = {}
    if group_by == "category":
        for item in deduped:
            buckets.setdefault(item["category"], []).append(item)
    else:
        buckets["Mixed"] = deduped

    written_files: list[Path] = []
    created_ids: list[str] = []

    for cat, items in sorted(buckets.items()):
        if not items:
            continue
        cat_slug = slugify(cat)
        total_shards = (len(items) + shard_size - 1) // shard_size
        for shard_idx in range(total_shards):
            chunk = items[shard_idx * shard_size : (shard_idx + 1) * shard_size]
            suffix = f"-{shard_idx + 1:02d}" if total_shards > 1 else ""
            if group_by == "category":
                pack_id = f"{slugify(pack_prefix)}-{cat_slug}{suffix}"
                shard_label = f" (Part {shard_idx + 1}/{total_shards})" if total_shards > 1 else ""
                full_name = f"{pack_name} · {cat}{shard_label}"
            else:
                pack_id = f"{slugify(pack_prefix)}{suffix}"
                shard_label = f" (Part {shard_idx + 1}/{total_shards})" if total_shards > 1 else ""
                full_name = f"{pack_name}{shard_label}"

            out = write_pack_file(
                packs_dir,
                pack_id=pack_id,
                name=full_name,
                description=description or f"{len(chunk)} {cat.lower()} expressions built by the Lexis pipeline.",
                category=cat,
                cefr=cefr,
                version=version,
                source=source,
                items=chunk,
            )
            written_files.append(out)
            created_ids.append(pack_id)

    manifest = rebuild_manifest(
        packs_dir,
        manifest_path,
        default_pack_ids=set(created_ids) if is_default else None,
    )
    return {
        "input_records": len(raw_records),
        "valid_unique_items": len(deduped),
        "validation_errors": len(validation_errors),
        "packs_written": [str(p) for p in written_files],
        "manifest_total_items": manifest["totalItems"],
        "manifest_pack_count": len(manifest["packs"]),
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate, shard, and register Lexis JSON phrase packs.")
    parser.add_argument("--input", "-i", type=Path, action="append", default=[], help="Input .json, .jsonl, or .jsonl.gz file (repeatable).")
    parser.add_argument("--packs-dir", type=Path, default=Path("data/packs"), help="Directory for sharded pack JSON files.")
    parser.add_argument("--manifest", type=Path, default=Path("data/manifest.json"), help="Path to manifest.json.")
    parser.add_argument("--pack-prefix", default="custom", help="ID prefix for generated packs (e.g. kaikki-c1).")
    parser.add_argument("--pack-name", default="Custom Pack", help="Display title prefix for generated packs.")
    parser.add_argument("--description", default="", help="Pack description shown in the UI.")
    parser.add_argument("--cefr", default="B2-C1", help="CEFR label shown in the Pack Manager.")
    parser.add_argument("--version", default="1.0.0", help="Pack semantic version.")
    parser.add_argument("--source", default="Lexis Pipeline", help="Source attribution string.")
    parser.add_argument("--group-by", choices=["category", "single"], default="category", help="Group into category packs or one combined pack.")
    parser.add_argument("--shard-size", type=int, default=250, help="Maximum items per sharded JSON pack file (default: 250).")
    parser.add_argument("--default", action="store_true", dest="is_default", help="Mark generated packs as auto-installed defaults.")
    parser.add_argument("--rebuild-manifest-only", action="store_true", help="Only regenerate data/manifest.json from existing packs.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.rebuild_manifest_only:
        manifest = rebuild_manifest(args.packs_dir, args.manifest)
        print(f"Rebuilt {args.manifest}: {len(manifest['packs'])} packs, {manifest['totalItems']} total phrases.")
        return 0

    if not args.input:
        print("Error: provide at least one --input file (or --rebuild-manifest-only).", file=sys.stderr)
        return 2

    result = build_sharded_packs(
        args.input,
        args.packs_dir,
        args.manifest,
        pack_prefix=args.pack_prefix,
        pack_name=args.pack_name,
        description=args.description,
        cefr=args.cefr,
        version=args.version,
        source=args.source,
        group_by=args.group_by,
        shard_size=args.shard_size,
        is_default=args.is_default,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
