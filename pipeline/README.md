# Lexis Data Pipeline

Zero-dependency Python 3.12 pipeline for turning open-license lexical datasets (**Kaikki.org / Wiktionary**, **Tatoeba EN–PL**, **CEFR-J / Words-CEFR-Dataset**, or custom phrase lists) into validated, sharded JSON packs under `data/packs/` and registering them in `data/manifest.json` for Lexis's IndexedDB pack loader.

## Pipeline Overview

```
Open Datasets (Kaikki JSONL / CEFR CSV / Tatoeba TSV / Plain phrase list)
  │
  ├─ 1. pipeline/ingest_kaikki.py   (stream Wiktionary JSONL -> idioms, IPA, register, Polish glosses)
  ├─ 2. pipeline/ingest_tatoeba.py  (attach authentic EN-PL sentences & cloze blanks)
  ├─ 3. pipeline/enrich_llm.py      (optional: upgrade seed cards -> full dialogues, nuance, quizzes)
  └─ 4. pipeline/build_packs.py     (validate, dedupe, shard into data/packs/*.json + update manifest)
```

## Quick Start

### 1. Extract idioms & phrasal verbs from Kaikki.org (Wiktionary)

Download the English Wiktextract JSONL dump (CC BY-SA) from [kaikki.org/dictionary/rawdata.html](https://kaikki.org/dictionary/rawdata.html) (and optionally a CEFR CSV from [openlanguageprofiles/olp-en-cefrj](https://github.com/openlanguageprofiles/olp-en-cefrj) or [Maximax67/Words-CEFR-Dataset](https://github.com/Maximax67/Words-CEFR-Dataset)):

```bash
python3 pipeline/ingest_kaikki.py \
  --input /path/to/kaikki.org-dictionary-English.jsonl.gz \
  --output /tmp/kaikki_seed.jsonl \
  --min-words 2 --max-words 6 \
  --require-ipa \
  --limit 2000
```

To filter by CEFR level as well:

```bash
python3 pipeline/ingest_kaikki.py \
  --input /path/to/kaikki.org-dictionary-English.jsonl.gz \
  --cefr-csv /path/to/cefrj-vocabulary-profile-1.5.csv \
  --cefr B2,C1,C2 \
  --min-words 1 \
  --output /tmp/cefr_b2c2_seed.jsonl
```

### 2. Enrich with real English–Polish example sentences from Tatoeba

Download English–Polish sentence pairs (CC BY 2.0 FR / CC0) from [tatoeba.org/en/downloads](https://tatoeba.org/en/downloads):

```bash
python3 pipeline/ingest_tatoeba.py \
  --seed /tmp/kaikki_seed.jsonl \
  --tatoeba /path/to/eng-pol-sentences.tsv \
  --output /tmp/kaikki_tatoeba.jsonl
```

### 3. (Optional) LLM-enrich into full Lexis cards

Upgrade raw seed entries (or a plain `.txt` list of one phrase per line) into full cards with `flat`/`upgraded` comparisons, 2 realistic dialogues, Polish-speaker nuance notes, cloze exercises, 4-option quizzes, and sandbox rubrics:

```bash
# Preview prompt & batch count without calling any API:
python3 pipeline/enrich_llm.py --input /tmp/kaikki_tatoeba.jsonl --output /tmp/enriched.jsonl --dry-run

# Run resumable enrichment (works with OpenAI, Gemini OpenAI-compat, OpenRouter, or local Ollama):
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
python3 pipeline/enrich_llm.py \
  --input /tmp/kaikki_tatoeba.jsonl \
  --output /tmp/enriched.jsonl \
  --model gpt-4o-mini \
  --batch-size 5
```

### 4. Shard into JSON packs & update `data/manifest.json`

```bash
python3 pipeline/build_packs.py \
  --input /tmp/enriched.jsonl \
  --pack-prefix wiktionary-b2c1 \
  --pack-name "Wiktionary Core (B2–C1)" \
  --description "High-frequency idioms and phrasal verbs enriched with EN–PL context." \
  --source "Wiktionary via Kaikki.org (CC BY-SA) + Tatoeba (CC BY)" \
  --shard-size 250
```

Reload Lexis and open **Packs & Backup → Refresh Catalog** to install the new sharded packs into IndexedDB (or click **Load Pack (.json)** to load a local `.json` pack directly).

### Refreshing `data/manifest.json` after manual edits

```bash
python3 pipeline/build_packs.py --rebuild-manifest-only
```

## Running Tests

```bash
python3 -m unittest discover -s pipeline/tests -v
```
