from __future__ import annotations

import re
import unicodedata


def safe_int(value: object, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def safe_float(value: object, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_keywords(raw_keywords: object) -> list[str]:
    if not isinstance(raw_keywords, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_keywords:
        if not isinstance(item, str):
            continue
        value = normalize_whitespace(item)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


def slugify(value: str, max_len: int = 40, default: str = "item") -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_").lower()
    if not cleaned:
        return default
    return cleaned[:max_len].strip("_") or default
