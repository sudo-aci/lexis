"""Schema normalization, deterministic ID generation, validation, and deduplication for Lexis phrase cards."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

VALID_CATEGORIES = ("Everyday", "Workplace", "Syntax")
VALID_REGISTERS = ("Casual", "Conversational", "Workplace", "Diplomatic", "Nuanced")
VALID_CEFR = ("A1", "A2", "B1", "B2", "B2-C1", "C1", "C2")

STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "at", "for", "with", "it", "its",
    "it's", "is", "are", "be", "my", "your", "our", "their", "this", "that",
    "someone", "something", "one's", "i", "you", "we", "they", "me", "him", "her",
}


def make_stable_id(prefix: str, phrase: str) -> str:
    """Create a deterministic ID from prefix + normalized phrase so pack rebuilds preserve user progress."""
    norm = re.sub(r"\s+", " ", phrase.strip().lower())
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
    clean_prefix = re.sub(r"[^a-z0-9]+", "", prefix.lower()) or "lex"
    return f"{clean_prefix}-{digest}"


def pick_cloze_word(phrase: str) -> str:
    """Pick the most informative content word in a phrase for default cloze exercises."""
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", phrase)
    if not tokens:
        return phrase.strip()
    content = [t for t in tokens if t.lower() not in STOPWORDS and len(t) >= 3]
    candidates = content or tokens
    # Prefer the longest informative word (often the idiom's anchor word)
    return max(candidates, key=lambda t: (len(t), candidates.index(t)))


def build_default_cloze(sentence: str, phrase: str) -> tuple[str, str]:
    """Blank out a key word of `phrase` inside `sentence`."""
    target = pick_cloze_word(phrase)
    pattern = re.compile(rf"\b{re.escape(target)}\b", re.IGNORECASE)
    match = pattern.search(sentence)
    if match:
        matched_word = match.group(0)
        cloze_sentence = sentence[: match.start()] + "___" + sentence[match.end() :]
        return cloze_sentence, matched_word
    return f"{phrase} — ___", target


def normalize_category(raw_cat: Any, default: str = "Everyday") -> str:
    if not isinstance(raw_cat, str):
        return default
    cleaned = raw_cat.strip()
    if cleaned == "Syntax & Flow":
        return "Syntax"
    for valid in VALID_CATEGORIES:
        if cleaned.lower() == valid.lower():
            return valid
    return default


def normalize_register(raw_reg: Any, default: str = "Conversational") -> str:
    if not isinstance(raw_reg, str) or not raw_reg.strip():
        return default
    cleaned = raw_reg.strip()
    for valid in VALID_REGISTERS:
        if cleaned.lower() == valid.lower():
            return valid
    mapping = {
        "informal": "Casual",
        "slang": "Casual",
        "colloquial": "Conversational",
        "idiomatic": "Conversational",
        "business": "Workplace",
        "corporate": "Workplace",
        "formal": "Diplomatic",
        "polite": "Diplomatic",
        "literary": "Nuanced",
        "academic": "Nuanced",
    }
    return mapping.get(cleaned.lower(), default)


def normalize_dialogues(entry: dict[str, Any], fallback_sentence: str) -> list[dict[str, Any]]:
    raw = entry.get("dialogues") or entry.get("examples")
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for dlg in raw:
            if isinstance(dlg, dict) and isinstance(dlg.get("lines"), list) and dlg["lines"]:
                lines = []
                for line in dlg["lines"]:
                    if isinstance(line, dict) and isinstance(line.get("text"), str) and line["text"].strip():
                        lines.append({
                            "speaker": str(line.get("speaker") or "Speaker").strip(),
                            "text": line["text"].strip(),
                        })
                if lines:
                    out.append({
                        "context": str(dlg.get("context") or "In Context").strip(),
                        "lines": lines,
                    })
            elif isinstance(dlg, str) and dlg.strip():
                out.append({
                    "context": "In Context",
                    "lines": [{"speaker": "Speaker", "text": dlg.strip()}],
                })
    if not out:
        out.append({
            "context": "In Context",
            "lines": [{"speaker": "Speaker", "text": fallback_sentence}],
        })
    return out


def normalize_item(
    raw: dict[str, Any],
    *,
    default_category: str = "Everyday",
    id_prefix: str = "lex",
    pack_id: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a raw or partial dictionary into a schema-valid Lexis card."""
    if not isinstance(raw, dict):
        return None
    phrase = raw.get("phrase") or raw.get("word") or raw.get("idiom")
    if not isinstance(phrase, str) or not phrase.strip():
        return None
    phrase = re.sub(r"\s+", " ", phrase.strip())

    item_id = str(raw.get("id") or make_stable_id(id_prefix, phrase)).strip()
    category = normalize_category(raw.get("category"), default=default_category)
    register = normalize_register(raw.get("register"), default="Workplace" if category == "Workplace" else "Conversational")
    cefr = str(raw.get("cefr") or "B2-C1").strip()

    ipa_raw = str(raw.get("ipa") or "").strip()
    if ipa_raw and not (ipa_raw.startswith("/") or ipa_raw.startswith("[")):
        ipa = f"/{ipa_raw}/"
    else:
        ipa = ipa_raw or "/.../"

    speech_text = str(raw.get("speechText") or re.sub(r"\[.*?\]", "something", phrase)).strip()
    flat = str(raw.get("flat") or raw.get("flatVersion") or raw.get("definition") or f"Standard way to express '{phrase}'.").strip()
    upgraded = str(raw.get("upgraded") or raw.get("naturalVersion") or raw.get("example") or phrase).strip()
    dialogues = normalize_dialogues(raw, upgraded)

    nuance = str(
        raw.get("nuance")
        or raw.get("definition")
        or "Pay attention to conversational register, prepositions, and natural collocations."
    ).strip()
    polish = str(raw.get("polish") or raw.get("polishEquivalent") or "—").strip()

    cloze_sentence = str(raw.get("clozeSentence") or "").strip()
    cloze_answer = str(raw.get("clozeAnswer") or "").strip()
    if not cloze_sentence or "___" not in cloze_sentence or not cloze_answer:
        cloze_sentence, cloze_answer = build_default_cloze(upgraded, phrase)

    raw_accepted = raw.get("clozeAccepted")
    if isinstance(raw_accepted, list) and raw_accepted:
        cloze_accepted = [str(x).strip() for x in raw_accepted if str(x).strip()]
    else:
        cloze_accepted = [cloze_answer]
    if cloze_answer not in cloze_accepted:
        cloze_accepted.insert(0, cloze_answer)

    cloze_hint = str(raw.get("clozeHint") or f"{len(cloze_answer)}-letter word completing the expression.").strip()

    quiz_prompt = str(raw.get("quizPrompt") or flat).strip()
    raw_options = raw.get("quizOptions")
    if isinstance(raw_options, list) and len(raw_options) >= 4:
        quiz_options = [str(o).strip() for o in raw_options[:4]]
        quiz_correct_index = raw.get("quizCorrectIndex", 0)
        if not isinstance(quiz_correct_index, int) or not (0 <= quiz_correct_index < 4):
            quiz_correct_index = 0
    else:
        quiz_options = [
            upgraded,
            flat if flat != upgraded else f"Standard phrasing for: {phrase}",
            f"Formally speaking, one might utilize '{phrase}' in this context.",
            f"Literal calque distortion of '{phrase}'.",
        ]
        quiz_correct_index = 0

    raw_explanations = raw.get("quizExplanations")
    if isinstance(raw_explanations, list) and len(raw_explanations) >= 4:
        quiz_explanations = [str(e).strip() for e in raw_explanations[:4]]
    else:
        quiz_explanations = [
            "Natural, idiomatic phrasing matching the target register.",
            "Functional textbook baseline without natural idiomatic polish.",
            "Overly stiff and unnatural for everyday speech.",
            "Awkward literal phrasing or collocation mismatch.",
        ]

    sandbox_prompt = str(raw.get("sandboxPrompt") or f'Write one natural sentence using "{phrase}" in a realistic situation.').strip()
    raw_sample = raw.get("sandboxSample")
    if not raw_sample and isinstance(raw.get("sandboxSamples"), list) and raw["sandboxSamples"]:
        raw_sample = raw["sandboxSamples"][0]
    sandbox_sample = str(raw_sample or upgraded).strip()

    raw_rubric = raw.get("sandboxRubric")
    if isinstance(raw_rubric, list) and raw_rubric:
        sandbox_rubric = [str(r).strip() for r in raw_rubric if str(r).strip()]
    else:
        sandbox_rubric = [
            f"Register: Matches {register.lower()} tone naturally.",
            f"Collocation: Uses '{phrase}' accurately in context.",
            "Cadence: Smooth, natural sentence rhythm.",
        ]

    item: dict[str, Any] = {
        "id": item_id,
        "phrase": phrase,
        "ipa": ipa,
        "speechText": speech_text,
        "category": category,
        "register": register,
        "cefr": cefr,
        "flat": flat,
        "upgraded": upgraded,
        "dialogues": dialogues,
        "nuance": nuance,
        "polish": polish,
        "clozeSentence": cloze_sentence,
        "clozeAnswer": cloze_answer,
        "clozeAccepted": cloze_accepted,
        "clozeHint": cloze_hint,
        "quizPrompt": quiz_prompt,
        "quizOptions": quiz_options,
        "quizCorrectIndex": quiz_correct_index,
        "quizExplanations": quiz_explanations,
        "sandboxPrompt": sandbox_prompt,
        "sandboxSample": sandbox_sample,
        "sandboxRubric": sandbox_rubric,
    }
    if pack_id or raw.get("packId"):
        item["packId"] = str(pack_id or raw["packId"])
    if raw.get("source"):
        item["source"] = str(raw["source"])
    return item


