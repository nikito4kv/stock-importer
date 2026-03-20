import argparse
import hashlib
import importlib
import io
import json
import logging
import os
import random
import re
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from legacy_core.common import normalize_keywords as shared_normalize_keywords
from legacy_core.common import safe_int as shared_safe_int
from legacy_core.common import slugify as shared_slugify
from legacy_core.env import get_env_path as shared_get_env_path
from legacy_core.env import load_dotenv as shared_load_dotenv
from legacy_core.files import build_run_dir as shared_build_run_dir
from legacy_core.files import (
    resolve_output_json_path as shared_resolve_output_json_path,
)
from legacy_core.image_providers import BingProvider as SharedBingProvider
from legacy_core.image_providers import OpenverseProvider as SharedOpenverseProvider
from legacy_core.image_providers import PexelsProvider as SharedPexelsProvider
from legacy_core.image_providers import PixabayProvider as SharedPixabayProvider
from legacy_core.image_providers import SearchCandidate as SharedSearchCandidate
from legacy_core.image_providers import WikimediaProvider as SharedWikimediaProvider
from legacy_core.keyword_payload import (
    extract_paragraph_tasks as shared_extract_paragraph_tasks,
)
from legacy_core.keyword_payload import (
    load_keywords_payload as shared_load_keywords_payload,
)
from legacy_core.licenses import is_license_allowed as shared_is_license_allowed
from legacy_core.licenses import normalize_license_info as shared_normalize_license_info
from legacy_core.network import http_get_json as shared_http_get_json
from legacy_core.network import is_public_host as shared_is_public_host
from legacy_core.network import (
    open_with_safe_redirects as shared_open_with_safe_redirects,
)
from legacy_core.network import read_limited as shared_read_limited
from legacy_core.network import validate_public_url as shared_validate_public_url
from legacy_core.query_utils import build_query_variants as shared_build_query_variants
from legacy_core.query_utils import candidate_hint_score as shared_candidate_hint_score
from legacy_core.query_utils import parse_sources as shared_parse_sources
from legacy_core.query_utils import tokenize as shared_tokenize
from legacy_core.relevance import ImageRelevanceCache as SharedImageRelevanceCache
from legacy_core.relevance import SimpleRateLimiter as SharedSimpleRateLimiter
from legacy_core.relevance import clean_model_text as shared_clean_model_text
from legacy_core.relevance import (
    parse_relevance_response as shared_parse_relevance_response,
)
from legacy_core.retry import retry_call
from providers import (
    ImageLicensePolicy,
    ImageProviderBuildContext,
    ImageProviderSearchService,
    build_default_provider_registry,
)
from services.genai_client import (
    create_gemini_model,
    ensure_gemini_sdk_available,
    get_transient_exceptions,
)

try:
    from PIL import Image, UnidentifiedImageError
except Exception as exc:
    Image = None
    UnidentifiedImageError = Exception
    PIL_IMPORT_ERROR = exc
else:
    PIL_IMPORT_ERROR = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


DEFAULT_KEYWORDS_JSON = "output/paragraph_intents.json"
DEFAULT_OUTPUT_JSON = "output/keyword_images.json"
DEFAULT_IMAGES_DIR = "output/images"
DEFAULT_RUN_PREFIX = "run"
DEFAULT_SOURCES = "pexels,pixabay,openverse,wikimedia"

DEFAULT_IMAGES_PER_KEYWORD = 2
DEFAULT_MAX_CANDIDATES_PER_KEYWORD = 90
DEFAULT_DELAY_SECONDS = 0.0
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MIN_WIDTH = 320
DEFAULT_MIN_HEIGHT = 180
DEFAULT_ADULT_FILTER_OFF = False

DEFAULT_RELEVANCE_MODEL = "gemini-2.5-flash"
DEFAULT_RELEVANCE_THRESHOLD = 0.82
DEFAULT_KEYWORD_WORKERS = 3
DEFAULT_DOWNLOAD_WORKERS = 10
DEFAULT_RELEVANCE_WORKERS = 4
DEFAULT_MAX_GEMINI_RPS = 2.0
DEFAULT_BATCH_SIZE = 12
DEFAULT_RELEVANCE_CACHE = "output/cache/relevance_cache.sqlite"
DEFAULT_PROVIDER_SEARCH_CACHE_ROOT = "output/cache"
DEFAULT_TOP_K_TO_RELEVANCE = 24

DEFAULT_COMMERCIAL_ONLY = True
DEFAULT_ALLOW_ATTRIBUTION_LICENSES = False

MAX_IMAGE_PIXELS = 40_000_000
MAX_RELEVANCE_QUEUE_FACTOR = 2
REDIRECT_CODES = {301, 302, 303, 307, 308}

ENV_FILENAME = ".env"
ENV_GEMINI_API_KEY = "GEMINI_API_KEY"
ENV_PEXELS_API_KEY = "PEXELS_API_KEY"
ENV_PIXABAY_API_KEY = "PIXABAY_API_KEY"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


SearchCandidate = SharedSearchCandidate
TRANSIENT_EXCEPTIONS = get_transient_exceptions()
_GEMINI_API_KEY = ""
_IMAGE_PROVIDER_SEARCH_SERVICE: ImageProviderSearchService | None = None


@dataclass(slots=True)
class ParagraphTask:
    paragraph_no: int
    original_index: int | None
    text: str
    keywords: list[str]


@dataclass(slots=True)
class RelevanceResult:
    is_relevant: bool
    is_match: bool
    score: float
    reason: str
    from_cache: bool


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
                return (
                    None,
                    RuntimeError("bing_image_urls.bing_image_urls is not callable"),
                )
            except Exception as inner_exc:
                return None, inner_exc
        return None, exc
    except Exception as exc:
        return None, exc


BING_SEARCH_FUNC, BING_IMPORT_ERROR = _load_bing_search_func()


