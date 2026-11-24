"""Unit and end-to-end tests for the Lexis Python data pipeline and existing data packs."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.build_packs import build_sharded_packs, rebuild_manifest
from pipeline.enrich_llm import enrich_seeds
from pipeline.ingest_kaikki import ingest_kaikki_file
from pipeline.ingest_tatoeba import enrich_seed_with_tatoeba, extract_tatoeba_standalone
from pipeline.schema import dedupe_items, make_stable_id, normalize_item, validate_item

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestSchema(unittest.TestCase):
    def test_stable_id_is_deterministic(self) -> None:
        id1 = make_stable_id("wk", "For What It's Worth")
        id2 = make_stable_id("wk", "  for   what it's worth ")
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("wk-"))

    def test_normalize_and_validate_minimal_entry(self) -> None:
        item = normalize_item({"phrase": "cut to the chase", "category": "Workplace"})
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(validate_item(item), [])
        self.assertIn("___", item["clozeSentence"])
        self.assertEqual(len(item["quizOptions"]), 4)
        self.assertEqual(len(item["quizExplanations"]), 4)

    def test_dedupe_keeps_richest_entry(self) -> None:
        sparse = normalize_item({"phrase": "touch base", "category": "Workplace"})
        rich = normalize_item({
            "phrase": "Touch Base",
            "category": "Workplace",
            "ipa": "/tʌtʃ beɪs/",
            "polish": "odezwać się / skontaktować krótko",
            "nuance": "Used in casual workplace check-ins when reconnecting briefly on a project.",
            "dialogues": [
                {"context": "Slack", "lines": [{"speaker": "A", "text": "Busy week?"}, {"speaker": "B", "text": "Let's touch base Friday."}]}
            ],
        })
        assert sparse is not None and rich is not None
        result = dedupe_items([sparse, rich])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ipa"], "/tʌtʃ beɪs/")
        self.assertIn("skontaktować", result[0]["polish"])


class TestIngestAndShardingPipeline(unittest.TestCase):
    def test_end_to_end_kaikki_tatoeba_enrich_and_shard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 1. Sample gzipped Kaikki.org JSONL
            kaikki_rows = [
                {
                    "word": "bite the bullet",
                    "lang_code": "en",
                    "pos": "verb",
                    "sounds": [{"ipa": "/baɪt ðə ˈbʊlɪt/", "tags": ["UK"]}],
                    "senses": [{
                        "glosses": ["To endure a painful or difficult situation bravely."],
                        "tags": ["idiomatic", "colloquial"],
                        "examples": [{"text": "I finally bit the bullet and refactored the legacy parser."}],
                    }],
                    "translations": [{"code": "pl", "lang": "Polish", "word": "zacisnąć zęby"}],
                },
                {
                    "word": "circle back",
                    "lang_code": "en",
                    "pos": "verb",
                    "sounds": [{"ipa": "/ˈsɜːrkəl bæk/", "tags": ["US"]}],
                    "senses": [{
                        "glosses": ["To return to a topic later after gathering more information."],
                        "tags": ["idiomatic", "business"],
                    }],
                    "translations": [{"code": "pl", "lang": "Polish", "word": "wrócić do tematu"}],
                },
                {
                    "word": "nie-angielskie słowo",
                    "lang_code": "pl",
                    "pos": "phrase",
                    "senses": [{"glosses": ["Should be skipped because lang_code != en"]}],
                },
                {
                    "word": "on the fence",
                    "lang_code": "en",
                    "pos": "prep_phrase",
                    "sounds": [{"ipa": "/ɒn ðə fens/", "tags": ["UK"]}],
                    "senses": [{"glosses": ["Undecided between two options."], "tags": ["idiomatic"]}],
                },
            ]
            kaikki_gz = tmp / "kaikki_sample.jsonl.gz"
            with gzip.open(kaikki_gz, "wt", encoding="utf-8") as f:
                for row in kaikki_rows:
                    f.write(json.dumps(row) + "\n")

            seed_jsonl = tmp / "seed.jsonl"
            stats = ingest_kaikki_file(kaikki_gz, seed_jsonl, min_words=2, require_ipa=True)
            self.assertEqual(stats["written"], 3)

            # 2. Sample Tatoeba EN-PL TSV enrichment
            tatoeba_tsv = tmp / "tatoeba.tsv"
            tatoeba_tsv.write_text(
                "101\tI'm still on the fence about taking that job offer.\t201\tNadal się waham, czy przyjąć tę ofertę pracy.\n"
                "102\tLet's circle back to the budget once design signs off.\t202\tWróćmy do budżetu, gdy design da akcept.\n",
                encoding="utf-8",
            )
            tatoeba_out = tmp / "seed_tatoeba.jsonl"
            t_stats = enrich_seed_with_tatoeba(seed_jsonl, tatoeba_tsv, tatoeba_out)
            self.assertEqual(t_stats["enriched"], 2)

            enriched_rows = [json.loads(line) for line in tatoeba_out.read_text(encoding="utf-8").splitlines() if line.strip()]
            fence_item = next(r for r in enriched_rows if r["phrase"] == "on the fence")
            self.assertIn("Nadal się waham", fence_item["polish"])
            self.assertIn("___", fence_item["clozeSentence"])

            # 3. Mock LLM enrichment + resumability check
            llm_out = tmp / "llm_enriched.jsonl"

            def fake_llm(_prompt: str) -> str:
                return json.dumps([
                    {
                        "phrase": "on the fence",
                        "flat": "I have not decided yet.",
                        "upgraded": "I'm still on the fence about moving abroad this year.",
                        "polish": "wahać się / nie móc się zdecydować",
                    }
                ])

            llm_stats = enrich_seeds([fence_item], llm_out, batch_size=5, caller=fake_llm)
            self.assertEqual(llm_stats["written"], 1)
            # Second run should skip already enriched phrase
            llm_stats_2 = enrich_seeds([fence_item], llm_out, batch_size=5, caller=fake_llm)
            self.assertEqual(llm_stats_2["skipped"], 1)
            self.assertEqual(llm_stats_2["written"], 0)

            # 4. Shard into JSON packs (shard_size=1 forces multi-part sharding!) + build manifest
            packs_dir = tmp / "packs"
            manifest_path = tmp / "manifest.json"
            build_res = build_sharded_packs(
                [tatoeba_out, llm_out],
                packs_dir,
                manifest_path,
                pack_prefix="test-wikt",
                pack_name="Test Wiktionary",
                shard_size=1,
                group_by="category",
            )
            self.assertEqual(build_res["valid_unique_items"], 3)
            self.assertEqual(build_res["manifest_total_items"], 3)
            self.assertEqual(build_res["manifest_pack_count"], 3)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["totalItems"], 3)
            self.assertTrue(all(p["sha256"] and p["sizeBytes"] > 0 for p in manifest["packs"]))

    def test_tatoeba_standalone_extract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tsv = tmp / "pairs.tsv"
            tsv.write_text(
                "How are things going lately?\tJak tam ostatnio leci?\n"
                "That makes complete sense to me.\tTo ma dla mnie pełen sens.\n",
                encoding="utf-8",
            )
            out = tmp / "out.jsonl"
            stats = extract_tatoeba_standalone(tsv, out, limit=10)
            self.assertEqual(stats["written"], 2)


class TestRepoPacksIntegrity(unittest.TestCase):
    def test_all_shipped_packs_and_manifest_are_valid(self) -> None:
        manifest_path = REPO_ROOT / "data" / "manifest.json"
        self.assertTrue(manifest_path.exists(), "data/manifest.json must exist")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        total = 0
        seen_ids: set[str] = set()
        for pack_meta in manifest["packs"]:
            pack_file = REPO_ROOT / pack_meta["file"]
            self.assertTrue(pack_file.exists(), f"Missing pack file {pack_file}")
            doc = json.loads(pack_file.read_text(encoding="utf-8"))
            items = doc["items"]
            self.assertEqual(len(items), pack_meta["itemCount"])
            total += len(items)
            for item in items:
                errs = validate_item(item)
                self.assertEqual(errs, [], f"Invalid item {item.get('id')} in {pack_file}: {errs}")
                self.assertNotIn(item["id"], seen_ids, f"Duplicate item ID across packs: {item['id']}")
                seen_ids.add(item["id"])

        self.assertEqual(total, manifest["totalItems"])

    def test_c2_pack_present_and_balanced(self) -> None:
        c2_file = REPO_ROOT / "data" / "packs" / "expansion-mastery-c2.json"
        self.assertTrue(c2_file.exists(), "C2 mastery pack must exist")
        doc = json.loads(c2_file.read_text(encoding="utf-8"))
        self.assertEqual(doc["pack"]["cefr"], "C2")
        items = doc["items"]
        self.assertEqual(len(items), 18)
        self.assertTrue(all(i.get("cefr") == "C2" for i in items))
        cats = {i["category"] for i in items}
        self.assertEqual(cats, {"Everyday", "Workplace", "Syntax"})

    def test_c2_slang_casual_pack_and_defaults(self) -> None:
        slang_file = REPO_ROOT / "data" / "packs" / "mastery-c2-slang-casual.json"
        self.assertTrue(slang_file.exists(), "C2 slang/casual pack must exist")
        doc = json.loads(slang_file.read_text(encoding="utf-8"))
        self.assertEqual(doc["pack"]["cefr"], "C2")
        items = doc["items"]
        self.assertEqual(len(items), 15)
        self.assertTrue(all(i.get("cefr") == "C2" for i in items))
        registers = {i["register"] for i in items}
        self.assertTrue({"Slang", "Casual", "Workplace"}.issubset(registers))

        manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
        by_id = {p["id"]: p for p in manifest["packs"]}
        self.assertTrue(by_id["expansion-mastery-c2"]["default"])
        self.assertTrue(by_id["mastery-c2-slang-casual"]["default"])
        self.assertTrue(by_id["expansion-precision-c1"]["default"])

        # Ensure mastery-c2-slang-casual introduces zero duplicate phrases vs all other packs
        other_phrases: set[str] = set()
        for pack_file in (REPO_ROOT / "data" / "packs").glob("*.json"):
            if pack_file.name == "mastery-c2-slang-casual.json":
                continue
            other_doc = json.loads(pack_file.read_text(encoding="utf-8"))
            for other_item in other_doc["items"]:
                other_phrases.add(other_item["phrase"].strip().lower())

        for item in items:
            self.assertNotIn(
                item["phrase"].strip().lower(),
                other_phrases,
                f"Duplicate phrase in mastery-c2-slang-casual.json: {item['phrase']}",
            )

    def test_slang_register_normalization(self) -> None:
        from pipeline.schema import normalize_register

        self.assertEqual(normalize_register("slang"), "Slang")
        self.assertEqual(normalize_register("Slang"), "Slang")
        item = normalize_item({"phrase": "low-key", "register": "slang", "cefr": "c2"})
        assert item is not None
        self.assertEqual(item["register"], "Slang")
        self.assertEqual(item["cefr"], "C2")


if __name__ == "__main__":
    unittest.main()
