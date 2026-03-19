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
        suffixes = self._suffixes_for(descriptor)
        variants = shared_build_query_variants(normalized, paragraph_text, short_suffixes=suffixes)
        if descriptor.provider_group == "storyblocks_images":
            variants = [normalized, f"{normalized} cinematic"] + variants
        elif descriptor.provider_group == "free_stock_api":
            variants = [normalized, f"{normalized} photo"] + variants
        elif descriptor.provider_group == "open_license_repository":
            variants = [normalized, f"{normalized} photograph"] + variants
        elif descriptor.provider_group == "generic_web_image":
            variants = [f"{normalized} photo", f"{normalized} realistic"] + variants

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

    def _suffixes_for(self, descriptor: ProviderDescriptor) -> tuple[str, ...]:
        if descriptor.provider_group == "storyblocks_images":
            return ("cinematic", "stock")
        if descriptor.provider_group == "free_stock_api":
            return ("photo", "realistic")
        if descriptor.provider_group == "open_license_repository":
            return ("photo", "documentary")
        if descriptor.provider_group == "generic_web_image":
            return ("photo", "reference")
        return ("photo",)


__all__ = ["ImageQueryPlanner", "ProviderQueryPlan"]