def get_env_path() -> Path:
    return shared_get_env_path(__file__, env_filename=ENV_FILENAME)


def load_dotenv(dotenv_path: Path | None = None) -> None:
    shared_load_dotenv(dotenv_path, anchor_file=__file__, env_filename=ENV_FILENAME)


def _ensure_runtime_dependencies() -> None:
    if PIL_IMPORT_ERROR is not None or Image is None:
        raise RuntimeError(
            "Pillow is required for image validation. Install it with: pip install pillow"
        )
    if not callable(BING_SEARCH_FUNC):
        raise RuntimeError(
            "bing-image-urls is required. Install it with: pip install bing-image-urls"
        ) from BING_IMPORT_ERROR
    ensure_gemini_sdk_available()


def _slugify(value: str, max_len: int = 40) -> str:
    return shared_slugify(value, max_len=max_len, default="item")


def _safe_int(value: object, default: int | None = None) -> int | None:
    return shared_safe_int(value, default)


def _normalize_keywords(raw_keywords: object) -> list[str]:
    return shared_normalize_keywords(raw_keywords)


def _extract_paragraph_tasks(
    payload: dict[str, object],
    max_paragraphs: int | None = None,
) -> list[ParagraphTask]:
    return [
        ParagraphTask(
            paragraph_no=task.paragraph_no,
            original_index=task.original_index,
            text=task.text,
            keywords=list(task.keywords),
        )
        for task in shared_extract_paragraph_tasks(
            payload,
            max_paragraphs=max_paragraphs,
            query_kind="image",
        )
    ]


