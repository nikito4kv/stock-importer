from __future__ import annotations

import re
from typing import Any, Iterable

from .common import normalize_whitespace


def parse_sources(value: str, default_sources: Iterable[str]) -> list[str]:
    raw_items = [item.strip().lower() for item in str(value or "").split(",")]
    items = [item for item in raw_items if item]
    if not items:
        items = [str(item).strip().lower() for item in default_sources if str(item).strip()]

    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{2,}", (text or "").lower())


def candidate_hint_score(keyword: str, candidate: Any) -> float:
    key_tokens = set(tokenize(keyword))
    if not key_tokens:
        return 0.0

    text = " ".join(
        [
            str(getattr(candidate, "url", "") or ""),
            str(getattr(candidate, "referrer_url", "") or ""),
            str(getattr(candidate, "author", "") or ""),
            str(getattr(candidate, "license_name", "") or ""),
        ]
    )
    text_tokens = set(tokenize(text))
    overlap = len(key_tokens & text_tokens)
    return overlap / max(1, len(key_tokens))


def build_query_variants(
    keyword: str,
    paragraph_text: str,
    short_suffixes: Iterable[str],
    paragraph_word_limit: int = 12,
) -> list[str]:
    base = normalize_whitespace(keyword)
    variants = [base] if base else []

    if base and len(base.split()) <= 5:
        for suffix in short_suffixes:
            suffix_value = normalize_whitespace(suffix)
            if suffix_value:
                variants.append(f"{base} {suffix_value}")

    paragraph_hint = normalize_whitespace(paragraph_text)
    if base and paragraph_hint:
        snippet = " ".join(paragraph_hint.split()[:paragraph_word_limit])
        if snippet:
            variants.append(f"{base} {snippet}")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in variants:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized
