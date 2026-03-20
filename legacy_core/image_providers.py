from __future__ import annotations

import asyncio
import importlib
import sys
import types
from dataclasses import dataclass
from typing import Any

from .licenses import normalize_license_info
from .network import HttpClientConfig, SafeHttpClient, http_get_json


def _strip_html(value: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", value or "").strip()


def _install_imghdr_compat() -> None:
    if "imghdr" in sys.modules:
        return

    module = types.ModuleType("imghdr")

    def what(file: Any = None, h: bytes | None = None) -> str | None:
        data = h
        if data is None and file is not None:
            try:
                reader = getattr(file, "read", None)
                if callable(reader):
                    teller = getattr(file, "tell", None)
                    seeker = getattr(file, "seek", None)
                    cursor = teller() if callable(teller) else None
                    data = reader(64)
                    if cursor is not None and callable(seeker):
                        seeker(cursor)
                else:
                    with open(str(file), "rb") as fp:
                        data = fp.read(64)
            except OSError:
                return None

        if isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes) or not data:
            return None

        if data.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if data[:6] in {b"GIF87a", b"GIF89a"}:
            return "gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "webp"
        if data.startswith(b"BM"):
            return "bmp"
        if data[:4] in {b"II*\x00", b"MM\x00*"}:
            return "tiff"
        return None

    module.what = what
    sys.modules["imghdr"] = module


def _load_bing_search_func() -> tuple[Any | None, Exception | None]:
    try:
        module = importlib.import_module("bing_image_urls")
        func = getattr(module, "bing_image_urls", None)
        if callable(func):
            return func, None
        return None, RuntimeError("bing_image_urls.bing_image_urls is not callable")
    except ModuleNotFoundError as exc:
        if exc.name == "imghdr":
            _install_imghdr_compat()
            try:
                module = importlib.import_module("bing_image_urls")
                func = getattr(module, "bing_image_urls", None)
                if callable(func):
                    return func, None
                return None, RuntimeError("bing_image_urls.bing_image_urls is not callable")
            except Exception as inner_exc:
                return None, inner_exc
        return None, exc
    except Exception as exc:
        return None, exc


BING_SEARCH_FUNC, BING_IMPORT_ERROR = _load_bing_search_func()


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


class WikimediaProvider:
    name = "wikimedia"

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
        amount = max(10, min(50, int(limit)))
        effective_timeout = (
            self._timeout
            if timeout_seconds is None or timeout_seconds <= 0
            else timeout_seconds
        )
        payload = http_get_json(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": query,
                "gsrnamespace": 6,
                "gsrlimit": amount,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
            },
            timeout_seconds=effective_timeout,
            user_agent=self._user_agent,
            provider_id=self.name,
            http_client=self._http_client,
        )

        query_data = payload.get("query")
        if not isinstance(query_data, dict):
            return []
        pages = query_data.get("pages")
        if not isinstance(pages, dict):
            return []

        results: list[SearchCandidate] = []
        for page in pages.values():
            if not isinstance(page, dict):
                continue
            imageinfo = page.get("imageinfo")
            if not isinstance(imageinfo, list) or not imageinfo:
                continue
            info = imageinfo[0]
            if not isinstance(info, dict):
                continue

            url = str(info.get("url") or "").strip()
            if not url:
                continue

            metadata = info.get("extmetadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata_map: dict[str, Any] = metadata

            def meta_value(key: str, _metadata_map: dict[str, Any] = metadata_map) -> str:
                raw = _metadata_map.get(key)
                if isinstance(raw, dict):
                    return _strip_html(str(raw.get("value") or ""))
                return ""

            raw_license = meta_value("LicenseShortName") or meta_value("UsageTerms")
            raw_license_url = meta_value("LicenseUrl") or None
            license_name, commercial_allowed, attribution_required = normalize_license_info(
                source=self.name,
                raw_license=raw_license,
                raw_license_url=raw_license_url,
            )

            results.append(
                SearchCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=str(info.get("descriptionurl") or "").strip() or None,
                    query_used=query,
                    license_name=license_name,
                    license_url=raw_license_url,
                    author=meta_value("Artist") or meta_value("Credit") or None,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                )
            )
        return results


class BingProvider:
    name = "bing"

    def __init__(self, adult_filter_off: bool = False):
        if not callable(BING_SEARCH_FUNC):
            raise RuntimeError(
                "bing-image-urls is not available. Install it with: pip install bing-image-urls"
            ) from BING_IMPORT_ERROR
        self._search_func = BING_SEARCH_FUNC
        self._adult_filter_off = bool(adult_filter_off)

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]:
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        amount = max(10, min(200, int(limit)))
        raw_urls = self._search_func(
            query,
            limit=amount,
            adult_filter_off=self._adult_filter_off,
            verify_status_only=True,
        )

        if not isinstance(raw_urls, (list, tuple, set)):
            return []

        results: list[SearchCandidate] = []
        for raw_url in raw_urls:
            url = str(raw_url).strip()
            if not url:
                continue
            results.append(
                SearchCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=None,
                    query_used=query,
                    license_name="unknown",
                    license_url=None,
                    author=None,
                    commercial_allowed=False,
                    attribution_required=True,
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
            elif source == "wikimedia":
                providers.append(
                    WikimediaProvider(timeout_seconds=timeout_seconds, user_agent=user_agent)
                )
            elif source == "bing":
                providers.append(BingProvider(adult_filter_off=adult_filter_off))
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
