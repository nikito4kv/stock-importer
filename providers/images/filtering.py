from __future__ import annotations

from dataclasses import dataclass

from legacy_core.licenses import is_license_allowed

from ..base import ProviderDescriptor
from .clients import SearchCandidate


@dataclass(slots=True)
class ImageLicensePolicy:
    commercial_only: bool = True
    allow_attribution_licenses: bool = False


def filter_and_rank_candidates(
    descriptor: ProviderDescriptor,
    keyword: str,
    candidates: list[SearchCandidate],
    *,
    license_policy: ImageLicensePolicy,
) -> tuple[list[SearchCandidate], list[str]]:
    del keyword
    accepted: list[SearchCandidate] = []
    rejected_reasons: list[str] = []
    for index, candidate in enumerate(candidates):
        if not is_license_allowed(
            candidate,
            commercial_only=license_policy.commercial_only,
            allow_attribution_licenses=license_policy.allow_attribution_licenses,
        ):
            rejected_reasons.append(f"{descriptor.provider_id}:license")
            continue
        rank_hint = candidate.rank_hint
        try:
            candidate.rank_hint = float(rank_hint)
        except (TypeError, ValueError):
            candidate.rank_hint = float(max(1, len(candidates) - index))
        accepted.append(candidate)
    return accepted, rejected_reasons


__all__ = ["ImageLicensePolicy", "filter_and_rank_candidates"]