def validate_item(item: dict[str, Any]) -> list[str]:
    """Validate a normalized Lexis item and return a list of validation errors (empty if valid)."""
    errors: list[str] = []
    required_strings = [
        "id", "phrase", "ipa", "speechText", "category", "register",
        "flat", "upgraded", "nuance", "polish",
        "clozeSentence", "clozeAnswer", "clozeHint",
        "quizPrompt", "sandboxPrompt", "sandboxSample",
    ]
    for key in required_strings:
        val = item.get(key)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"Missing or empty string field '{key}'")

    if item.get("category") not in VALID_CATEGORIES:
        errors.append(f"Invalid category '{item.get('category')}' (expected one of {VALID_CATEGORIES})")

    if "___" not in str(item.get("clozeSentence", "")):
        errors.append("clozeSentence must contain '___'")

    dialogues = item.get("dialogues")
    if not isinstance(dialogues, list) or len(dialogues) == 0:
        errors.append("dialogues must be a non-empty list")
    else:
        for i, d in enumerate(dialogues):
            if not isinstance(d, dict) or not isinstance(d.get("lines"), list) or not d["lines"]:
                errors.append(f"dialogues[{i}] must contain a non-empty 'lines' list")

    opts = item.get("quizOptions")
    exps = item.get("quizExplanations")
    idx = item.get("quizCorrectIndex")
    if not isinstance(opts, list) or len(opts) < 4:
        errors.append("quizOptions must have at least 4 entries")
    if not isinstance(exps, list) or len(exps) < 4:
        errors.append("quizExplanations must have at least 4 entries")
    if not isinstance(idx, int) or (isinstance(opts, list) and not (0 <= idx < len(opts))):
        errors.append(f"quizCorrectIndex '{idx}' out of bounds")

    accepted = item.get("clozeAccepted")
    if not isinstance(accepted, list) or len(accepted) == 0:
        errors.append("clozeAccepted must be a non-empty list")

    rubric = item.get("sandboxRubric")
    if not isinstance(rubric, list) or len(rubric) == 0:
        errors.append("sandboxRubric must be a non-empty list")

    return errors


def richness_score(item: dict[str, Any]) -> int:
    """Heuristic score so deduplication keeps the richest version of a duplicate phrase."""
    score = 0
    if item.get("ipa") and item["ipa"] != "/.../":
        score += 2
    if item.get("polish") and item["polish"] != "—":
        score += 3
    if len(item.get("dialogues") or []) >= 2:
        score += 3
    elif len(item.get("dialogues") or []) == 1 and len((item["dialogues"][0].get("lines") or [])) >= 2:
        score += 2
    if len(str(item.get("nuance") or "")) > 60:
        score += 2
    if "Standard way to express" not in str(item.get("flat") or ""):
        score += 2
    return score


def dedupe_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate items by normalized lowercase phrase, preserving order and keeping the richest entry."""
    chosen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        key = re.sub(r"\s+", " ", str(item.get("phrase", "")).strip().lower())
        if not key:
            continue
        if key not in chosen:
            chosen[key] = item
            order.append(key)
        elif richness_score(item) > richness_score(chosen[key]):
            chosen[key] = item
    return [chosen[k] for k in order]
