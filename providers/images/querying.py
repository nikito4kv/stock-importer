from __future__ import annotations

from dataclasses import dataclass

from legacy_core.common import normalize_whitespace

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
        del paragraph_text
        normalized = self.normalize(query)
        queries = [normalized] if normalized else []
        return ProviderQueryPlan(provider_id=descriptor.provider_id, queries=queries)


__all__ = ["ImageQueryPlanner", "ProviderQueryPlan"]
