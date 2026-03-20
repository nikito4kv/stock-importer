from __future__ import annotations

from dataclasses import dataclass

from legacy_core.common import normalize_whitespace
from legacy_core.query_utils import build_query_variants as shared_build_query_variants

from ..base import ProviderDescriptor


@dataclass(slots=True)
class ProviderQueryPlan:
    provider_id: str
    queries: list[str]


class ImageQueryPlanner:
    def normalize(self, query: str) -> str:
        return normalize_whitespace(query)

    def rewrite_for_provider(
        self,
        descriptor: ProviderDescriptor,
        query: str,
        paragraph_text: str,
    ) -> ProviderQueryPlan:
        normalized = self.normalize(query)
        suffixes = self._suffixes_for(descriptor.provider_id)
        variants = shared_build_query_variants(
            normalized,
            paragraph_text,
            short_suffixes=suffixes,
        )
        if descriptor.provider_id == "storyblocks_image":
            variants = [normalized, f"{normalized} cinematic"] + variants
        else:
            variants = [normalized, f"{normalized} photo"] + variants

        unique: list[str] = []
        seen: set[str] = set()
        for item in variants:
            current = normalize_whitespace(item)
            if not current:
                continue
            key = current.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(current)
        return ProviderQueryPlan(provider_id=descriptor.provider_id, queries=unique)

    def _suffixes_for(self, provider_id: str) -> tuple[str, ...]:
        if provider_id == "storyblocks_image":
            return ("cinematic", "stock")
        return ("photo", "realistic")


__all__ = ["ImageQueryPlanner", "ProviderQueryPlan"]
