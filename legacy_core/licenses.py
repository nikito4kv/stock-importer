from __future__ import annotations

import re
from typing import Any


def normalize_license_info(
    source: str,
    raw_license: str,
    raw_license_url: str | None,
) -> tuple[str, bool, bool]:
    del raw_license_url

    value = (raw_license or "").strip()
    lower = value.lower()
    compact = re.sub(r"[^a-z0-9]+", "", lower)

    if source == "pexels":
        return "pexels-license", True, False
    if source == "pixabay":
        return "pixabay-license", True, False

    if "publicdomain" in compact or "cc0" in compact or "pdm" in compact:
        return value or "public-domain", True, False

    if "noncommercial" in lower or re.search(r"\bnc\b", lower):
        return value or "non-commercial", False, True

    if "ccbysa" in compact or "ccby" in compact or "by-sa" in lower or "cc by" in lower:
        return value or "cc-by", True, True

    if "cc" in lower:
        return value or "cc-license", True, True

    return value or "unknown", False, True


def is_license_allowed(
    candidate: Any,
    commercial_only: bool,
    allow_attribution_licenses: bool,
) -> bool:
    del candidate, commercial_only, allow_attribution_licenses
    return True