def _http_get_json(
    url: str,
    params: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    return shared_http_get_json(
        url,
        params=params,
        headers=headers,
        timeout_seconds=timeout_seconds,
        user_agent=USER_AGENT,
    )


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def _normalize_license_info(
    source: str,
    raw_license: str,
    raw_license_url: str | None,
) -> tuple[str, bool, bool]:
    return shared_normalize_license_info(source, raw_license, raw_license_url)


def _is_license_allowed(
    candidate: SearchCandidate,
    commercial_only: bool,
    allow_attribution_licenses: bool,
) -> bool:
    return shared_is_license_allowed(
        candidate,
        commercial_only=commercial_only,
        allow_attribution_licenses=allow_attribution_licenses,
    )


class PexelsProvider(SharedPexelsProvider):
    def __init__(self, api_key: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(api_key, timeout_seconds=timeout_seconds, user_agent=USER_AGENT)


class PixabayProvider(SharedPixabayProvider):
    def __init__(self, api_key: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(api_key, timeout_seconds=timeout_seconds, user_agent=USER_AGENT)


class OpenverseProvider(SharedOpenverseProvider):
    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(timeout_seconds=timeout_seconds, user_agent=USER_AGENT)


class WikimediaProvider(SharedWikimediaProvider):
    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        super().__init__(timeout_seconds=timeout_seconds, user_agent=USER_AGENT)


class BingProvider(SharedBingProvider):
    pass


def _parse_sources(value: str) -> list[str]:
    return shared_parse_sources(value, default_sources=["openverse", "wikimedia"])


def _configure_image_provider_search_service(cache_root: str | Path) -> ImageProviderSearchService:
    global _IMAGE_PROVIDER_SEARCH_SERVICE
    if _IMAGE_PROVIDER_SEARCH_SERVICE is not None:
        _IMAGE_PROVIDER_SEARCH_SERVICE.close()
    _IMAGE_PROVIDER_SEARCH_SERVICE = ImageProviderSearchService(
        build_default_provider_registry(),
        cache_root,
    )
    return _IMAGE_PROVIDER_SEARCH_SERVICE


def _close_image_provider_search_service() -> None:
    global _IMAGE_PROVIDER_SEARCH_SERVICE
    if _IMAGE_PROVIDER_SEARCH_SERVICE is not None:
        _IMAGE_PROVIDER_SEARCH_SERVICE.close()
        _IMAGE_PROVIDER_SEARCH_SERVICE = None


def _build_providers(
    source_names: list[str],
    timeout_seconds: float,
    adult_filter_off: bool,
) -> list[Any]:
    service = _IMAGE_PROVIDER_SEARCH_SERVICE or _configure_image_provider_search_service(
        DEFAULT_PROVIDER_SEARCH_CACHE_ROOT
    )
    providers = service.build_providers(
        source_names,
        ImageProviderBuildContext(
            timeout_seconds=timeout_seconds,
            user_agent=USER_AGENT,
            adult_filter_off=adult_filter_off,
            pexels_api_key=os.getenv(ENV_PEXELS_API_KEY, "").strip(),
            pixabay_api_key=os.getenv(ENV_PIXABAY_API_KEY, "").strip(),
            logger=logger,
            allow_generic_web_image=("bing" in source_names),
            free_images_only=True,
        ),
    )
    if not providers:
        raise RuntimeError("No sources are available. Check API keys and dependencies.")
    logger.info(
        "Sources enabled: %s",
        ", ".join(getattr(provider, "provider_id", "unknown") for provider in providers),
    )
    return providers


def _tokenize(text: str) -> list[str]:
    return shared_tokenize(text)


def _candidate_hint_score(keyword: str, candidate: SearchCandidate) -> float:
    return shared_candidate_hint_score(keyword, candidate)


def _build_query_variants(keyword: str, paragraph_text: str) -> list[str]:
    return shared_build_query_variants(
        keyword,
        paragraph_text,
        short_suffixes=("photo", "realistic"),
    )


def _is_public_host(hostname: str) -> bool:
    return shared_is_public_host(hostname)


def _validate_public_url(raw_url: str) -> str:
    return shared_validate_public_url(raw_url)


def _open_with_safe_redirects(
    raw_url: str,
    timeout_seconds: float,
    max_redirects: int,
) -> tuple[Any, str]:
    return shared_open_with_safe_redirects(
        raw_url,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        accept_header="image/*,*/*;q=0.8",
        user_agent=USER_AGENT,
    )


def _read_limited(response: Any, max_bytes: int) -> bytes:
    return shared_read_limited(
        response,
        max_bytes=max_bytes,
        payload_name="Image",
        chunk_size=64 * 1024,
    )


def _normalize_image_bytes(
    raw_bytes: bytes,
    min_width: int,
    min_height: int,
) -> tuple[bytes, int, int, str]:
    if Image is None:
        raise RuntimeError("Pillow is not available")

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

    try:
        with Image.open(io.BytesIO(raw_bytes)) as probe:
            probe.verify()

        with Image.open(io.BytesIO(raw_bytes)) as img:
            img.load()
            width, height = img.size
            if width < min_width or height < min_height:
                raise ValueError(
                    f"Image too small: {width}x{height}, minimum is {min_width}x{min_height}"
                )
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError(
                    f"Image has too many pixels ({width * height}), max {MAX_IMAGE_PIXELS}"
                )

            if "A" in img.getbands() or "transparency" in img.info:
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                converted = background
            else:
                converted = img.convert("RGB")

            output = io.BytesIO()
            converted.save(output, format="JPEG", quality=90, optimize=True)
            normalized = output.getvalue()
            source_format = str(img.format or "UNKNOWN")
            return normalized, width, height, source_format
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Invalid or unreadable image bytes: {exc}") from exc


def _download_image_candidate(
    candidate: SearchCandidate,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    min_width: int,
    min_height: int,
) -> tuple[bytes, dict[str, object]]:
    response, final_url = _open_with_safe_redirects(
        raw_url=candidate.url,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )

    with response:
        content_type = (
            (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        )
        if content_type and not content_type.lower().startswith("image/"):
            raise ValueError(f"Non-image Content-Type: {content_type}")
        if content_type.lower() == "image/svg+xml":
            raise ValueError("SVG images are not allowed")
        raw_bytes = _read_limited(response, max_bytes=max_bytes)

    normalized, width, height, source_format = _normalize_image_bytes(
        raw_bytes,
        min_width=min_width,
        min_height=min_height,
    )
    sha256 = hashlib.sha256(normalized).hexdigest()

    metadata = {
        "provider": candidate.source,
        "query_used": candidate.query_used,
        "rank_hint": candidate.rank_hint,
        "url": candidate.url,
        "final_url": final_url,
        "referrer_url": candidate.referrer_url,
        "sha256": sha256,
        "content_type": content_type,
        "source_format": source_format,
        "width": width,
        "height": height,
        "bytes": len(normalized),
        "license": candidate.license_name,
        "license_url": candidate.license_url,
        "author": candidate.author,
        "commercial_allowed": candidate.commercial_allowed,
        "attribution_required": candidate.attribution_required,
    }
    return normalized, metadata


def _download_with_retries(
    candidate: SearchCandidate,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    min_width: int,
    min_height: int,
    retries: int = 1,
) -> tuple[bytes, dict[str, object]]:
    return retry_call(
        lambda: _download_image_candidate(
            candidate=candidate,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            min_width=min_width,
            min_height=min_height,
        ),
        retries=retries,
        base_delay_seconds=0.35,
        error_prefix="Failed to download image after retries",
    )


def _resolve_output_path(path_value: str | Path) -> Path:
    return shared_resolve_output_json_path(path_value)


def _build_run_dir(images_root: Path, run_prefix: str) -> tuple[str, Path]:
    return shared_build_run_dir(
        images_root,
        run_prefix,
        default_prefix=DEFAULT_RUN_PREFIX,
        max_prefix_len=24,
    )


def _build_flat_image_path(
    run_dir: Path,
    paragraph_no: int,
    keyword_index: int,
    image_no: int,
    keyword: str,
    image_hash: str,
) -> Path:
    slug = _slugify(keyword, max_len=28)
    short_hash = image_hash[:8]
    filename = (
        f"p{paragraph_no:03d}_k{keyword_index:02d}_"
        f"i{image_no:02d}_{slug}_{short_hash}.jpg"
    )
    return run_dir / filename


def _clean_model_text(text: str) -> str:
    return shared_clean_model_text(text)


def _parse_relevance_response(raw_text: str) -> tuple[bool, float, str]:
    return shared_parse_relevance_response(raw_text)


def _configure_genai(api_key: str) -> None:
    global _GEMINI_API_KEY
    _GEMINI_API_KEY = api_key.strip()
    ensure_gemini_sdk_available()


class SimpleRateLimiter(SharedSimpleRateLimiter):
    pass


class RelevanceCache(SharedImageRelevanceCache):
    pass


class RelevanceEvaluator:
    def __init__(
        self,
        model_name: str,
        threshold: float,
        max_gemini_rps: float,
        max_parallel_calls: int,
        cache: RelevanceCache,
    ):
        self.model_name = model_name
        self.threshold = max(0.0, min(1.0, float(threshold)))
        self._limiter = SimpleRateLimiter(max_rps=max_gemini_rps)
        self._semaphore = threading.Semaphore(max(1, int(max_parallel_calls)))
        self._cache = cache
        self._local = threading.local()

        ensure_gemini_sdk_available()

    def _get_model(self) -> Any:
        model = getattr(self._local, "model", None)
        if model is None:
            model = create_gemini_model(
                api_key=_GEMINI_API_KEY,
                model_name=self.model_name,
            )
            self._local.model = model
        return model

    def _make_prompt(self, keyword: str, paragraph_text: str) -> str:
        paragraph_short = re.sub(r"\s+", " ", paragraph_text or "").strip()[:300]
        return (
            "You are a strict image relevance judge.\n"
            "Task: decide if image visually matches keyword.\n"
            "Return ONLY JSON:\n"
            '{"match": true, "score": 0.0, "reason": "short"}\n'
            "Rules:\n"
            "- Be strict. Unrelated image -> match=false.\n"
            "- If uncertain -> match=false.\n"
            "- Score is between 0 and 1.\n"
            f"Keyword: {keyword}\n"
            f"Paragraph context: {paragraph_short or 'N/A'}"
        )

    def evaluate(
        self,
        keyword: str,
        paragraph_text: str,
        image_sha256: str,
        image_bytes: bytes,
    ) -> RelevanceResult:
        keyword_norm = re.sub(r"\s+", " ", keyword.strip()).casefold()
        cached = self._cache.get(keyword_norm, image_sha256, self.model_name)
        if cached is not None:
            is_match, score, reason = cached
            accepted = is_match and score >= self.threshold
            return RelevanceResult(
                is_relevant=accepted,
                is_match=is_match,
                score=score,
                reason=reason,
                from_cache=True,
            )

        transient_types = TRANSIENT_EXCEPTIONS

        prompt = self._make_prompt(keyword=keyword, paragraph_text=paragraph_text)
        model: Any = self._get_model()
        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                with self._semaphore:
                    self._limiter.wait()
                    response = model.generate_content(
                        [
                            prompt,
                            {
                                "mime_type": "image/jpeg",
                                "data": image_bytes,
                            },
                        ]
                    )

                raw = str(getattr(response, "text", "") or "").strip()
                is_match, score, reason = _parse_relevance_response(raw)
                self._cache.set(
                    keyword_norm=keyword_norm,
                    image_sha256=image_sha256,
                    model_name=self.model_name,
                    is_match=is_match,
                    score=score,
                    reason=reason,
                )
                accepted = is_match and score >= self.threshold
                return RelevanceResult(
                    is_relevant=accepted,
                    is_match=is_match,
                    score=score,
                    reason=reason,
                    from_cache=False,
                )
            except Exception as exc:
                last_error = exc
                if transient_types and isinstance(exc, transient_types) and attempt < 3:
                    time.sleep(0.9 * attempt + random.uniform(0.0, 0.3))
                    continue
                if attempt < 3:
                    time.sleep(0.4 * attempt)

        reason = f"gemini_error: {last_error}"
        return RelevanceResult(
            is_relevant=False,
            is_match=False,
            score=0.0,
            reason=reason[:240],
            from_cache=False,
        )


def _load_keywords_payload(path_value: str | Path) -> dict[str, object]:
    return shared_load_keywords_payload(path_value)


def _collect_candidates_for_keyword(
    keyword: str,
    paragraph_text: str,
    providers: list[Any],
    max_candidates_per_keyword: int,
    commercial_only: bool,
    allow_attribution_licenses: bool,
) -> tuple[list[SearchCandidate], list[str]]:
    service = _IMAGE_PROVIDER_SEARCH_SERVICE or _configure_image_provider_search_service(
        DEFAULT_PROVIDER_SEARCH_CACHE_ROOT
    )
    collected, errors, diagnostics = service.search_keyword(
        keyword,
        paragraph_text,
        providers,
        max_candidates_per_keyword=max_candidates_per_keyword,
        license_policy=ImageLicensePolicy(
            commercial_only=commercial_only,
            allow_attribution_licenses=allow_attribution_licenses,
        ),
    )
    if diagnostics.rejected_prefilters:
        logger.debug(
            "Prefilter rejected %s candidate(s) for '%s'",
            len(diagnostics.rejected_prefilters),
            keyword,
        )
    return collected, errors


def _download_candidates_parallel(
    candidates: list[SearchCandidate],
    download_executor: ThreadPoolExecutor,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    min_width: int,
    min_height: int,
) -> tuple[list[tuple[bytes, dict[str, object]]], int]:
    downloaded: list[tuple[bytes, dict[str, object]]] = []
    failed = 0

    future_map = {
        download_executor.submit(
            _download_with_retries,
            candidate,
            timeout_seconds,
            max_bytes,
            max_redirects,
            min_width,
            min_height,
            1,
        ): candidate
        for candidate in candidates
    }

    for future in as_completed(future_map):
        try:
            image_bytes, metadata = future.result()
            downloaded.append((image_bytes, metadata))
        except Exception:
            failed += 1

    return downloaded, failed


def _evaluate_relevance_parallel(
    downloaded_items: list[tuple[bytes, dict[str, object]]],
    keyword: str,
    paragraph_text: str,
    images_needed: int,
    relevance_executor: ThreadPoolExecutor,
    relevance_workers: int,
    evaluator: RelevanceEvaluator,
    seen_hashes_global: set[str],
    seen_hashes_lock: threading.Lock | None = None,
) -> tuple[
    list[tuple[bytes, dict[str, object]]],
    list[dict[str, object]],
    int,
    int,
    int,
]:
    accepted: list[tuple[bytes, dict[str, object]]] = []
    rejected_examples: list[dict[str, object]] = []
    rejected_count = 0
    gemini_calls = 0
    cache_hits = 0

    downloaded_items = sorted(
        downloaded_items,
        key=lambda item: float(item[1].get("rank_hint", 0.0) or 0.0),
        reverse=True,
    )[:DEFAULT_TOP_K_TO_RELEVANCE]

    queue_index = 0
    queue_limit = max(2, relevance_workers * MAX_RELEVANCE_QUEUE_FACTOR)
    pending: dict[Any, tuple[bytes, dict[str, object]]] = {}

    def submit_next() -> bool:
        nonlocal queue_index
        if queue_index >= len(downloaded_items):
            return False
        image_bytes, metadata = downloaded_items[queue_index]
        queue_index += 1
        future = relevance_executor.submit(
            evaluator.evaluate,
            keyword,
            paragraph_text,
            str(metadata.get("sha256", "")),
            image_bytes,
        )
        pending[future] = (image_bytes, metadata)
        return True

    while len(pending) < queue_limit and submit_next():
        pass

    while pending:
        done_set, _ = wait(set(pending.keys()), return_when=FIRST_COMPLETED)
        for future in done_set:
            image_bytes, metadata = pending.pop(future)
            try:
                relevance = future.result()
            except Exception as exc:
                relevance = RelevanceResult(
                    is_relevant=False,
                    is_match=False,
                    score=0.0,
                    reason=f"relevance_worker_error: {exc}",
                    from_cache=False,
                )

            if not relevance.from_cache:
                gemini_calls += 1
            else:
                cache_hits += 1

            metadata["relevance_match"] = relevance.is_match
            metadata["relevance_score"] = relevance.score
            metadata["relevance_reason"] = relevance.reason
            metadata["relevance_from_cache"] = relevance.from_cache
            metadata["relevance_threshold"] = evaluator.threshold

            image_hash = str(metadata.get("sha256", ""))
            is_unique = False
            if relevance.is_relevant and image_hash:
                if seen_hashes_lock is None:
                    if image_hash not in seen_hashes_global:
                        seen_hashes_global.add(image_hash)
                        is_unique = True
                else:
                    with seen_hashes_lock:
                        if image_hash not in seen_hashes_global:
                            seen_hashes_global.add(image_hash)
                            is_unique = True

            if is_unique:
                accepted.append((image_bytes, metadata))
            else:
                rejected_count += 1
                if len(rejected_examples) < 5:
                    rejected_examples.append(
                        {
                            "url": metadata.get("final_url") or metadata.get("url"),
                            "score": relevance.score,
                            "reason": relevance.reason,
                            "source": metadata.get("provider"),
                        }
                    )

        if len(accepted) >= images_needed:
            for pending_future in pending.keys():
                pending_future.cancel()
            break

        while len(pending) < queue_limit and submit_next():
            pass

    return (
        accepted[:images_needed],
        rejected_examples,
        rejected_count,
        gemini_calls,
        cache_hits,
    )


def run_image_fetch(
    input_json: str | Path = DEFAULT_KEYWORDS_JSON,
    output_json: str | Path = DEFAULT_OUTPUT_JSON,
    images_dir: str | Path = DEFAULT_IMAGES_DIR,
    run_prefix: str = DEFAULT_RUN_PREFIX,
    sources: str = DEFAULT_SOURCES,
    images_per_keyword: int = DEFAULT_IMAGES_PER_KEYWORD,
    max_candidates_per_keyword: int = DEFAULT_MAX_CANDIDATES_PER_KEYWORD,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_paragraphs: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    min_width: int = DEFAULT_MIN_WIDTH,
    min_height: int = DEFAULT_MIN_HEIGHT,
    adult_filter_off: bool = DEFAULT_ADULT_FILTER_OFF,
    relevance_model: str = DEFAULT_RELEVANCE_MODEL,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    keyword_workers: int = DEFAULT_KEYWORD_WORKERS,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    relevance_workers: int = DEFAULT_RELEVANCE_WORKERS,
    max_gemini_rps: float = DEFAULT_MAX_GEMINI_RPS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    relevance_cache_path: str | Path = DEFAULT_RELEVANCE_CACHE,
    commercial_only: bool = DEFAULT_COMMERCIAL_ONLY,
    allow_attribution_licenses: bool = DEFAULT_ALLOW_ATTRIBUTION_LICENSES,
    fail_fast: bool = False,
) -> tuple[list[dict[str, object]], Path]:
    if images_per_keyword <= 0:
        raise ValueError("images_per_keyword must be positive")
    if max_candidates_per_keyword <= 0:
        raise ValueError("max_candidates_per_keyword must be positive")
    if not (0.0 <= relevance_threshold <= 1.0):
        raise ValueError("relevance_threshold must be in [0, 1]")
    if keyword_workers < 1 or download_workers < 1 or relevance_workers < 1:
        raise ValueError("workers must be >= 1")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    load_dotenv()
    _ensure_runtime_dependencies()

    payload = _load_keywords_payload(input_json)
    paragraphs = _extract_paragraph_tasks(payload, max_paragraphs=max_paragraphs)
    if not paragraphs:
        raise ValueError("No paragraph image queries found in input JSON")

    gemini_key = os.getenv(ENV_GEMINI_API_KEY, "").strip()
    if not gemini_key or gemini_key.lower() == "your_gemini_api_key_here":
        raise RuntimeError(
            f"{ENV_GEMINI_API_KEY} is missing in {get_env_path()}. Create it from .env.example or set the environment variable."
        )
    _configure_genai(gemini_key)

    provider_cache_root = _resolve_output_path(output_json).parent / "cache"
    _configure_image_provider_search_service(provider_cache_root)

    provider_names = _parse_sources(sources)
    providers = _build_providers(
        source_names=provider_names,
        timeout_seconds=timeout_seconds,
        adult_filter_off=adult_filter_off,
    )

    images_root = Path(images_dir)
    images_root.mkdir(parents=True, exist_ok=True)
    run_id, run_dir = _build_run_dir(images_root=images_root, run_prefix=run_prefix)

    cache = RelevanceCache(relevance_cache_path)
    evaluator = RelevanceEvaluator(
        model_name=relevance_model,
        threshold=relevance_threshold,
        max_gemini_rps=max_gemini_rps,
        max_parallel_calls=relevance_workers,
        cache=cache,
    )

    paragraph_results: list[dict[str, object]] = []
    images_by_paragraph: dict[str, dict[str, list[dict[str, object]]]] = {}

    seen_hashes: set[str] = set()
    seen_hashes_lock = threading.Lock()

    keyword_jobs: list[dict[str, object]] = []
    for paragraph in paragraphs:
        for keyword_index, keyword in enumerate(paragraph.keywords, start=1):
            keyword_jobs.append(
                {
                    "paragraph_no": paragraph.paragraph_no,
                    "original_index": paragraph.original_index,
                    "paragraph_text": paragraph.text,
                    "keyword_index": keyword_index,
                    "keyword": keyword,
                }
            )

    total_keywords = len(keyword_jobs)
    keywords_with_images = 0
    images_downloaded = 0
    total_candidates = 0
    total_download_failures = 0
    total_rejected_by_relevance = 0
    total_gemini_calls = 0
    total_cache_hits = 0

    started_at = time.perf_counter()

    def process_keyword_job(
        job: dict[str, object],
        download_executor: ThreadPoolExecutor,
        relevance_executor: ThreadPoolExecutor,
    ) -> dict[str, object]:
        paragraph_no = int(job["paragraph_no"])
        keyword_index = int(job["keyword_index"])
        keyword = str(job["keyword"])
        paragraph_text = str(job.get("paragraph_text", ""))

        t_keyword_start = time.perf_counter()
        errors: list[str] = []
        rejected_examples: list[dict[str, object]] = []
        collected: list[dict[str, object]] = []

        candidates, source_errors = _collect_candidates_for_keyword(
            keyword=keyword,
            paragraph_text=paragraph_text,
            providers=providers,
            max_candidates_per_keyword=max_candidates_per_keyword,
            commercial_only=commercial_only,
            allow_attribution_licenses=allow_attribution_licenses,
        )
        errors.extend(source_errors)

        download_failed_total = 0
        rejected_total = 0
        gemini_calls_total = 0
        cache_hits_total = 0
        checked_total = 0

        for offset in range(0, len(candidates), batch_size):
            if len(collected) >= images_per_keyword:
                break

            batch_candidates = candidates[offset : offset + batch_size]
            downloaded_items, download_failed = _download_candidates_parallel(
                candidates=batch_candidates,
                download_executor=download_executor,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                max_redirects=max_redirects,
                min_width=min_width,
                min_height=min_height,
            )
            download_failed_total += download_failed
            checked_total += len(downloaded_items)

            remaining = images_per_keyword - len(collected)
            (
                accepted_pairs,
                rejected_batch_examples,
                rejected_count,
                gemini_calls,
                cache_hits,
            ) = _evaluate_relevance_parallel(
                downloaded_items=downloaded_items,
                keyword=keyword,
                paragraph_text=paragraph_text,
                images_needed=remaining,
                relevance_executor=relevance_executor,
                relevance_workers=relevance_workers,
                evaluator=evaluator,
                seen_hashes_global=seen_hashes,
                seen_hashes_lock=seen_hashes_lock,
            )
            rejected_total += rejected_count
            gemini_calls_total += gemini_calls
            cache_hits_total += cache_hits

            for sample in rejected_batch_examples:
                if len(rejected_examples) >= 5:
                    break
                rejected_examples.append(sample)

            for image_bytes, metadata in accepted_pairs:
                if len(collected) >= images_per_keyword:
                    break
                image_no = len(collected) + 1
                image_hash = str(metadata.get("sha256", ""))
                file_path = _build_flat_image_path(
                    run_dir=run_dir,
                    paragraph_no=paragraph_no,
                    keyword_index=keyword_index,
                    image_no=image_no,
                    keyword=keyword,
                    image_hash=image_hash,
                )
                file_path.write_bytes(image_bytes)
                metadata["image_no"] = image_no
                metadata["path"] = str(file_path)
                metadata["run_id"] = run_id
                collected.append(metadata)

            logger.info(
                "Progress p%s k%s '%s': accepted=%s/%s checked=%s/%s",
                paragraph_no,
                keyword_index,
                keyword,
                len(collected),
                images_per_keyword,
                min(offset + len(batch_candidates), len(candidates)),
                len(candidates),
            )

        if len(collected) < images_per_keyword:
            errors.append(
                f"Collected {len(collected)}/{images_per_keyword} images for keyword"
            )

        elapsed_keyword = round(time.perf_counter() - t_keyword_start, 2)
        logger.info(
            "Paragraph %s keyword %s '%s': %s/%s image(s), rejected=%s, sec=%.2f",
            paragraph_no,
            keyword_index,
            keyword,
            len(collected),
            images_per_keyword,
            rejected_total,
            elapsed_keyword,
        )

        return {
            "paragraph_no": paragraph_no,
            "keyword_index": keyword_index,
            "keyword": keyword,
            "images": collected,
            "errors": errors,
            "metrics": {
                "candidates_total": len(candidates),
                "download_failed": download_failed_total,
                "rejected_by_relevance": rejected_total,
                "gemini_calls": gemini_calls_total,
                "cache_hits": cache_hits_total,
                "checked_downloaded": checked_total,
                "elapsed_seconds": elapsed_keyword,
                "saved_files_count": len(collected),
                "saved_paths": [str(item.get("path", "")) for item in collected],
            },
            "rejected_examples": rejected_examples,
        }

    results_by_key: dict[tuple[int, int], dict[str, object]] = {}

    try:
        with (
            ThreadPoolExecutor(max_workers=download_workers) as download_executor,
            ThreadPoolExecutor(max_workers=relevance_workers) as relevance_executor,
            ThreadPoolExecutor(max_workers=keyword_workers) as keyword_executor,
        ):
            future_map = {
                keyword_executor.submit(
                    process_keyword_job,
                    job,
                    download_executor,
                    relevance_executor,
                ): job
                for job in keyword_jobs
            }

            for future in as_completed(future_map):
                job = future_map[future]
                paragraph_no = int(job["paragraph_no"])
                keyword_index = int(job["keyword_index"])
                try:
                    result = future.result()
                except Exception as exc:
                    for pending in future_map:
                        if not pending.done():
                            pending.cancel()
                    raise RuntimeError(
                        f"Keyword processing failed for paragraph {paragraph_no}, "
                        f"keyword #{keyword_index}: {exc}"
                    ) from exc

                key = (paragraph_no, keyword_index)
                results_by_key[key] = result

                metrics = result.get("metrics", {})
                total_candidates += int(metrics.get("candidates_total", 0))
                total_download_failures += int(metrics.get("download_failed", 0))
                total_rejected_by_relevance += int(
                    metrics.get("rejected_by_relevance", 0)
                )
                total_gemini_calls += int(metrics.get("gemini_calls", 0))
                total_cache_hits += int(metrics.get("cache_hits", 0))
                images_downloaded += int(metrics.get("saved_files_count", 0))

                if len(result.get("images", [])) > 0:
                    keywords_with_images += 1
                if fail_fast and len(result.get("images", [])) < images_per_keyword:
                    for pending in future_map:
                        if not pending.done():
                            pending.cancel()
                    raise RuntimeError(
                        f"Failed to collect enough relevant images for paragraph {paragraph_no}, "
                        f"keyword '{result.get('keyword')}'"
                    )

                if delay_seconds > 0:
                    time.sleep(delay_seconds)

        for paragraph in paragraphs:
            keyword_results: list[dict[str, object]] = []
            paragraph_map: dict[str, list[dict[str, object]]] = {}

            for keyword_index, keyword in enumerate(paragraph.keywords, start=1):
                key = (paragraph.paragraph_no, keyword_index)
                result = results_by_key.get(key)
                if result is None:
                    result = {
                        "keyword_index": keyword_index,
                        "keyword": keyword,
                        "images": [],
                        "errors": ["Keyword result is missing"],
                        "metrics": {
                            "candidates_total": 0,
                            "download_failed": 0,
                            "rejected_by_relevance": 0,
                            "gemini_calls": 0,
                            "cache_hits": 0,
                            "checked_downloaded": 0,
                            "elapsed_seconds": 0.0,
                            "saved_files_count": 0,
                            "saved_paths": [],
                        },
                        "rejected_examples": [],
                    }

                keyword_results.append(
                    {
                        "keyword_index": int(
                            result.get("keyword_index", keyword_index)
                        ),
                        "keyword": str(result.get("keyword", keyword)),
                        "images": list(result.get("images", [])),
                        "errors": list(result.get("errors", [])),
                        "metrics": dict(result.get("metrics", {})),
                        "rejected_examples": list(result.get("rejected_examples", [])),
                    }
                )
                paragraph_map[keyword] = list(result.get("images", []))

            paragraph_results.append(
                {
                    "paragraph_no": paragraph.paragraph_no,
                    "original_index": paragraph.original_index,
                    "text": paragraph.text,
                    "keywords": keyword_results,
                }
            )
            images_by_paragraph[str(paragraph.paragraph_no)] = paragraph_map

        total_elapsed = round(time.perf_counter() - started_at, 2)

        out_file = _resolve_output_path(output_json)
        provider_chain = [
            getattr(provider, "provider_id", getattr(provider, "name", "unknown"))
            for provider in providers
        ]
        provider_limits: dict[str, dict[str, object]] = {
            name: {
                "rate_limit": "unknown",
                "remaining": "unknown",
                "reset": "unknown",
                "saved_images": 0,
            }
            for name in provider_chain
        }

        for paragraph in paragraph_results:
            keywords = paragraph.get("keywords", [])
            if not isinstance(keywords, list):
                continue
            for keyword_info in keywords:
                if not isinstance(keyword_info, dict):
                    continue
                images = keyword_info.get("images", [])
                if not isinstance(images, list):
                    continue
                for image_info in images:
                    if not isinstance(image_info, dict):
                        continue
                    provider_name = str(image_info.get("provider", "unknown"))
                    stats = provider_limits.setdefault(
                        provider_name,
                        {
                            "rate_limit": "unknown",
                            "remaining": "unknown",
                            "reset": "unknown",
                            "saved_images": 0,
                        },
                    )
                    stats["saved_images"] = int(stats.get("saved_images", 0)) + 1

        output_payload = {
            "source_keywords_file": str(Path(input_json)),
            "images_root": str(images_root),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "provider_chain": provider_chain,
            "images_per_keyword": images_per_keyword,
            "max_candidates_per_keyword": max_candidates_per_keyword,
            "relevance_model": relevance_model,
            "relevance_threshold": relevance_threshold,
            "workers": {
                "keyword_workers": keyword_workers,
                "download_workers": download_workers,
                "relevance_workers": relevance_workers,
                "batch_size": batch_size,
            },
            "license_policy": {
                "commercial_only": commercial_only,
                "allow_attribution_licenses": allow_attribution_licenses,
            },
            "provider_strategy": {
                "free_images_only": True,
                "mixed_mode_fallback_ready": True,
                "generic_web_image_opt_in": "bing" in provider_chain,
            },
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "paragraphs_total": len(paragraph_results),
                "keywords_total": total_keywords,
                "keywords_with_images": keywords_with_images,
                "images_downloaded": images_downloaded,
                "candidates_total": total_candidates,
                "download_failures": total_download_failures,
                "rejected_by_relevance": total_rejected_by_relevance,
                "gemini_calls": total_gemini_calls,
                "cache_hits": total_cache_hits,
                "elapsed_seconds": total_elapsed,
            },
            "limits": {
                "images": {
                    "configured": {
                        "sources": sources,
                        "images_per_keyword": images_per_keyword,
                        "max_candidates_per_keyword": max_candidates_per_keyword,
                        "delay_seconds": delay_seconds,
                        "max_paragraphs": max_paragraphs,
                        "timeout_seconds": timeout_seconds,
                        "max_bytes": max_bytes,
                        "max_redirects": max_redirects,
                        "min_width": min_width,
                        "min_height": min_height,
                        "adult_filter_off": adult_filter_off,
                        "relevance_model": relevance_model,
                        "relevance_threshold": relevance_threshold,
                        "keyword_workers": keyword_workers,
                        "download_workers": download_workers,
                        "relevance_workers": relevance_workers,
                        "max_gemini_rps": max_gemini_rps,
                        "batch_size": batch_size,
                        "relevance_cache_path": str(relevance_cache_path),
                        "provider_search_cache_root": str(provider_cache_root),
                        "top_k_to_relevance": DEFAULT_TOP_K_TO_RELEVANCE,
                        "commercial_only": commercial_only,
                        "allow_attribution_licenses": allow_attribution_licenses,
                    },
                    "effective": {
                        "provider_chain": provider_chain,
                        "images_per_keyword": images_per_keyword,
                        "max_candidates_per_keyword": max_candidates_per_keyword,
                        "timeout_seconds": timeout_seconds,
                        "max_bytes": max_bytes,
                        "max_redirects": max_redirects,
                        "min_width": min_width,
                        "min_height": min_height,
                        "workers": {
                            "keyword_workers": keyword_workers,
                            "download_workers": download_workers,
                            "relevance_workers": relevance_workers,
                        },
                        "max_gemini_rps": max_gemini_rps,
                        "batch_size": batch_size,
                        "top_k_to_relevance": DEFAULT_TOP_K_TO_RELEVANCE,
                        "relevance_threshold": relevance_threshold,
                    },
                    "usage": {
                        "keywords_total": total_keywords,
                        "keywords_with_images": keywords_with_images,
                        "candidates_total": total_candidates,
                        "download_failures": total_download_failures,
                        "rejected_by_relevance": total_rejected_by_relevance,
                        "images_downloaded": images_downloaded,
                        "gemini_calls": total_gemini_calls,
                        "cache_hits": total_cache_hits,
                    },
                },
                "providers": provider_limits,
                "gemini": {
                    "model": relevance_model,
                    "max_rps": max_gemini_rps,
                    "calls": total_gemini_calls,
                    "cache_hits": total_cache_hits,
                    "remaining": "sdk_not_exposed",
                },
            },
            "images_by_paragraph": images_by_paragraph,
            "items": paragraph_results,
        }

        out_file.write_text(
            json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return paragraph_results, out_file
    finally:
        cache.close()
        _close_image_provider_search_service()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch commercial images from free sources and keep only "
            "Gemini-verified relevant results"
        )
    )
    parser.add_argument("--input", "-i", default=DEFAULT_KEYWORDS_JSON)
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--images-dir", default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--run-prefix", default=DEFAULT_RUN_PREFIX)
    parser.add_argument("--sources", default=DEFAULT_SOURCES)

    parser.add_argument(
        "--images-per-keyword", type=int, default=DEFAULT_IMAGES_PER_KEYWORD
    )
    parser.add_argument(
        "--max-candidates-per-keyword",
        type=int,
        default=DEFAULT_MAX_CANDIDATES_PER_KEYWORD,
    )

    parser.add_argument("--relevance-model", default=DEFAULT_RELEVANCE_MODEL)
    parser.add_argument(
        "--relevance-threshold",
        type=float,
        default=DEFAULT_RELEVANCE_THRESHOLD,
    )
    parser.add_argument("--keyword-workers", type=int, default=DEFAULT_KEYWORD_WORKERS)
    parser.add_argument(
        "--download-workers", type=int, default=DEFAULT_DOWNLOAD_WORKERS
    )
    parser.add_argument(
        "--relevance-workers", type=int, default=DEFAULT_RELEVANCE_WORKERS
    )
    parser.add_argument("--max-gemini-rps", type=float, default=DEFAULT_MAX_GEMINI_RPS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--relevance-cache", default=DEFAULT_RELEVANCE_CACHE)

    parser.add_argument(
        "--allow-non-commercial",
        action="store_true",
        help="Allow non-commercial licenses",
    )
    parser.add_argument(
        "--allow-attribution-licenses",
        action="store_true",
        help="Allow licenses requiring attribution (CC-BY, CC-BY-SA)",
    )

    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--max-paragraphs", type=int)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-redirects", type=int, default=DEFAULT_MAX_REDIRECTS)
    parser.add_argument("--min-width", type=int, default=DEFAULT_MIN_WIDTH)
    parser.add_argument("--min-height", type=int, default=DEFAULT_MIN_HEIGHT)
    parser.add_argument("--adult-filter-off", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    _, out_file = run_image_fetch(
        input_json=args.input,
        output_json=args.output,
        images_dir=args.images_dir,
        run_prefix=args.run_prefix,
        sources=args.sources,
        images_per_keyword=args.images_per_keyword,
        max_candidates_per_keyword=args.max_candidates_per_keyword,
        delay_seconds=args.delay,
        max_paragraphs=args.max_paragraphs,
        timeout_seconds=args.timeout,
        max_bytes=args.max_bytes,
        max_redirects=args.max_redirects,
        min_width=args.min_width,
        min_height=args.min_height,
        adult_filter_off=args.adult_filter_off,
        relevance_model=args.relevance_model,
        relevance_threshold=args.relevance_threshold,
        keyword_workers=args.keyword_workers,
        download_workers=args.download_workers,
        relevance_workers=args.relevance_workers,
        max_gemini_rps=args.max_gemini_rps,
        batch_size=args.batch_size,
        relevance_cache_path=args.relevance_cache,
        commercial_only=not args.allow_non_commercial,
        allow_attribution_licenses=args.allow_attribution_licenses,
        fail_fast=args.fail_fast,
    )
    logger.info("Done. Saved image manifest to: %s", out_file)


if __name__ == "__main__":
    main()
