from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import ProviderDescriptor
from ..registry import ProviderRegistry
from .caching import MetadataCache, SearchResultCache
from .clients import (
    ImageProviderBuildContext,
    ImageSearchProvider,
    SearchCandidate,
    WrappedImageSearchProvider,
    build_image_provider_clients,
    default_cache_root,
)
from .filtering import ImageLicensePolicy, filter_and_rank_candidates
from .querying import ImageQueryPlanner


@dataclass(slots=True)
class ImageSearchDiagnostics:
    provider_queries: dict[str, list[str]]
    rejected_prefilters: list[str]
    cache_hits: int
    provider_cache_hits: dict[str, int] = field(default_factory=dict)
    provider_rejected_prefilters: dict[str, list[str]] = field(default_factory=dict)


class ImageProviderSearchService:
    def __init__(
        self,
        registry: ProviderRegistry,
        cache_root: str | Path,
    ):
        cache_dir = default_cache_root(cache_root)
        self._registry = registry
        self._query_planner = ImageQueryPlanner()
        self._search_cache = SearchResultCache(cache_dir / "search_results.sqlite")
        self._metadata_cache = MetadataCache(cache_dir / "metadata.sqlite")

    def build_providers(
        self,
        provider_ids: list[str],
        context: ImageProviderBuildContext,
    ) -> list[WrappedImageSearchProvider]:
        return build_image_provider_clients(self._registry, provider_ids, context)

    def search_keyword(
        self,
        keyword: str,
        paragraph_text: str,
        providers: list[ImageSearchProvider],
        *,
        max_candidates_per_keyword: int,
        license_policy: ImageLicensePolicy,
    ) -> tuple[list[SearchCandidate], list[str], ImageSearchDiagnostics]:
        all_candidates: list[SearchCandidate] = []
        errors: list[str] = []
        rejected_prefilters: list[str] = []
        cache_hits = 0
        provider_queries: dict[str, list[str]] = {}
        provider_cache_hits: dict[str, int] = {}
        provider_rejected: dict[str, list[str]] = {}
        seen_urls: set[str] = set()

        if not providers:
            return [], ["No image providers are configured"], ImageSearchDiagnostics({}, [], 0)

        for provider in providers:
            provider_candidates, provider_errors, provider_diagnostics = self.search_provider(
                keyword,
                paragraph_text,
                provider,
                max_candidates_per_keyword=max_candidates_per_keyword,
                license_policy=license_policy,
            )
            provider_queries[provider.provider_id] = list(provider_diagnostics.provider_queries.get(provider.provider_id, []))
            errors.extend(provider_errors)
            rejected_prefilters.extend(provider_diagnostics.rejected_prefilters)
            cache_hits += provider_diagnostics.cache_hits
            provider_cache_hits.update(provider_diagnostics.provider_cache_hits)
            provider_rejected.update(provider_diagnostics.provider_rejected_prefilters)

            provider_candidates.sort(key=lambda item: item.rank_hint, reverse=True)
            unique_candidates: list[SearchCandidate] = []
            for candidate in provider_candidates:
                if candidate.url in seen_urls:
                    continue
                seen_urls.add(candidate.url)
                unique_candidates.append(candidate)
            all_candidates.extend(unique_candidates)

        all_candidates.sort(key=lambda item: item.rank_hint, reverse=True)
        return (
            all_candidates[:max_candidates_per_keyword],
            errors,
            ImageSearchDiagnostics(
                provider_queries=provider_queries,
                rejected_prefilters=rejected_prefilters,
                cache_hits=cache_hits,
                provider_cache_hits=provider_cache_hits,
                provider_rejected_prefilters=provider_rejected,
            ),
        )

    def search_provider(
        self,
        keyword: str,
        paragraph_text: str,
        provider: ImageSearchProvider,
        *,
        max_candidates_per_keyword: int,
        license_policy: ImageLicensePolicy,
    ) -> tuple[list[SearchCandidate], list[str], ImageSearchDiagnostics]:
        descriptor = provider.descriptor
        provider_limit = max(8, min(80, max_candidates_per_keyword or 8))
        query_plan = self._query_planner.rewrite_for_provider(descriptor, keyword, paragraph_text)
        provider_candidates: list[SearchCandidate] = []
        errors: list[str] = []
        rejected_prefilters: list[str] = []
        cache_hits = 0

        for query in query_plan.queries:
            cached = self._search_cache.get(provider.provider_id, query, provider_limit)
            if cached is not None:
                found = cached
                cache_hits += 1
            else:
                try:
                    found = provider.search(query, provider_limit)
                except Exception as exc:
                    errors.append(f"{provider.provider_id} search failed for '{query}': {exc}")
                    continue
                self._search_cache.set(provider.provider_id, query, provider_limit, found)

            filtered, rejected = filter_and_rank_candidates(
                descriptor,
                keyword,
                list(found),
                license_policy=license_policy,
                metadata_cache=self._metadata_cache,
            )
            rejected_prefilters.extend(rejected)
            provider_candidates.extend(filtered)
            if len(provider_candidates) >= provider_limit:
                break

        provider_candidates.sort(key=lambda item: item.rank_hint, reverse=True)
        provider_candidates = provider_candidates[:provider_limit]
        return (
            provider_candidates,
            errors,
            ImageSearchDiagnostics(
                provider_queries={provider.provider_id: list(query_plan.queries)},
                rejected_prefilters=rejected_prefilters,
                cache_hits=cache_hits,
                provider_cache_hits={provider.provider_id: cache_hits},
                provider_rejected_prefilters={provider.provider_id: list(rejected_prefilters)},
            ),
        )

    def close(self) -> None:
        self._search_cache.close()
        self._metadata_cache.close()


__all__ = ["ImageProviderSearchService", "ImageSearchDiagnostics"]
