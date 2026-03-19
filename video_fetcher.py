import argparse
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from legacy_core.common import normalize_keywords as shared_normalize_keywords
from legacy_core.common import safe_float as shared_safe_float
from legacy_core.common import safe_int as shared_safe_int
from legacy_core.diagnostics import build_provider_limit_summary
from legacy_core.diagnostics import bump_provider_stat as shared_bump_provider_stat
from legacy_core.diagnostics import init_provider_stats
from legacy_core.env import get_env_path as shared_get_env_path
from legacy_core.env import load_dotenv as shared_load_dotenv
from legacy_core.files import build_run_dir as shared_build_run_dir
from legacy_core.files import resolve_output_json_path as shared_resolve_output_json_path
from legacy_core.files import write_hashed_temp_file
from legacy_core.keyword_payload import extract_paragraph_tasks as shared_extract_paragraph_tasks
from legacy_core.keyword_payload import load_keywords_payload as shared_load_keywords_payload
from legacy_core.licenses import is_license_allowed as shared_is_license_allowed
from legacy_core.licenses import normalize_license_info as shared_normalize_license_info
from legacy_core.network import http_get_json as shared_http_get_json
from legacy_core.network import is_public_host as shared_is_public_host
from legacy_core.network import open_with_safe_redirects as shared_open_with_safe_redirects
from legacy_core.network import read_limited as shared_read_limited
from legacy_core.network import validate_public_url as shared_validate_public_url
from pathlib import Path
from typing import Any
from legacy_core.query_utils import build_query_variants as shared_build_query_variants
from legacy_core.query_utils import candidate_hint_score as shared_candidate_hint_score
from legacy_core.query_utils import parse_sources as shared_parse_sources
from legacy_core.query_utils import tokenize as shared_tokenize
from legacy_core.relevance import SimpleRateLimiter as SharedSimpleRateLimiter
from legacy_core.relevance import VideoRelevanceCache as SharedVideoRelevanceCache
from legacy_core.relevance import clean_model_text as shared_clean_model_text
from legacy_core.relevance import parse_relevance_response as shared_parse_relevance_response
from legacy_core.retry import retry_call
from services.genai_client import create_gemini_model, ensure_gemini_sdk_available, get_transient_exceptions
from legacy_core.video_tools import ensure_ffmpeg_tools_available
from legacy_core.video_tools import guess_video_extension as shared_guess_video_extension
from legacy_core.video_tools import parse_frame_rate as shared_parse_frame_rate
from legacy_core.video_tools import probe_video as shared_probe_video
from legacy_core.video_tools import run_command as shared_run_command
from legacy_core.video_tools import validate_video_quality as shared_validate_video_quality


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


DEFAULT_KEYWORDS_JSON = "output/paragraph_intents.json"
DEFAULT_OUTPUT_JSON = "output/keyword_videos.json"
DEFAULT_VIDEOS_DIR = "output/videos"
DEFAULT_TEMP_ROOT = "output/tmp/videos"
DEFAULT_RUN_PREFIX = "run"
DEFAULT_SOURCES = "pexels,pixabay,wikimedia"

DEFAULT_VIDEOS_PER_KEYWORD = 2
DEFAULT_MAX_CANDIDATES_PER_KEYWORD = 60
DEFAULT_DELAY_SECONDS = 0.0
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_MAX_BYTES = 120 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MIN_WIDTH = 1280
DEFAULT_MIN_HEIGHT = 720
DEFAULT_MIN_DURATION_SECONDS = 4.0
DEFAULT_MAX_DURATION_SECONDS = 20.0
DEFAULT_MIN_FPS = 20.0

DEFAULT_RELEVANCE_MODEL = "gemini-2.5-flash"
DEFAULT_RELEVANCE_THRESHOLD = 0.82
DEFAULT_KEYWORD_WORKERS = 2
DEFAULT_DOWNLOAD_WORKERS = 4
DEFAULT_RELEVANCE_WORKERS = 3
DEFAULT_MAX_GEMINI_RPS = 1.5
DEFAULT_BATCH_SIZE = 8
DEFAULT_FRAME_SAMPLES = 8
DEFAULT_FRAME_EXTRACT_TIMEOUT = 20.0
DEFAULT_RELEVANCE_CACHE = "output/cache/video_relevance_cache.sqlite"

DEFAULT_COMMERCIAL_ONLY = True
DEFAULT_ALLOW_ATTRIBUTION_LICENSES = False

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


FFMPEG_BIN: str | None = None
FFPROBE_BIN: str | None = None
TRANSIENT_EXCEPTIONS = get_transient_exceptions()
_GEMINI_API_KEY = ""


@dataclass(slots=True)
class VideoCandidate:
    source: str
    url: str
    referrer_url: str | None
    query_used: str
    license_name: str
    license_url: str | None
    author: str | None
    commercial_allowed: bool
    attribution_required: bool
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    file_type: str | None = None
    rank_hint: float = 0.0


@dataclass(slots=True)
class ParagraphTask:
    paragraph_no: int
    original_index: int | None
    text: str
    keywords: list[str]


@dataclass(slots=True)
class VideoRelevanceResult:
    is_relevant: bool
    is_match: bool
    score: float
    reason: str
    from_cache: bool
    frames_used: int


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_env_path() -> Path:
    return shared_get_env_path(__file__, env_filename=ENV_FILENAME)


def load_dotenv(dotenv_path: Path | None = None) -> None:
    shared_load_dotenv(dotenv_path, anchor_file=__file__, env_filename=ENV_FILENAME)


def _ensure_runtime_dependencies() -> None:
    global FFMPEG_BIN, FFPROBE_BIN
    ensure_gemini_sdk_available()

    ffmpeg, ffprobe = ensure_ffmpeg_tools_available()
    FFMPEG_BIN = ffmpeg
    FFPROBE_BIN = ffprobe


def _safe_int(value: object, default: int | None = None) -> int | None:
    return shared_safe_int(value, default)


def _safe_float(value: object, default: float | None = None) -> float | None:
    return shared_safe_float(value, default)


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
            query_kind="video",
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
    candidate: VideoCandidate,
    commercial_only: bool,
    allow_attribution_licenses: bool,
) -> bool:
    return shared_is_license_allowed(
        candidate,
        commercial_only=commercial_only,
        allow_attribution_licenses=allow_attribution_licenses,
    )


