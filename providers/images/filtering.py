from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from legacy_core.licenses import is_license_allowed
from legacy_core.query_utils import candidate_hint_score

from ..base import ProviderDescriptor
from .clients import SearchCandidate

LOW_QUALITY_TOKENS = {
    "screenshot",
    "illustration",
    "vector",
    "logo",
    "icon",
    "meme",
    "ui",
    "interface",
    "clipart",
}


@dataclass(slots=True)
class ImageLicensePolicy:
    commercial_only: bool = True
    allow_attribution_licenses: bool = False


@dataclass(slots=True)
class RankedCandidate:
    candidate: SearchCandidate
    score: float
    reasons: list[str] = field(default_factory=list)


def filter_and_rank_candidates(
    descriptor: ProviderDescriptor,
    keyword: str,
    candidates: list[SearchCandidate],
    *,
    license_policy: ImageLicensePolicy,
    metadata_cache: Any | None = None,
) -> tuple[list[SearchCandidate], list[str]]:
    accepted: list[RankedCandidate] = []
    rejected_reasons: list[str] = []
    for candidate in candidates:
        if not is_license_allowed(
            candidate,
            commercial_only=license_policy.commercial_only,
            allow_attribution_licenses=license_policy.allow_attribution_licenses,
        ):
            rejected_reasons.append(f"{descriptor.provider_id}:license")
            continue

        quality = cached_quality_assessment(candidate, descriptor, keyword, metadata_cache)
        if quality["reject"]:
            rejected_reasons.append(f"{descriptor.provider_id}:{quality['reason']}")
            continue

        candidate.rank_hint = float(quality["score"])
        accepted.append(
            RankedCandidate(
                candidate=candidate,
                score=float(quality["score"]),
                reasons=[str(quality["reason"])],
            )
        )

    accepted.sort(key=lambda item: item.score, reverse=True)
    return [item.candidate for item in accepted], rejected_reasons


def cached_quality_assessment(
    candidate: SearchCandidate,
    descriptor: ProviderDescriptor,
    keyword: str,
    metadata_cache: Any | None = None,
) -> dict[str, object]:
    cache_key = f"{descriptor.provider_id}:{candidate.url}"
    cached = metadata_cache.get(cache_key) if metadata_cache is not None else None
    if isinstance(cached, dict):
        return cached

    assessment = assess_candidate_quality(candidate, descriptor, keyword)
    if metadata_cache is not None:
        metadata_cache.set(cache_key, assessment)
    return assessment


def assess_candidate_quality(
    candidate: SearchCandidate,
    descriptor: ProviderDescriptor,
    keyword: str,
) -> dict[str, object]:
    haystack = " ".join(
        part for part in [candidate.url, candidate.referrer_url or "", candidate.query_used] if part
    ).casefold()
    requested_non_photo = any(token in keyword.casefold() for token in ("logo", "interface", "ui", "illustration", "meme"))
    if not requested_non_photo and any(token in haystack for token in LOW_QUALITY_TOKENS):
        return {"reject": True, "reason": "low_quality_prefilter", "score": 0.0}

    score = candidate_hint_score(keyword, candidate)
    score += descriptor.priority / 100.0
    if descriptor.provider_group == "storyblocks_images":
        score += 0.35
    elif descriptor.provider_group == "free_stock_api":
        score += 0.2
    elif descriptor.provider_group == "open_license_repository":
        score += 0.05
    elif descriptor.provider_group == "generic_web_image":
        score -= 0.3

    if candidate.license_name == "unknown":
        score -= 0.2
    if not candidate.referrer_url:
        score -= 0.05
    if candidate.attribution_required:
        score -= 0.05

    return {"reject": False, "reason": "accepted", "score": round(score, 4)}


__all__ = [
    "ImageLicensePolicy",
    "RankedCandidate",
    "assess_candidate_quality",
    "cached_quality_assessment",
    "filter_and_rank_candidates",
]
