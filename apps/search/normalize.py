"""Query normalisation (§6.3 L0).

Two different normalisations, for two different jobs:

- `normalize()` keeps the sentence intact (lowercase, unaccented, collapsed). This is what gets
  embedded, because embeddings want natural language — stripping "para mi" out of "regalo para mi
  madre" throws away exactly the relationship signal the facets were written to match.
- `cache_key()` additionally drops stopwords, so "un regalo para mi madre" and "regalo madre"
  share one cache entry and one `QueryDemand` row instead of looking like distinct demand.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

MAX_QUERY_CHARS = 200

#: Deliberately short. These carry no gift signal, so they only add noise to the cache key and
#: split demand across phrasings. Anything that could disambiguate a recipient stays.
STOPWORDS: frozenset[str] = frozenset(
    {
        "un",
        "una",
        "unos",
        "unas",
        "el",
        "la",
        "los",
        "las",
        "lo",
        "de",
        "del",
        "al",
        "a",
        "en",
        "y",
        "o",
        "que",
        "qué",
        "para",
        "por",
        "con",
        "sin",
        "es",
        "ser",
        "algo",
        "alguna",
        "alguno",
        "cual",
        "cuál",
        "como",
        "cómo",
        "me",
        "mi",
        "mis",
        "su",
        "sus",
        "le",
        "se",
    }
)

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace. Word order preserved."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()[:MAX_QUERY_CHARS]


def strip_stopwords(normalized: str) -> str:
    words = [w for w in normalized.split() if w not in STOPWORDS]
    # If the query was nothing but stopwords, keep it as-is rather than hashing an empty string.
    return " ".join(words) or normalized


def cache_key(normalized: str, slots: dict | None = None) -> str:
    """md5 of the stopword-free query plus its slots. Same key ⇒ same results, by definition."""
    payload = json.dumps(
        {"q": strip_stopwords(normalized), "s": _canonical_slots(slots)},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


def _canonical_slots(slots: dict | None) -> dict:
    """Sort list values so {"a": ["x","y"]} and {"a": ["y","x"]} hash identically."""
    out: dict = {}
    for key in sorted(slots or {}):
        value = slots[key]
        if value in (None, "", [], {}):
            continue
        out[key] = sorted(value) if isinstance(value, list) else value
    return out


def slots_to_sentence(slots: dict | None, free_text: str = "") -> str:
    """Compose advanced-search slots into a Spanish sentence to embed (§6.5).

    The whole point of advanced search is that it costs no LLM call: we build the sentence with
    string formatting, embed it, and run the identical L1–L4 pipeline as simple search.
    """
    from apps.catalog import vocabularies

    slots = slots or {}
    parts: list[str] = ["regalo"]

    recipients = [vocabularies.label(k).lower() for k in slots.get("recipients", [])]
    if recipients:
        parts.append(f"para {_join(recipients)}")

    occasions = [vocabularies.label(k).lower() for k in slots.get("occasions", [])]
    if occasions:
        parts.append(f"en {_join(occasions)}")

    interests = [vocabularies.label(k).lower() for k in slots.get("interests", [])]
    if interests:
        parts.append(f"a quien le gusta {_join(interests)}")

    if free_text.strip():
        parts.append(free_text.strip())

    return " ".join(parts)


def _join(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} y {values[-1]}"
