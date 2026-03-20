from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from legacy_core.image_providers import (
    BingProvider as LegacyBingProvider,
)
from legacy_core.image_providers import (
    OpenverseProvider as LegacyOpenverseProvider,
)
from legacy_core.image_providers import (
    PexelsProvider as LegacyPexelsProvider,
)
from legacy_core.image_providers import (
    PixabayProvider as LegacyPixabayProvider,
)
from legacy_core.image_providers import (
    SearchCandidate,
)
from legacy_core.image_providers import (
    WikimediaProvider as LegacyWikimediaProvider,
)

from ..base import ProviderDescriptor
from ..registry import ProviderRegistry


class ImageSearchProvider(Protocol):
    provider_id: str
    descriptor: ProviderDescriptor

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]: ...


@dataclass(slots=True)
class ImageProviderBuildContext:
    timeout_seconds: float
    user_agent: str
    adult_filter_off: bool = False
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    logger: Any | None = None
    allow_generic_web_image: bool = False
    free_images_only: bool = False


@dataclass(slots=True)
class WrappedImageSearchProvider:
    provider_id: str
    descriptor: ProviderDescriptor
    client: Any

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]:
        return list(
            self.client.search(query, limit, timeout_seconds=timeout_seconds)
        )

    @property
    def http_client(self) -> Any | None:
        return getattr(self.client, "http_client", None)

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def build_image_provider_clients(
    registry: ProviderRegistry,
    provider_ids: list[str],
    context: ImageProviderBuildContext,
) -> list[WrappedImageSearchProvider]:
    providers: list[WrappedImageSearchProvider] = []
    for provider_id in provider_ids:
        descriptor = registry.get(provider_id)
        if descriptor is None:
            if context.logger is not None:
                context.logger.warning("Unknown image provider '%s' skipped", provider_id)
            continue
        if descriptor.provider_group == "storyblocks_images" and context.free_images_only:
            continue
        if descriptor.provider_group == "generic_web_image" and not context.allow_generic_web_image:
            continue
        client = _build_single_provider(descriptor, context)
        if client is not None:
            providers.append(WrappedImageSearchProvider(provider_id=provider_id, descriptor=descriptor, client=client))

    providers.sort(key=lambda item: (-item.descriptor.priority, item.provider_id))
    return providers


def _build_single_provider(
    descriptor: ProviderDescriptor,
    context: ImageProviderBuildContext,
) -> Any | None:
    try:
        if descriptor.provider_id == "pexels":
            if not context.pexels_api_key.strip():
                if context.logger is not None:
                    context.logger.warning("Pexels skipped: PEXELS_API_KEY is not set")
                return None
            return LegacyPexelsProvider(
                context.pexels_api_key,
                timeout_seconds=context.timeout_seconds,
                user_agent=context.user_agent,
            )
        if descriptor.provider_id == "pixabay":
            if not context.pixabay_api_key.strip():
                if context.logger is not None:
                    context.logger.warning("Pixabay skipped: PIXABAY_API_KEY is not set")
                return None
            return LegacyPixabayProvider(
                context.pixabay_api_key,
                timeout_seconds=context.timeout_seconds,
                user_agent=context.user_agent,
            )
        if descriptor.provider_id == "openverse":
            return LegacyOpenverseProvider(
                timeout_seconds=context.timeout_seconds,
                user_agent=context.user_agent,
            )
        if descriptor.provider_id == "wikimedia":
            return LegacyWikimediaProvider(
                timeout_seconds=context.timeout_seconds,
                user_agent=context.user_agent,
            )
        if descriptor.provider_id == "bing":
            return LegacyBingProvider(adult_filter_off=context.adult_filter_off)
    except Exception as exc:
        if context.logger is not None:
            context.logger.warning("Image provider '%s' unavailable: %s", descriptor.provider_id, exc)
        return None

    if context.logger is not None:
        context.logger.warning("Image provider '%s' is not supported by the legacy free-image pipeline", descriptor.provider_id)
    return None


def default_cache_root(base_dir: str | Path) -> Path:
    return Path(base_dir) / "provider_cache"


__all__ = [
    "ImageProviderBuildContext",
    "ImageSearchProvider",
    "SearchCandidate",
    "WrappedImageSearchProvider",
    "build_image_provider_clients",
    "default_cache_root",
]
