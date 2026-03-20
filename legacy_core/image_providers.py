from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .licenses import normalize_license_info
from .network import HttpClientConfig, SafeHttpClient, http_get_json


@dataclass(slots=True)
class SearchCandidate:
    source: str
    url: str
    referrer_url: str | None
    query_used: str
    license_name: str
    license_url: str | None
    author: str | None
    commercial_allowed: bool
    attribution_required: bool
    rank_hint: float = 0.0


class PexelsProvider:
    name = "pexels"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
        user_agent: str,
        *,
        http_client: SafeHttpClient | None = None,
    ):
        self._api_key = api_key.strip()
        self._timeout = timeout_seconds
        self._user_agent = user_agent
        self._http_client = http_client or SafeHttpClient(
            HttpClientConfig(user_agent=user_agent)
        )
        self._owns_http_client = http_client is None
        if not self._api_key:
            raise ValueError("Missing PEXELS_API_KEY")

    @property
    def http_client(self) -> SafeHttpClient:
        return self._http_client

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]:
        per_page = max(5, min(80, int(limit)))
        effective_timeout = (
            self._timeout
            if timeout_seconds is None or timeout_seconds <= 0
            else timeout_seconds
        )
        payload = http_get_json(
            "https://api.pexels.com/v1/search",
            params={
                "query": query,
                "per_page": per_page,
                "page": 1,
                "locale": "en-US",
            },
            headers={"Authorization": self._api_key},
            timeout_seconds=effective_timeout,
            user_agent=self._user_agent,
            provider_id=self.name,
            http_client=self._http_client,
        )

        photos = payload.get("photos")
        if not isinstance(photos, list):
            return []

        results: list[SearchCandidate] = []
        for item in photos:
            if not isinstance(item, dict):
                continue
            src = item.get("src")
            if not isinstance(src, dict):
                continue
            url = str(src.get("large2x") or src.get("original") or "").strip()
            if not url:
                continue

            license_name, commercial_allowed, attribution_required = normalize_license_info(
                source=self.name,
                raw_license="pexels-license",
                raw_license_url="https://www.pexels.com/license/",
            )

            results.append(
                SearchCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=str(item.get("url") or "").strip() or None,
                    query_used=query,
                    license_name=license_name,
                    license_url="https://www.pexels.com/license/",
                    author=str(item.get("photographer") or "").strip() or None,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                )
            )
        return results


class PixabayProvider:
    name = "pixabay"

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float,
        user_agent: str,
        *,
        http_client: SafeHttpClient | None = None,
    ):
        self._api_key = api_key.strip()
        self._timeout = timeout_seconds
        self._user_agent = user_agent
        self._http_client = http_client or SafeHttpClient(
            HttpClientConfig(user_agent=user_agent)
        )
        self._owns_http_client = http_client is None
        if not self._api_key:
            raise ValueError("Missing PIXABAY_API_KEY")

    @property
    def http_client(self) -> SafeHttpClient:
        return self._http_client

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]:
        per_page = max(10, min(200, int(limit)))
        effective_timeout = (
            self._timeout
            if timeout_seconds is None or timeout_seconds <= 0
            else timeout_seconds
        )
        payload = http_get_json(
            "https://pixabay.com/api/",
            params={
                "key": self._api_key,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": per_page,
                "page": 1,
            },
            timeout_seconds=effective_timeout,
            user_agent=self._user_agent,
            provider_id=self.name,
            http_client=self._http_client,
        )

        hits = payload.get("hits")
        if not isinstance(hits, list):
            return []

        results: list[SearchCandidate] = []
        for item in hits:
            if not isinstance(item, dict):
                continue
            url = str(
                item.get("largeImageURL")
                or item.get("webformatURL")
                or item.get("previewURL")
                or ""
            ).strip()
            if not url:
                continue

            license_name, commercial_allowed, attribution_required = normalize_license_info(
                source=self.name,
                raw_license="pixabay-license",
                raw_license_url="https://pixabay.com/service/license-summary/",
            )

            results.append(
                SearchCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=str(item.get("pageURL") or "").strip() or None,
                    query_used=query,
                    license_name=license_name,
                    license_url="https://pixabay.com/service/license-summary/",
                    author=str(item.get("user") or "").strip() or None,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                )
            )
        return results


class OpenverseProvider:
    name = "openverse"

    def __init__(
        self,
        timeout_seconds: float,
        user_agent: str,
        *,
        http_client: SafeHttpClient | None = None,
    ):
        self._timeout = timeout_seconds
        self._user_agent = user_agent
        self._http_client = http_client or SafeHttpClient(
            HttpClientConfig(user_agent=user_agent)
        )
        self._owns_http_client = http_client is None

    @property
    def http_client(self) -> SafeHttpClient:
        return self._http_client

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]:
        page_size = max(10, min(80, int(limit)))
        effective_timeout = (
            self._timeout
            if timeout_seconds is None or timeout_seconds <= 0
            else timeout_seconds
        )
        payload = http_get_json(
            "https://api.openverse.org/v1/images/",
            params={
                "q": query,
                "page_size": page_size,
                "mature": "false",
                "extension": ["jpg", "jpeg", "png", "webp"],
            },
            timeout_seconds=effective_timeout,
            user_agent=self._user_agent,
            provider_id=self.name,
            http_client=self._http_client,
        )

        results_raw = payload.get("results")
        if not isinstance(results_raw, list):
            return []

        results: list[SearchCandidate] = []
        for item in results_raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue

            raw_license = str(item.get("license") or "").strip()
            raw_license_url = str(item.get("license_url") or "").strip() or None
            license_name, commercial_allowed, attribution_required = normalize_license_info(
                source=self.name,
                raw_license=raw_license,
                raw_license_url=raw_license_url,
            )

            results.append(
                SearchCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=str(item.get("foreign_landing_url") or "").strip()
                    or None,
                    query_used=query,
                    license_name=license_name,
                    license_url=raw_license_url,
                    author=str(item.get("creator") or "").strip() or None,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                )
            )
        return results
def build_image_providers(
    *,
    source_names: list[str],
    timeout_seconds: float,
    adult_filter_off: bool,
    user_agent: str,
    pexels_api_key: str,
    pixabay_api_key: str,
    logger: Any | None = None,
) -> list[Any]:
    providers: list[Any] = []
    for source in source_names:
        try:
            if source == "pexels":
                if not pexels_api_key.strip():
                    if logger is not None:
                        logger.warning("Pexels skipped: PEXELS_API_KEY is not set")
                    continue
                providers.append(
                    PexelsProvider(
                        pexels_api_key,
                        timeout_seconds=timeout_seconds,
                        user_agent=user_agent,
                    )
                )
            elif source == "pixabay":
                if not pixabay_api_key.strip():
                    if logger is not None:
                        logger.warning("Pixabay skipped: PIXABAY_API_KEY is not set")
                    continue
                providers.append(
                    PixabayProvider(
                        pixabay_api_key,
                        timeout_seconds=timeout_seconds,
                        user_agent=user_agent,
                    )
                )
            elif source == "openverse":
                providers.append(
                    OpenverseProvider(timeout_seconds=timeout_seconds, user_agent=user_agent)
                )
            else:
                if logger is not None:
                    logger.warning("Unknown source '%s' skipped", source)
        except Exception as exc:
            if logger is not None:
                logger.warning("Source '%s' unavailable: %s", source, exc)

    if not providers:
        raise RuntimeError("No sources are available. Check API keys and dependencies.")

    if logger is not None:
        logger.info(
            "Sources enabled: %s",
            ", ".join(getattr(provider, "name", "unknown") for provider in providers),
        )
    return providers