class PexelsVideoProvider:
    name = "pexels"

    def __init__(self, api_key: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._api_key = api_key.strip()
        self._timeout = timeout_seconds
        if not self._api_key:
            raise ValueError("Missing PEXELS_API_KEY")

    def search(self, query: str, limit: int) -> list[VideoCandidate]:
        per_page = max(5, min(80, int(limit)))
        payload = _http_get_json(
            "https://api.pexels.com/videos/search",
            params={
                "query": query,
                "per_page": per_page,
                "page": 1,
                "orientation": "landscape",
            },
            headers={"Authorization": self._api_key},
            timeout_seconds=self._timeout,
        )

        videos = payload.get("videos")
        if not isinstance(videos, list):
            return []

        results: list[VideoCandidate] = []
        for item in videos:
            if not isinstance(item, dict):
                continue

            duration_seconds = _safe_float(item.get("duration"), None)
            video_files = item.get("video_files")
            if not isinstance(video_files, list):
                continue

            best_file: dict[str, object] | None = None
            best_score = -1.0
            for video_file in video_files:
                if not isinstance(video_file, dict):
                    continue
                url = str(video_file.get("link") or "").strip()
                if not url:
                    continue
                width = _safe_int(video_file.get("width"), 0) or 0
                height = _safe_int(video_file.get("height"), 0) or 0
                fps = _safe_float(video_file.get("fps"), 0.0) or 0.0
                file_type = str(video_file.get("file_type") or "").strip().lower()
                if file_type and "mp4" not in file_type and "webm" not in file_type:
                    continue

                score = float(width * height) + (fps * 100.0)
                if score > best_score:
                    best_score = score
                    best_file = video_file

            if not isinstance(best_file, dict):
                continue

            best_url = str(best_file.get("link") or "").strip()
            if not best_url:
                continue

            width = _safe_int(best_file.get("width"), None)
            height = _safe_int(best_file.get("height"), None)
            fps = _safe_float(best_file.get("fps"), None)
            file_type = str(best_file.get("file_type") or "").strip() or None

            license_name, commercial_allowed, attribution_required = (
                _normalize_license_info(
                    source=self.name,
                    raw_license="pexels-license",
                    raw_license_url="https://www.pexels.com/license/",
                )
            )

            user = item.get("user")
            author = None
            if isinstance(user, dict):
                author = str(user.get("name") or "").strip() or None

            results.append(
                VideoCandidate(
                    source=self.name,
                    url=best_url,
                    referrer_url=str(item.get("url") or "").strip() or None,
                    query_used=query,
                    license_name=license_name,
                    license_url="https://www.pexels.com/license/",
                    author=author,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                    duration_seconds=duration_seconds,
                    width=width,
                    height=height,
                    fps=fps,
                    file_type=file_type,
                )
            )

        return results


class PixabayVideoProvider:
    name = "pixabay"

    def __init__(self, api_key: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._api_key = api_key.strip()
        self._timeout = timeout_seconds
        if not self._api_key:
            raise ValueError("Missing PIXABAY_API_KEY")

    def search(self, query: str, limit: int) -> list[VideoCandidate]:
        per_page = max(5, min(200, int(limit)))
        payload = _http_get_json(
            "https://pixabay.com/api/videos/",
            params={
                "key": self._api_key,
                "q": query,
                "safesearch": "true",
                "per_page": per_page,
                "page": 1,
            },
            timeout_seconds=self._timeout,
        )

        hits = payload.get("hits")
        if not isinstance(hits, list):
            return []

        results: list[VideoCandidate] = []
        for item in hits:
            if not isinstance(item, dict):
                continue

            videos = item.get("videos")
            if not isinstance(videos, dict):
                continue

            best_variant: dict[str, object] | None = None
            best_score = -1.0
            for variant in videos.values():
                if not isinstance(variant, dict):
                    continue
                url = str(variant.get("url") or "").strip()
                if not url:
                    continue

                width = _safe_int(variant.get("width"), 0) or 0
                height = _safe_int(variant.get("height"), 0) or 0
                size = _safe_int(variant.get("size"), 0) or 0
                score = float(width * height) + (float(size) / 1024.0)

                if score > best_score:
                    best_score = score
                    best_variant = variant

            if not isinstance(best_variant, dict):
                continue

            url = str(best_variant.get("url") or "").strip()
            if not url:
                continue

            width = _safe_int(best_variant.get("width"), None)
            height = _safe_int(best_variant.get("height"), None)
            license_name, commercial_allowed, attribution_required = (
                _normalize_license_info(
                    source=self.name,
                    raw_license="pixabay-license",
                    raw_license_url="https://pixabay.com/service/license-summary/",
                )
            )

            results.append(
                VideoCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=str(item.get("pageURL") or "").strip() or None,
                    query_used=query,
                    license_name=license_name,
                    license_url="https://pixabay.com/service/license-summary/",
                    author=str(item.get("user") or "").strip() or None,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                    duration_seconds=_safe_float(item.get("duration"), None),
                    width=width,
                    height=height,
                    fps=None,
                    file_type="video/mp4",
                )
            )

        return results


class WikimediaVideoProvider:
    name = "wikimedia"

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._timeout = timeout_seconds

    def search(self, query: str, limit: int) -> list[VideoCandidate]:
        amount = max(5, min(50, int(limit)))
        payload = _http_get_json(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"{query} filetype:video",
                "gsrnamespace": 6,
                "gsrlimit": amount,
                "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata",
            },
            timeout_seconds=self._timeout,
        )

        query_data = payload.get("query")
        if not isinstance(query_data, dict):
            return []
        pages = query_data.get("pages")
        if not isinstance(pages, dict):
            return []

        results: list[VideoCandidate] = []
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

            mime = str(info.get("mime") or "").strip().lower()
            if mime and not mime.startswith("video/"):
                continue

            metadata = info.get("extmetadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata_map: dict[str, Any] = metadata

            def meta_value(key: str) -> str:
                raw = metadata_map.get(key)
                if isinstance(raw, dict):
                    return _strip_html(str(raw.get("value") or ""))
                return ""

            raw_license = meta_value("LicenseShortName") or meta_value("UsageTerms")
            raw_license_url = meta_value("LicenseUrl") or None
            license_name, commercial_allowed, attribution_required = (
                _normalize_license_info(
                    source=self.name,
                    raw_license=raw_license,
                    raw_license_url=raw_license_url,
                )
            )

            duration_seconds = (
                _safe_float(meta_value("PlaytimeSeconds"), None)
                or _safe_float(meta_value("length"), None)
                or _safe_float(meta_value("Duration"), None)
            )

            results.append(
                VideoCandidate(
                    source=self.name,
                    url=url,
                    referrer_url=str(info.get("descriptionurl") or "").strip() or None,
                    query_used=query,
                    license_name=license_name,
                    license_url=raw_license_url,
                    author=meta_value("Artist") or meta_value("Credit") or None,
                    commercial_allowed=commercial_allowed,
                    attribution_required=attribution_required,
                    duration_seconds=duration_seconds,
                    width=_safe_int(info.get("width"), None),
                    height=_safe_int(info.get("height"), None),
                    fps=None,
                    file_type=mime or None,
                )
            )

        return results


def _parse_sources(value: str) -> list[str]:
    return shared_parse_sources(value, default_sources=["wikimedia"])


def _build_providers(
    source_names: list[str],
    timeout_seconds: float,
) -> list[Any]:
    providers: list[Any] = []
    for source in source_names:
        try:
            if source == "pexels":
                key = os.getenv(ENV_PEXELS_API_KEY, "").strip()
                if not key:
                    logger.warning("Pexels skipped: %s is not set", ENV_PEXELS_API_KEY)
                    continue
                providers.append(
                    PexelsVideoProvider(key, timeout_seconds=timeout_seconds)
                )
            elif source == "pixabay":
                key = os.getenv(ENV_PIXABAY_API_KEY, "").strip()
                if not key:
                    logger.warning(
                        "Pixabay skipped: %s is not set", ENV_PIXABAY_API_KEY
                    )
                    continue
                providers.append(
                    PixabayVideoProvider(key, timeout_seconds=timeout_seconds)
                )
            elif source == "wikimedia":
                providers.append(
                    WikimediaVideoProvider(timeout_seconds=timeout_seconds)
                )
            else:
                logger.warning("Unknown source '%s' skipped", source)
        except Exception as exc:
            logger.warning("Source '%s' unavailable: %s", source, exc)

    if not providers:
        raise RuntimeError(
            "No video sources are available. Check API keys and settings."
        )

    logger.info(
        "Video sources enabled: %s",
        ", ".join(getattr(p, "name", "unknown") for p in providers),
    )
    return providers


def _tokenize(text: str) -> list[str]:
    return shared_tokenize(text)


def _candidate_hint_score(keyword: str, candidate: VideoCandidate) -> float:
    return shared_candidate_hint_score(keyword, candidate)


def _build_query_variants(keyword: str, paragraph_text: str) -> list[str]:
    return shared_build_query_variants(
        keyword,
        paragraph_text,
        short_suffixes=("cinematic footage", "b-roll"),
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
        accept_header="video/*,*/*;q=0.8",
        user_agent=USER_AGENT,
    )


def _read_limited(response: Any, max_bytes: int) -> bytes:
    return shared_read_limited(
        response,
        max_bytes=max_bytes,
        payload_name="Video",
        chunk_size=128 * 1024,
    )


def _parse_frame_rate(value: object) -> float:
    return shared_parse_frame_rate(value)


def _run_command(args: list[str], timeout_seconds: float) -> tuple[bytes, bytes]:
    return shared_run_command(args, timeout_seconds)


def _probe_video(file_path: Path, timeout_seconds: float) -> dict[str, object]:
    if FFPROBE_BIN is None:
        raise RuntimeError("ffprobe binary is not configured")
    return shared_probe_video(
        file_path,
        ffprobe_bin=FFPROBE_BIN,
        timeout_seconds=timeout_seconds,
    )


def _guess_extension(content_type: str, final_url: str) -> str:
    return shared_guess_video_extension(content_type, final_url)


def _validate_video_quality(
    width: int,
    height: int,
    duration_seconds: float,
    fps: float,
    min_width: int,
    min_height: int,
    min_duration_seconds: float,
    max_duration_seconds: float,
    min_fps: float,
) -> None:
    shared_validate_video_quality(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        fps=fps,
        min_width=min_width,
        min_height=min_height,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        min_fps=min_fps,
    )


def _download_video_candidate(
    candidate: VideoCandidate,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    min_width: int,
    min_height: int,
    min_duration_seconds: float,
    max_duration_seconds: float,
    min_fps: float,
    temp_dir: Path,
) -> tuple[Path, dict[str, object]]:
    response, final_url = _open_with_safe_redirects(
        raw_url=candidate.url,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )

    with response:
        content_type = (
            (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        )
        content_type_l = content_type.lower()
        if content_type and not (
            content_type_l.startswith("video/")
            or content_type_l in {"application/octet-stream", "binary/octet-stream"}
        ):
            raise ValueError(f"Non-video Content-Type: {content_type}")

        raw_bytes = _read_limited(response, max_bytes=max_bytes)

    ext = _guess_extension(content_type, final_url)
    sha256, temp_path = write_hashed_temp_file(temp_dir, raw_bytes, ext)

    probe = _probe_video(
        temp_path,
        timeout_seconds=max(6.0, min(20.0, timeout_seconds)),
    )

    width = _safe_int(probe.get("width"), candidate.width or 0) or 0
    height = _safe_int(probe.get("height"), candidate.height or 0) or 0
    duration_seconds = (
        _safe_float(probe.get("duration_seconds"), candidate.duration_seconds or 0.0)
        or 0.0
    )
    fps = _safe_float(probe.get("fps"), candidate.fps or 0.0) or 0.0

    _validate_video_quality(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        fps=fps,
        min_width=min_width,
        min_height=min_height,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        min_fps=min_fps,
    )

    metadata = {
        "provider": candidate.source,
        "query_used": candidate.query_used,
        "url": candidate.url,
        "final_url": final_url,
        "referrer_url": candidate.referrer_url,
        "sha256": sha256,
        "content_type": content_type,
        "extension": ext,
        "width": width,
        "height": height,
        "duration_seconds": duration_seconds,
        "fps": fps,
        "bytes": len(raw_bytes),
        "codec_name": probe.get("codec_name"),
        "pix_fmt": probe.get("pix_fmt"),
        "bit_rate": probe.get("bit_rate"),
        "format_name": probe.get("format_name"),
        "license": candidate.license_name,
        "license_url": candidate.license_url,
        "author": candidate.author,
        "commercial_allowed": candidate.commercial_allowed,
        "attribution_required": candidate.attribution_required,
    }
    return temp_path, metadata


def _download_with_retries(
    candidate: VideoCandidate,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    min_width: int,
    min_height: int,
    min_duration_seconds: float,
    max_duration_seconds: float,
    min_fps: float,
    temp_dir: Path,
    retries: int = 1,
) -> tuple[Path, dict[str, object]]:
    return retry_call(
        lambda: _download_video_candidate(
            candidate=candidate,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            min_width=min_width,
            min_height=min_height,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            min_fps=min_fps,
            temp_dir=temp_dir,
        ),
        retries=retries,
        base_delay_seconds=0.45,
        error_prefix="Failed to download video after retries",
    )


def _resolve_output_path(path_value: str | Path) -> Path:
    return shared_resolve_output_json_path(path_value)


def _build_run_dir(videos_root: Path, run_prefix: str) -> tuple[str, Path]:
    return shared_build_run_dir(
        videos_root,
        run_prefix,
        default_prefix=DEFAULT_RUN_PREFIX,
        max_prefix_len=24,
    )


def _build_flat_video_path(
    run_dir: Path,
    paragraph_no: int,
    keyword_index: int,
    video_no: int,
    keyword: str,
    video_hash: str,
    extension: str,
) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", keyword or "video").strip("_").lower()
    if not slug:
        slug = "video"
    slug = slug[:28]
    short_hash = video_hash[:8] if video_hash else "unknown"

    ext = (extension or ".mp4").lower()
    if ext not in {".mp4", ".webm", ".mov", ".m4v", ".ogv"}:
        ext = ".mp4"

    filename = (
        f"p{paragraph_no:03d}_k{keyword_index:02d}_"
        f"v{video_no:02d}_{slug}_{short_hash}{ext}"
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


class VideoRelevanceCache(SharedVideoRelevanceCache):
    pass


class VideoRelevanceEvaluator:
    SAMPLER_VERSION = "uniform-v1"

    def __init__(
        self,
        model_name: str,
        threshold: float,
        max_gemini_rps: float,
        max_parallel_calls: int,
        frame_samples: int,
        frame_extract_timeout: float,
        cache: VideoRelevanceCache,
    ):
        self.model_name = model_name
        self.threshold = max(0.0, min(1.0, float(threshold)))
        self.frame_samples = max(1, int(frame_samples))
        self.frame_extract_timeout = max(1.0, float(frame_extract_timeout))
        self._limiter = SimpleRateLimiter(max_rps=max_gemini_rps)
        self._semaphore = threading.Semaphore(max(1, int(max_parallel_calls)))
        self._cache = cache
        self._local = threading.local()

        ensure_gemini_sdk_available()
        if FFMPEG_BIN is None:
            raise RuntimeError("ffmpeg binary is not configured")

    def _get_model(self) -> Any:
        model = getattr(self._local, "model", None)
        if model is None:
            model = create_gemini_model(
                api_key=_GEMINI_API_KEY,
                model_name=self.model_name,
            )
            setattr(self._local, "model", model)
        return model

    def _build_timestamps(self, duration_seconds: float) -> list[float]:
        duration = max(0.0, float(duration_seconds))
        count = max(1, self.frame_samples)
        if duration <= 0.0:
            return [0.0 for _ in range(count)]

        start = duration * 0.12
        end = duration * 0.88
        if end <= start:
            start = duration * 0.10
            end = duration * 0.90
        if end <= start:
            return [max(0.0, duration / 2.0)]

        step = (end - start) / float(count + 1)
        return [max(0.0, start + step * float(i + 1)) for i in range(count)]

    def _extract_frame_jpeg(self, video_path: Path, timestamp: float) -> bytes:
        if FFMPEG_BIN is None:
            raise RuntimeError("ffmpeg binary is not configured")

        stdout, _ = _run_command(
            [
                FFMPEG_BIN,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, timestamp):.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "6",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            timeout_seconds=self.frame_extract_timeout,
        )
        if not stdout:
            raise ValueError("Failed to extract frame")
        return bytes(stdout)

    def _extract_keyframes(
        self, video_path: Path, duration_seconds: float
    ) -> list[bytes]:
        frames: list[bytes] = []
        seen_hashes: set[str] = set()

        for ts in self._build_timestamps(duration_seconds):
            try:
                frame_bytes = self._extract_frame_jpeg(video_path, ts)
            except Exception:
                continue

            frame_hash = hashlib.sha256(frame_bytes).hexdigest()
            if frame_hash in seen_hashes:
                continue
            seen_hashes.add(frame_hash)
            frames.append(frame_bytes)

        if not frames:
            frames.append(self._extract_frame_jpeg(video_path, 0.0))

        return frames[: self.frame_samples]

    def _make_prompt(
        self,
        keyword: str,
        paragraph_text: str,
        duration_seconds: float,
        width: int,
        height: int,
        fps: float,
    ) -> str:
        paragraph_short = re.sub(r"\s+", " ", paragraph_text or "").strip()[:320]
        return (
            "You are a strict stock-video relevance judge.\n"
            "Task: determine if provided keyframes belong to a video that visually matches keyword intent.\n"
            "Return ONLY valid JSON:\n"
            '{"match": true, "score": 0.0, "reason": "short"}\n'
            "Rules:\n"
            "- Be strict and literal.\n"
            "- If uncertain, set match=false.\n"
            "- Reject UI/screenshots/memes unless keyword explicitly asks for that.\n"
            "- score must be between 0 and 1.\n"
            f"Keyword: {keyword}\n"
            f"Paragraph context: {paragraph_short or 'N/A'}\n"
            f"Video metadata: duration={duration_seconds:.2f}s, resolution={width}x{height}, fps={fps:.2f}"
        )

    def evaluate(
        self,
        keyword: str,
        paragraph_text: str,
        media_sha256: str,
        video_path: Path,
        duration_seconds: float,
        width: int,
        height: int,
        fps: float,
    ) -> VideoRelevanceResult:
        keyword_norm = re.sub(r"\s+", " ", keyword.strip()).casefold()
        cached = self._cache.get(
            keyword_norm=keyword_norm,
            media_sha256=media_sha256,
            model_name=self.model_name,
            frame_samples=self.frame_samples,
            sampler_version=self.SAMPLER_VERSION,
        )
        if cached is not None:
            is_match, score, reason = cached
            accepted = is_match and score >= self.threshold
            return VideoRelevanceResult(
                is_relevant=accepted,
                is_match=is_match,
                score=score,
                reason=reason,
                from_cache=True,
                frames_used=self.frame_samples,
            )

        transient_types = TRANSIENT_EXCEPTIONS

        last_error: Exception | None = None
        frames_used = 0
        model: Any = self._get_model()

        for attempt in range(1, 4):
            try:
                frames = self._extract_keyframes(video_path, duration_seconds)
                frames_used = len(frames)
                prompt = self._make_prompt(
                    keyword=keyword,
                    paragraph_text=paragraph_text,
                    duration_seconds=duration_seconds,
                    width=width,
                    height=height,
                    fps=fps,
                )
                payload: list[Any] = [prompt]
                payload.extend(
                    {"mime_type": "image/jpeg", "data": frame_bytes}
                    for frame_bytes in frames
                )

                with self._semaphore:
                    self._limiter.wait()
                    response = model.generate_content(payload)

                raw = str(getattr(response, "text", "") or "").strip()
                is_match, score, reason = _parse_relevance_response(raw)
                self._cache.set(
                    keyword_norm=keyword_norm,
                    media_sha256=media_sha256,
                    model_name=self.model_name,
                    frame_samples=self.frame_samples,
                    sampler_version=self.SAMPLER_VERSION,
                    is_match=is_match,
                    score=score,
                    reason=reason,
                )

                accepted = is_match and score >= self.threshold
                return VideoRelevanceResult(
                    is_relevant=accepted,
                    is_match=is_match,
                    score=score,
                    reason=reason,
                    from_cache=False,
                    frames_used=frames_used,
                )
            except Exception as exc:
                last_error = exc
                if transient_types and isinstance(exc, transient_types) and attempt < 3:
                    time.sleep(0.9 * attempt + random.uniform(0.0, 0.3))
                    continue
                if attempt < 3:
                    time.sleep(0.45 * attempt)

        reason = f"gemini_error: {last_error}"
        return VideoRelevanceResult(
            is_relevant=False,
            is_match=False,
            score=0.0,
            reason=reason[:240],
            from_cache=False,
            frames_used=frames_used,
        )


def _load_keywords_payload(path_value: str | Path) -> dict[str, object]:
    return shared_load_keywords_payload(path_value)


def _bump_provider_stat(
    provider_stats: dict[str, dict[str, int]],
    lock: threading.Lock,
    provider_name: str,
    key: str,
    amount: int = 1,
) -> None:
    shared_bump_provider_stat(provider_stats, lock, provider_name, key, amount)


def _collect_candidates_for_keyword(
    keyword: str,
    paragraph_text: str,
    providers: list[Any],
    max_candidates_per_keyword: int,
    commercial_only: bool,
    allow_attribution_licenses: bool,
    provider_stats: dict[str, dict[str, int]],
    provider_stats_lock: threading.Lock,
) -> tuple[list[VideoCandidate], list[str]]:
    errors: list[str] = []
    variants = _build_query_variants(keyword, paragraph_text)
    collected: list[VideoCandidate] = []
    seen_urls: set[str] = set()

    provider_limit = max(
        5,
        min(80, max_candidates_per_keyword // max(1, len(providers))),
    )

    for query in variants:
        if len(collected) >= max_candidates_per_keyword:
            break

        for provider in providers:
            if len(collected) >= max_candidates_per_keyword:
                break

            provider_name = str(getattr(provider, "name", "unknown"))
            _bump_provider_stat(
                provider_stats,
                provider_stats_lock,
                provider_name,
                "search_calls",
                1,
            )

            try:
                found = provider.search(query, provider_limit)
            except Exception as exc:
                _bump_provider_stat(
                    provider_stats,
                    provider_stats_lock,
                    provider_name,
                    "search_errors",
                    1,
                )
                errors.append(f"{provider_name} search failed for '{query}': {exc}")
                continue

            _bump_provider_stat(
                provider_stats,
                provider_stats_lock,
                provider_name,
                "candidates_returned",
                len(found),
            )

            for candidate in found:
                if len(collected) >= max_candidates_per_keyword:
                    break
                if candidate.url in seen_urls:
                    continue
                if not _is_license_allowed(
                    candidate,
                    commercial_only=commercial_only,
                    allow_attribution_licenses=allow_attribution_licenses,
                ):
                    continue

                candidate.rank_hint = _candidate_hint_score(keyword, candidate)
                seen_urls.add(candidate.url)
                collected.append(candidate)
                _bump_provider_stat(
                    provider_stats,
                    provider_stats_lock,
                    provider_name,
                    "candidates_kept",
                    1,
                )

    collected.sort(key=lambda item: item.rank_hint, reverse=True)
    return collected, errors


def _download_candidates_parallel(
    candidates: list[VideoCandidate],
    download_executor: ThreadPoolExecutor,
    timeout_seconds: float,
    max_bytes: int,
    max_redirects: int,
    min_width: int,
    min_height: int,
    min_duration_seconds: float,
    max_duration_seconds: float,
    min_fps: float,
    temp_dir: Path,
) -> tuple[list[tuple[Path, dict[str, object]]], int]:
    downloaded: list[tuple[Path, dict[str, object]]] = []
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
            min_duration_seconds,
            max_duration_seconds,
            min_fps,
            temp_dir,
            1,
        ): candidate
        for candidate in candidates
    }

    for future in as_completed(future_map):
        try:
            video_path, metadata = future.result()
            downloaded.append((video_path, metadata))
        except Exception:
            failed += 1

    return downloaded, failed


def _evaluate_relevance_parallel(
    downloaded_items: list[tuple[Path, dict[str, object]]],
    keyword: str,
    paragraph_text: str,
    videos_needed: int,
    relevance_executor: ThreadPoolExecutor,
    relevance_workers: int,
    evaluator: VideoRelevanceEvaluator,
    seen_hashes_global: set[str],
    seen_hashes_lock: threading.Lock | None = None,
) -> tuple[
    list[tuple[Path, dict[str, object]]],
    list[dict[str, object]],
    int,
    int,
    int,
]:
    accepted: list[tuple[Path, dict[str, object]]] = []
    rejected_examples: list[dict[str, object]] = []
    rejected_count = 0
    gemini_calls = 0
    cache_hits = 0

    queue_index = 0
    queue_limit = max(2, relevance_workers * MAX_RELEVANCE_QUEUE_FACTOR)
    pending: dict[Any, tuple[Path, dict[str, object]]] = {}

    def submit_next() -> bool:
        nonlocal queue_index
        if queue_index >= len(downloaded_items):
            return False
        video_path, metadata = downloaded_items[queue_index]
        queue_index += 1
        future = relevance_executor.submit(
            evaluator.evaluate,
            keyword,
            paragraph_text,
            str(metadata.get("sha256", "")),
            video_path,
            float(metadata.get("duration_seconds") or 0.0),
            int(metadata.get("width") or 0),
            int(metadata.get("height") or 0),
            float(metadata.get("fps") or 0.0),
        )
        pending[future] = (video_path, metadata)
        return True

    while len(pending) < queue_limit and submit_next():
        pass

    while pending:
        done_set, _ = wait(set(pending.keys()), return_when=FIRST_COMPLETED)
        for future in done_set:
            video_path, metadata = pending.pop(future)
            try:
                relevance = future.result()
            except Exception as exc:
                relevance = VideoRelevanceResult(
                    is_relevant=False,
                    is_match=False,
                    score=0.0,
                    reason=f"relevance_worker_error: {exc}",
                    from_cache=False,
                    frames_used=0,
                )

            if not relevance.from_cache:
                gemini_calls += 1
            else:
                cache_hits += 1

            metadata["relevance_match"] = relevance.is_match
            metadata["relevance_score"] = relevance.score
            metadata["relevance_reason"] = relevance.reason
            metadata["relevance_from_cache"] = relevance.from_cache
            metadata["relevance_frames_used"] = relevance.frames_used
            metadata["relevance_threshold"] = evaluator.threshold

            media_hash = str(metadata.get("sha256", ""))
            is_unique = False
            if relevance.is_relevant and media_hash:
                if seen_hashes_lock is None:
                    if media_hash not in seen_hashes_global:
                        seen_hashes_global.add(media_hash)
                        is_unique = True
                else:
                    with seen_hashes_lock:
                        if media_hash not in seen_hashes_global:
                            seen_hashes_global.add(media_hash)
                            is_unique = True

            if is_unique:
                accepted.append((video_path, metadata))
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

        if len(accepted) >= videos_needed:
            for pending_future in pending.keys():
                pending_future.cancel()
            break

        while len(pending) < queue_limit and submit_next():
            pass

    return (
        accepted[:videos_needed],
        rejected_examples,
        rejected_count,
        gemini_calls,
        cache_hits,
    )


def _cleanup_downloaded_files(
    downloaded_items: list[tuple[Path, dict[str, object]]],
) -> None:
    unique_paths: set[Path] = set()
    for video_path, _ in downloaded_items:
        unique_paths.add(video_path)

    for path in unique_paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except OSError:
            continue


def run_video_fetch(
    input_json: str | Path = DEFAULT_KEYWORDS_JSON,
    output_json: str | Path = DEFAULT_OUTPUT_JSON,
    videos_dir: str | Path = DEFAULT_VIDEOS_DIR,
    temp_root: str | Path = DEFAULT_TEMP_ROOT,
    run_prefix: str = DEFAULT_RUN_PREFIX,
    sources: str = DEFAULT_SOURCES,
    videos_per_keyword: int = DEFAULT_VIDEOS_PER_KEYWORD,
    max_candidates_per_keyword: int = DEFAULT_MAX_CANDIDATES_PER_KEYWORD,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    max_paragraphs: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    min_width: int = DEFAULT_MIN_WIDTH,
    min_height: int = DEFAULT_MIN_HEIGHT,
    min_duration_seconds: float = DEFAULT_MIN_DURATION_SECONDS,
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
    min_fps: float = DEFAULT_MIN_FPS,
    relevance_model: str = DEFAULT_RELEVANCE_MODEL,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
    keyword_workers: int = DEFAULT_KEYWORD_WORKERS,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    relevance_workers: int = DEFAULT_RELEVANCE_WORKERS,
    max_gemini_rps: float = DEFAULT_MAX_GEMINI_RPS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    frame_samples: int = DEFAULT_FRAME_SAMPLES,
    frame_extract_timeout: float = DEFAULT_FRAME_EXTRACT_TIMEOUT,
    relevance_cache_path: str | Path = DEFAULT_RELEVANCE_CACHE,
    commercial_only: bool = DEFAULT_COMMERCIAL_ONLY,
    allow_attribution_licenses: bool = DEFAULT_ALLOW_ATTRIBUTION_LICENSES,
    fail_fast: bool = False,
) -> tuple[list[dict[str, object]], Path]:
    if videos_per_keyword <= 0:
        raise ValueError("videos_per_keyword must be positive")
    if max_candidates_per_keyword <= 0:
        raise ValueError("max_candidates_per_keyword must be positive")
    if not (0.0 <= relevance_threshold <= 1.0):
        raise ValueError("relevance_threshold must be in [0, 1]")
    if keyword_workers < 1 or download_workers < 1 or relevance_workers < 1:
        raise ValueError("workers must be >= 1")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if frame_samples < 1:
        raise ValueError("frame_samples must be >= 1")
    if min_duration_seconds <= 0 or max_duration_seconds <= 0:
        raise ValueError("video duration limits must be positive")
    if min_duration_seconds > max_duration_seconds:
        raise ValueError("min_duration_seconds must be <= max_duration_seconds")

    load_dotenv()
    _ensure_runtime_dependencies()

    payload = _load_keywords_payload(input_json)
    paragraphs = _extract_paragraph_tasks(payload, max_paragraphs=max_paragraphs)
    if not paragraphs:
        raise ValueError("No paragraph video queries found in input JSON")

    gemini_key = os.getenv(ENV_GEMINI_API_KEY, "").strip()
    if not gemini_key or gemini_key.lower() == "your_gemini_api_key_here":
        raise RuntimeError(
            f"{ENV_GEMINI_API_KEY} is missing in {get_env_path()}. Create it from .env.example or set the environment variable."
        )
    _configure_genai(gemini_key)

    provider_names = _parse_sources(sources)
    providers = _build_providers(
        source_names=provider_names,
        timeout_seconds=timeout_seconds,
    )

    videos_root = Path(videos_dir)
    videos_root.mkdir(parents=True, exist_ok=True)
    run_id, run_dir = _build_run_dir(videos_root=videos_root, run_prefix=run_prefix)

    temp_base = Path(temp_root)
    temp_base.mkdir(parents=True, exist_ok=True)
    temp_run_dir = temp_base / run_id
    temp_run_dir.mkdir(parents=True, exist_ok=True)

    cache = VideoRelevanceCache(relevance_cache_path)
    evaluator = VideoRelevanceEvaluator(
        model_name=relevance_model,
        threshold=relevance_threshold,
        max_gemini_rps=max_gemini_rps,
        max_parallel_calls=relevance_workers,
        frame_samples=frame_samples,
        frame_extract_timeout=frame_extract_timeout,
        cache=cache,
    )

    paragraph_results: list[dict[str, object]] = []
    videos_by_paragraph: dict[str, dict[str, list[dict[str, object]]]] = {}

    seen_hashes: set[str] = set()
    seen_hashes_lock = threading.Lock()

    provider_stats = init_provider_stats(
        [str(getattr(provider, "name", "unknown")) for provider in providers],
        saved_key="saved_videos",
    )
    provider_stats_lock = threading.Lock()

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
    keywords_with_videos = 0
    videos_downloaded = 0
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
            provider_stats=provider_stats,
            provider_stats_lock=provider_stats_lock,
        )
        errors.extend(source_errors)

        download_failed_total = 0
        rejected_total = 0
        gemini_calls_total = 0
        cache_hits_total = 0
        checked_total = 0

        for offset in range(0, len(candidates), batch_size):
            if len(collected) >= videos_per_keyword:
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
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
                min_fps=min_fps,
                temp_dir=temp_run_dir,
            )
            download_failed_total += download_failed
            checked_total += len(downloaded_items)

            remaining = videos_per_keyword - len(collected)
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
                videos_needed=remaining,
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

            for video_path, metadata in accepted_pairs:
                if len(collected) >= videos_per_keyword:
                    break

                video_no = len(collected) + 1
                video_hash = str(metadata.get("sha256", ""))
                extension = str(metadata.get("extension") or ".mp4")
                file_path = _build_flat_video_path(
                    run_dir=run_dir,
                    paragraph_no=paragraph_no,
                    keyword_index=keyword_index,
                    video_no=video_no,
                    keyword=keyword,
                    video_hash=video_hash,
                    extension=extension,
                )
                shutil.copy2(video_path, file_path)

                metadata["video_no"] = video_no
                metadata["path"] = str(file_path)
                metadata["run_id"] = run_id
                collected.append(metadata)

            _cleanup_downloaded_files(downloaded_items)

            logger.info(
                "Progress p%s k%s '%s': accepted=%s/%s checked=%s/%s",
                paragraph_no,
                keyword_index,
                keyword,
                len(collected),
                videos_per_keyword,
                min(offset + len(batch_candidates), len(candidates)),
                len(candidates),
            )

        if len(collected) < videos_per_keyword:
            errors.append(
                f"Collected {len(collected)}/{videos_per_keyword} videos for keyword"
            )

        elapsed_keyword = round(time.perf_counter() - t_keyword_start, 2)
        logger.info(
            "Paragraph %s keyword %s '%s': %s/%s video(s), rejected=%s, sec=%.2f",
            paragraph_no,
            keyword_index,
            keyword,
            len(collected),
            videos_per_keyword,
            rejected_total,
            elapsed_keyword,
        )

        return {
            "paragraph_no": paragraph_no,
            "keyword_index": keyword_index,
            "keyword": keyword,
            "videos": collected,
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
                videos_downloaded += int(metrics.get("saved_files_count", 0))

                if len(result.get("videos", [])) > 0:
                    keywords_with_videos += 1

                if fail_fast and len(result.get("videos", [])) < videos_per_keyword:
                    for pending in future_map:
                        if not pending.done():
                            pending.cancel()
                    raise RuntimeError(
                        f"Failed to collect enough relevant videos for paragraph {paragraph_no}, "
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
                        "videos": [],
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
                        "videos": list(result.get("videos", [])),
                        "errors": list(result.get("errors", [])),
                        "metrics": dict(result.get("metrics", {})),
                        "rejected_examples": list(result.get("rejected_examples", [])),
                    }
                )
                paragraph_map[keyword] = list(result.get("videos", []))

            paragraph_results.append(
                {
                    "paragraph_no": paragraph.paragraph_no,
                    "original_index": paragraph.original_index,
                    "text": paragraph.text,
                    "keywords": keyword_results,
                }
            )
            videos_by_paragraph[str(paragraph.paragraph_no)] = paragraph_map

        total_elapsed = round(time.perf_counter() - started_at, 2)

        provider_chain = [
            getattr(provider, "name", "unknown") for provider in providers
        ]
        provider_limits: dict[str, dict[str, object]] = {}
        provider_limits = build_provider_limit_summary(
            provider_chain=provider_chain,
            provider_stats=provider_stats,
            paragraph_results=paragraph_results,
            asset_list_key="videos",
            saved_key="saved_videos",
        )

        out_file = _resolve_output_path(output_json)
        output_payload = {
            "source_keywords_file": str(Path(input_json)),
            "videos_root": str(videos_root),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "provider_chain": provider_chain,
            "videos_per_keyword": videos_per_keyword,
            "max_candidates_per_keyword": max_candidates_per_keyword,
            "relevance_model": relevance_model,
            "relevance_threshold": relevance_threshold,
            "frame_samples": frame_samples,
            "workers": {
                "keyword_workers": keyword_workers,
                "download_workers": download_workers,
                "relevance_workers": relevance_workers,
                "batch_size": batch_size,
            },
            "video_constraints": {
                "min_width": min_width,
                "min_height": min_height,
                "min_duration_seconds": min_duration_seconds,
                "max_duration_seconds": max_duration_seconds,
                "min_fps": min_fps,
                "max_bytes": max_bytes,
                "max_redirects": max_redirects,
            },
            "license_policy": {
                "commercial_only": commercial_only,
                "allow_attribution_licenses": allow_attribution_licenses,
            },
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "paragraphs_total": len(paragraph_results),
                "keywords_total": total_keywords,
                "keywords_with_videos": keywords_with_videos,
                "videos_downloaded": videos_downloaded,
                "candidates_total": total_candidates,
                "download_failures": total_download_failures,
                "rejected_by_relevance": total_rejected_by_relevance,
                "gemini_calls": total_gemini_calls,
                "cache_hits": total_cache_hits,
                "elapsed_seconds": total_elapsed,
            },
            "limits": {
                "videos": {
                    "configured": {
                        "sources": sources,
                        "videos_per_keyword": videos_per_keyword,
                        "max_candidates_per_keyword": max_candidates_per_keyword,
                        "delay_seconds": delay_seconds,
                        "max_paragraphs": max_paragraphs,
                        "timeout_seconds": timeout_seconds,
                        "max_bytes": max_bytes,
                        "max_redirects": max_redirects,
                        "min_width": min_width,
                        "min_height": min_height,
                        "min_duration_seconds": min_duration_seconds,
                        "max_duration_seconds": max_duration_seconds,
                        "min_fps": min_fps,
                        "relevance_model": relevance_model,
                        "relevance_threshold": relevance_threshold,
                        "keyword_workers": keyword_workers,
                        "download_workers": download_workers,
                        "relevance_workers": relevance_workers,
                        "max_gemini_rps": max_gemini_rps,
                        "batch_size": batch_size,
                        "frame_samples": frame_samples,
                        "frame_extract_timeout": frame_extract_timeout,
                        "relevance_cache_path": str(relevance_cache_path),
                        "commercial_only": commercial_only,
                        "allow_attribution_licenses": allow_attribution_licenses,
                    },
                    "effective": {
                        "provider_chain": provider_chain,
                        "videos_per_keyword": videos_per_keyword,
                        "max_candidates_per_keyword": max_candidates_per_keyword,
                        "timeout_seconds": timeout_seconds,
                        "max_bytes": max_bytes,
                        "max_redirects": max_redirects,
                        "min_width": min_width,
                        "min_height": min_height,
                        "min_duration_seconds": min_duration_seconds,
                        "max_duration_seconds": max_duration_seconds,
                        "min_fps": min_fps,
                        "workers": {
                            "keyword_workers": keyword_workers,
                            "download_workers": download_workers,
                            "relevance_workers": relevance_workers,
                        },
                        "max_gemini_rps": max_gemini_rps,
                        "batch_size": batch_size,
                        "frame_samples": frame_samples,
                        "frame_extract_timeout": frame_extract_timeout,
                        "relevance_threshold": relevance_threshold,
                    },
                    "usage": {
                        "keywords_total": total_keywords,
                        "keywords_with_videos": keywords_with_videos,
                        "candidates_total": total_candidates,
                        "download_failures": total_download_failures,
                        "rejected_by_relevance": total_rejected_by_relevance,
                        "videos_downloaded": videos_downloaded,
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
            "videos_by_paragraph": videos_by_paragraph,
            "items": paragraph_results,
        }

        out_file.write_text(
            json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return paragraph_results, out_file
    finally:
        cache.close()
        shutil.rmtree(temp_run_dir, ignore_errors=True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch commercial videos from free sources and keep only "
            "Gemini-verified relevant results"
        )
    )
    parser.add_argument("--input", "-i", default=DEFAULT_KEYWORDS_JSON)
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--videos-dir", default=DEFAULT_VIDEOS_DIR)
    parser.add_argument("--temp-root", default=DEFAULT_TEMP_ROOT)
    parser.add_argument("--run-prefix", default=DEFAULT_RUN_PREFIX)
    parser.add_argument("--sources", default=DEFAULT_SOURCES)

    parser.add_argument(
        "--videos-per-keyword", type=int, default=DEFAULT_VIDEOS_PER_KEYWORD
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
    parser.add_argument("--frame-samples", type=int, default=DEFAULT_FRAME_SAMPLES)
    parser.add_argument(
        "--frame-extract-timeout",
        type=float,
        default=DEFAULT_FRAME_EXTRACT_TIMEOUT,
    )
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
    parser.add_argument(
        "--min-duration",
        type=float,
        default=DEFAULT_MIN_DURATION_SECONDS,
        help="Minimum duration in seconds",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=DEFAULT_MAX_DURATION_SECONDS,
        help="Maximum duration in seconds",
    )
    parser.add_argument("--min-fps", type=float, default=DEFAULT_MIN_FPS)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    _, out_file = run_video_fetch(
        input_json=args.input,
        output_json=args.output,
        videos_dir=args.videos_dir,
        temp_root=args.temp_root,
        run_prefix=args.run_prefix,
        sources=args.sources,
        videos_per_keyword=args.videos_per_keyword,
        max_candidates_per_keyword=args.max_candidates_per_keyword,
        delay_seconds=args.delay,
        max_paragraphs=args.max_paragraphs,
        timeout_seconds=args.timeout,
        max_bytes=args.max_bytes,
        max_redirects=args.max_redirects,
        min_width=args.min_width,
        min_height=args.min_height,
        min_duration_seconds=args.min_duration,
        max_duration_seconds=args.max_duration,
        min_fps=args.min_fps,
        relevance_model=args.relevance_model,
        relevance_threshold=args.relevance_threshold,
        keyword_workers=args.keyword_workers,
        download_workers=args.download_workers,
        relevance_workers=args.relevance_workers,
        max_gemini_rps=args.max_gemini_rps,
        batch_size=args.batch_size,
        frame_samples=args.frame_samples,
        frame_extract_timeout=args.frame_extract_timeout,
        relevance_cache_path=args.relevance_cache,
        commercial_only=not args.allow_non_commercial,
        allow_attribution_licenses=args.allow_attribution_licenses,
        fail_fast=args.fail_fast,
    )
    logger.info("Done. Saved video manifest to: %s", out_file)


if __name__ == "__main__":
    main()
