from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path

from services.errors import ProviderSearchError
from services.retry import is_timeout_exception

from ..registry import ProviderRegistry
from .caching import SearchResultCache
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


def _enrich_provider_search_error(
    exc: ProviderSearchError,
    *,
    provider_id: str,
    provider_queries: list[str],
    failed_query: str,
) -> ProviderSearchError:
    details = dict(exc.details)
    details.setdefault("provider_id", provider_id)
    details.setdefault("provider_queries", list(provider_queries))
    details.setdefault("failed_query", failed_query)
    return ProviderSearchError(
        code=exc.code,
        message=str(exc),
        provider_id=provider_id,
        details=details,
        retryable=exc.retryable,
        fatal=exc.fatal,
    )


def _wrap_provider_search_exception(
    exc: Exception,
    *,
    provider_id: str,
    provider_queries: list[str],
    failed_query: str,
) -> ProviderSearchError:
    if isinstance(exc, ProviderSearchError):
        return _enrich_provider_search_error(
            exc,
            provider_id=provider_id,
            provider_queries=provider_queries,
            failed_query=failed_query,
        )

    retryable = is_timeout_exception(exc)
    code = "provider_search_timeout" if retryable else "provider_search_failed"
    return ProviderSearchError(
        code=code,
        message=f"{provider_id} search failed for '{failed_query}': {exc}",
        provider_id=provider_id,
        retryable=retryable,
        details={
            "provider_queries": list(provider_queries),
            "failed_query": failed_query,
        },
    )


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
        self._cache_dir = cache_dir
        self._query_planner = ImageQueryPlanner()
        self._search_cache_instance: SearchResultCache | None = None
        self._closed = False

    @property
    def _search_cache(self) -> SearchResultCache:
        cache = self._search_cache_instance
        if cache is not None:
            return cache
        if self._closed:
            raise RuntimeError("ImageProviderSearchService is closed")
        cache = SearchResultCache(self._cache_dir / "search_results.sqlite")
        self._search_cache_instance = cache
        return cache

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("ImageProviderSearchService is closed")

    def _provider_supports_timeout_seconds(
        self,
        provider: ImageSearchProvider,
    ) -> bool:
        try:
            parameters = inspect.signature(provider.search).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == "timeout_seconds"
            for parameter in parameters
        )

    def _search_with_compatible_signature(
        self,
        provider: ImageSearchProvider,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None,
    ) -> list[SearchCandidate]:
        if self._provider_supports_timeout_seconds(provider):
            return provider.search(
                query,
                limit,
                timeout_seconds=timeout_seconds,
            )
        return provider.search(query, limit)

    def build_providers(
        self,
        provider_ids: list[str],
        context: ImageProviderBuildContext,
    ) -> list[WrappedImageSearchProvider]:
        self._require_open()
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
        self._require_open()
        all_candidates: list[SearchCandidate] = []
        errors: list[str] = []
        rejected_prefilters: list[str] = []
        cache_hits = 0
        provider_queries: dict[str, list[str]] = {}
        provider_cache_hits: dict[str, int] = {}
        provider_rejected: dict[str, list[str]] = {}
        seen_urls: set[str] = set()

        if not providers:
            return (
                [],
                ["No image providers are configured"],
                ImageSearchDiagnostics({}, [], 0),
            )

        for provider in providers:
            try:
                provider_candidates, provider_errors, provider_diagnostics = (
                    self.search_provider(
                        keyword,
                        paragraph_text,
                        provider,
                        max_candidates_per_keyword=max_candidates_per_keyword,
                        license_policy=license_policy,
                    )
                )
            except ProviderSearchError as exc:
                provider_queries[provider.provider_id] = list(
                    exc.details.get("provider_queries", [])
                )
                provider_cache_hits[provider.provider_id] = 0
                provider_rejected[provider.provider_id] = []
                errors.append(str(exc))
                continue
            provider_queries[provider.provider_id] = list(
                provider_diagnostics.provider_queries.get(provider.provider_id, [])
            )
            errors.extend(provider_errors)
            rejected_prefilters.extend(provider_diagnostics.rejected_prefilters)
            cache_hits += provider_diagnostics.cache_hits
            provider_cache_hits.update(provider_diagnostics.provider_cache_hits)
            provider_rejected.update(provider_diagnostics.provider_rejected_prefilters)

            unique_candidates: list[SearchCandidate] = []
            for candidate in provider_candidates:
                if candidate.url in seen_urls:
                    continue
                seen_urls.add(candidate.url)
                unique_candidates.append(candidate)
            all_candidates.extend(unique_candidates)
            if len(all_candidates) >= max_candidates_per_keyword:
                break

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
        timeout_seconds: float | None = None,
    ) -> tuple[list[SearchCandidate], list[str], ImageSearchDiagnostics]:
        self._require_open()
        descriptor = provider.descriptor
        provider_limit = max(1, int(max_candidates_per_keyword or 8))
        query_plan = self._query_planner.rewrite_for_provider(
            descriptor, keyword, paragraph_text
        )
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
                    found = self._search_with_compatible_signature(
                        provider,
                        query,
                        provider_limit,
                        timeout_seconds=timeout_seconds,
                    )
                except Exception as exc:
                    raise _wrap_provider_search_exception(
                        exc,
                        provider_id=provider.provider_id,
                        provider_queries=list(query_plan.queries),
                        failed_query=query,
                    ) from exc
                self._search_cache.set(
                    provider.provider_id, query, provider_limit, found
                )

            filtered, rejected = filter_and_rank_candidates(
                descriptor,
                keyword,
                list(found),
                license_policy=license_policy,
            )
            rejected_prefilters.extend(rejected)
            provider_candidates.extend(filtered)
            if len(provider_candidates) >= provider_limit:
                break

        provider_candidates = provider_candidates[:provider_limit]
        return (
            provider_candidates,
            errors,
            ImageSearchDiagnostics(
                provider_queries={provider.provider_id: list(query_plan.queries)},
                rejected_prefilters=rejected_prefilters,
                cache_hits=cache_hits,
                provider_cache_hits={provider.provider_id: cache_hits},
                provider_rejected_prefilters={
                    provider.provider_id: list(rejected_prefilters)
                },
            ),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._search_cache_instance is not None:
            self._search_cache_instance.close()


__all__ = ["ImageProviderSearchService", "ImageSearchDiagnostics"]
