from __future__ import annotations

import inspect
import io
import mimetypes
import os
import re
import urllib.error
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field, replace
from hashlib import sha1
from pathlib import Path
from threading import Lock, RLock
from time import perf_counter
from typing import Any, Callable, Protocol, Sequence, cast
from urllib.parse import urlparse

from config.settings import ConcurrencySettings, ProviderSettings, default_settings
from domain.enums import AssetKind, EventLevel, ProviderCapability, RunStage, RunStatus
from domain.models import (
    AssetCandidate,
    AssetSelection,
    LiveDownloadedFileSnapshot,
    LiveParagraphStateSnapshot,
    LiveRunStateSnapshot,
    MediaSlot,
    ParagraphDiagnostics,
    ParagraphManifestEntry,
    ParagraphUnit,
    Project,
    ProviderResult,
    Run,
    RunManifest,
    utc_now,
)
from domain.project_modes import DEFAULT_FREE_IMAGE_PROVIDER_IDS
from legacy_core.network import (
    HttpClientConfig,
    SafeHttpClient,
    open_with_safe_redirects,
    read_limited,
)
from providers.base import ProviderDescriptor
from providers.concurrency import (
    ConcurrencyModeResolution,
    ExecutionConcurrencyMode,
    resolve_execution_concurrency_mode,
)
from providers.images import (
    ImageLicensePolicy,
    ImageProviderBuildContext,
    ImageProviderSearchService,
    SearchCandidate,
    WrappedImageSearchProvider,
    build_image_provider_clients,
)
from providers.registry import ProviderRegistry
from services.errors import (
    ConfigError,
    DownloadError,
    ProviderError,
    SessionError,
)
from services.events import AppEvent, EventBus
from services.retry import (
    build_retry_profile,
    classify_retryable_exception,
    is_timeout_exception,
    sleep_for_retry_attempt,
)
from storage.repositories import ManifestRepository, ProjectRepository, RunRepository

try:
    from PIL import Image, UnidentifiedImageError
except Exception:  # pragma: no cover - Pillow is an optional runtime import
    Image = None
    UnidentifiedImageError = OSError

from .backpressure import BoundedExecutor
from .orchestrator import RunOrchestrator
from .perf import (
    PerformanceContext,
    ensure_run_performance_context,
    persist_run_performance_context,
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _supports_keyword_argument(
    fn: Callable[..., Any],
    parameter_name: str,
) -> bool:
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or parameter.name == parameter_name
        for parameter in parameters
    )


def _content_type_starts_with_image(content_type: str) -> bool:
    normalized = (content_type or "").split(";", 1)[0].strip().casefold()
    return normalized.startswith("image/")


def _validate_image_payload(
    payload: bytes, *, content_type: str = ""
) -> dict[str, object]:
    normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if normalized_type and not normalized_type.startswith("image/"):
        raise ValueError(f"Non-image Content-Type: {content_type}")
    if normalized_type == "image/svg+xml":
        raise ValueError("SVG images are not allowed")
    if not payload:
        raise ValueError("Image payload is empty")
    if Image is None:
        return {"content_type": normalized_type, "bytes": len(payload)}

    Image.MAX_IMAGE_PIXELS = 32_000_000
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ValueError("Image dimensions are invalid")
            if width * height > 32_000_000:
                raise ValueError("Image has too many pixels")
            return {
                "content_type": normalized_type,
                "bytes": len(payload),
                "width": width,
                "height": height,
                "source_format": str(image.format or "UNKNOWN"),
            }
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Invalid or unreadable image bytes: {exc}") from exc


class CandidateSearchBackend(Protocol):
    provider_id: str
    capability: ProviderCapability
    descriptor: ProviderDescriptor

    def search(
        self, paragraph: ParagraphUnit, query: str, limit: int
    ) -> ProviderResult: ...


class AssetDownloadBackend(Protocol):
    provider_id: str

    def download_asset(
        self,
        asset: AssetCandidate,
        *,
        destination_dir: Path,
        filename: str,
    ) -> AssetCandidate: ...


@dataclass(slots=True)
class VideoSelectionPolicy:
    ranked_candidate_limit: int = 24
    ranking_queue_size: int = 8
    ranking_workers: int = 2
    ranking_timeout_seconds: float = 10.0
    early_stop_quality_threshold: float = 8.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked_candidate_limit": self.ranked_candidate_limit,
            "ranking_queue_size": self.ranking_queue_size,
            "ranking_workers": self.ranking_workers,
            "ranking_timeout_seconds": self.ranking_timeout_seconds,
            "early_stop_quality_threshold": self.early_stop_quality_threshold,
        }


@dataclass(slots=True)
class MediaSelectionConfig:
    video_enabled: bool = True
    storyblocks_images_enabled: bool = True
    free_images_enabled: bool = True
    supporting_image_limit: int = 1
    fallback_image_limit: int = 1
    max_candidates_per_provider: int = 8
    provider_workers: int = 4
    provider_queue_size: int = 8
    bounded_downloads: int = 8
    download_workers: int = 4
    early_stop_when_satisfied: bool = True
    no_match_budget_seconds: float = 20.0
    search_timeout_seconds: float = 20.0
    download_timeout_seconds: float = 120.0
    retry_budget: int = 2
    fail_fast_storyblocks_errors: bool = True
    output_root: str = ""
    video_selection: VideoSelectionPolicy = field(default_factory=VideoSelectionPolicy)
    should_cancel: Callable[[], bool] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_enabled": self.video_enabled,
            "storyblocks_images_enabled": self.storyblocks_images_enabled,
            "free_images_enabled": self.free_images_enabled,
            "supporting_image_limit": self.supporting_image_limit,
            "fallback_image_limit": self.fallback_image_limit,
            "max_candidates_per_provider": self.max_candidates_per_provider,
            "provider_workers": self.provider_workers,
            "provider_queue_size": self.provider_queue_size,
            "bounded_downloads": self.bounded_downloads,
            "download_workers": self.download_workers,
            "early_stop_when_satisfied": self.early_stop_when_satisfied,
            "no_match_budget_seconds": self.no_match_budget_seconds,
            "search_timeout_seconds": self.search_timeout_seconds,
            "download_timeout_seconds": self.download_timeout_seconds,
            "retry_budget": self.retry_budget,
            "fail_fast_storyblocks_errors": self.fail_fast_storyblocks_errors,
            "output_root": self.output_root,
            "video_selection": self.video_selection.to_dict(),
        }


@dataclass(slots=True)
class CallbackCandidateSearchBackend:
    provider_id: str
    capability: ProviderCapability
    descriptor: ProviderDescriptor
    search_fn: Callable[[ParagraphUnit, str, int], list[AssetCandidate]]

    def search(
        self,
        paragraph: ParagraphUnit,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult:
        candidates = list(self.search_fn(paragraph, query, limit))
        for index, candidate in enumerate(candidates, start=1):
            candidate.provider_name = self.provider_id
            candidate.kind = (
                AssetKind.VIDEO
                if self.capability == ProviderCapability.VIDEO
                else AssetKind.IMAGE
            )
            candidate.metadata.setdefault("search_query", query)
            candidate.metadata.setdefault("rank_hint", float(max(1, limit - index + 1)))
        return ProviderResult(
            provider_name=self.provider_id,
            capability=self.capability,
            query=query,
            candidates=candidates[:limit],
        )

    @classmethod
    def empty(cls, descriptor: ProviderDescriptor) -> "CallbackCandidateSearchBackend":
        return cls(
            provider_id=descriptor.provider_id,
            capability=descriptor.capability,
            descriptor=descriptor,
            search_fn=lambda _paragraph, _query, _limit: [],
        )


@dataclass(slots=True)
class FreeImageCandidateSearchBackend:
    provider_id: str
    capability: ProviderCapability
    descriptor: ProviderDescriptor
    provider: WrappedImageSearchProvider
    image_search_service: ImageProviderSearchService
    license_policy: ImageLicensePolicy
    timeout_seconds: float = 15.0
    user_agent: str = "ParagraphMediaPipeline/1.0"
    max_download_bytes: int = 25 * 1024 * 1024
    _http_client: SafeHttpClient | None = field(default=None, init=False, repr=False)
    _owns_http_client: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        shared_client = self.provider.http_client
        if shared_client is not None:
            self._http_client = shared_client
            self._owns_http_client = False
            return
        self._http_client = SafeHttpClient(HttpClientConfig(user_agent=self.user_agent))
        self._owns_http_client = True

    def search(
        self,
        paragraph: ParagraphUnit,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult:
        effective_timeout_seconds = (
            self.timeout_seconds
            if timeout_seconds is None or timeout_seconds <= 0
            else float(timeout_seconds)
        )
        search_provider = cast(Any, self.image_search_service).search_provider
        if _supports_keyword_argument(search_provider, "timeout_seconds"):
            candidates, errors, diagnostics = search_provider(
                query,
                paragraph.text,
                self.provider,
                max_candidates_per_keyword=limit,
                license_policy=self.license_policy,
                timeout_seconds=effective_timeout_seconds,
            )
        else:
            candidates, errors, diagnostics = search_provider(
                query,
                paragraph.text,
                self.provider,
                max_candidates_per_keyword=limit,
                license_policy=self.license_policy,
            )
        return ProviderResult(
            provider_name=self.provider_id,
            capability=self.capability,
            query=query,
            candidates=[
                self._to_asset_candidate(candidate) for candidate in candidates[:limit]
            ],
            errors=errors,
            diagnostics={
                "provider_queries": diagnostics.provider_queries,
                "rejected_prefilters": diagnostics.rejected_prefilters,
                "cache_hits": diagnostics.cache_hits,
            },
        )

    @property
    def http_client(self) -> SafeHttpClient:
        assert self._http_client is not None
        return self._http_client

    def close(self) -> None:
        self.provider.close()
        if self._owns_http_client and self._http_client is not None:
            self._http_client.close()

    def _to_asset_candidate(self, candidate: SearchCandidate) -> AssetCandidate:
        digest = sha1(candidate.url.encode("utf-8")).hexdigest()[:16]
        semantic_signature = _normalize_text(
            candidate.query_used or candidate.url
        ).casefold()
        return AssetCandidate(
            asset_id=digest,
            provider_name=self.provider_id,
            kind=AssetKind.IMAGE,
            source_url=candidate.url,
            license_name=candidate.license_name,
            metadata={
                "referrer_url": candidate.referrer_url,
                "author": candidate.author,
                "commercial_allowed": candidate.commercial_allowed,
                "attribution_required": candidate.attribution_required,
                "license_url": candidate.license_url,
                "query_used": candidate.query_used,
                "rank_hint": candidate.rank_hint,
                "semantic_signature": semantic_signature,
            },
        )

    def download_asset(
        self,
        asset: AssetCandidate,
        *,
        destination_dir: Path,
        filename: str,
        timeout_seconds: float | None = None,
    ) -> AssetCandidate:
        source_url = str(asset.source_url or "").strip()
        if not source_url:
            raise DownloadError(
                code="asset_source_url_missing",
                message=f"Asset '{asset.asset_id}' has no source URL for download.",
                details={"asset_id": asset.asset_id, "provider_id": self.provider_id},
            )

        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / filename
        effective_timeout_seconds = (
            self.timeout_seconds
            if timeout_seconds is None or timeout_seconds <= 0
            else float(timeout_seconds)
        )
        try:
            response, final_url = open_with_safe_redirects(
                source_url,
                timeout_seconds=effective_timeout_seconds,
                max_redirects=4,
                accept_header="image/*,*/*;q=0.8",
                user_agent=self.user_agent,
                http_client=self.http_client,
            )
            with response:
                content_type = (
                    (response.headers.get("Content-Type") or "")
                    if hasattr(response, "headers")
                    else ""
                )
                payload = read_limited(
                    response,
                    max_bytes=self.max_download_bytes,
                    payload_name="image payload",
                    chunk_size=65536,
                )
            validation = _validate_image_payload(payload, content_type=content_type)
        except Exception as exc:
            raise self._coerce_download_error(exc, asset_id=asset.asset_id) from exc

        destination.write_bytes(payload)
        downloaded = AssetCandidate.from_dict(asset.to_dict())
        downloaded.local_path = destination
        downloaded.metadata["download_status"] = "completed"
        downloaded.metadata["final_url"] = final_url
        downloaded.metadata.update(validation)
        return downloaded

    def _coerce_download_error(
        self,
        exc: Exception,
        *,
        asset_id: str,
    ) -> DownloadError:
        if isinstance(exc, DownloadError):
            return exc

        retryable = False
        failure_kind = "unexpected"
        http_status: int | None = None

        if is_timeout_exception(exc):
            retryable = True
            failure_kind = "timeout"
        elif isinstance(exc, urllib.error.HTTPError):
            http_status = int(getattr(exc, "code", 0) or 0)
            retryable = http_status == 429 or 500 <= http_status < 600
            failure_kind = f"http_{http_status or 'unknown'}"
        elif isinstance(exc, (urllib.error.URLError, OSError)):
            retryable = True
            failure_kind = "network"
        elif isinstance(exc, ValueError):
            failure_kind = "validation"

        details: dict[str, object] = {
            "asset_id": asset_id,
            "provider_id": self.provider_id,
            "failure_kind": failure_kind,
        }
        if http_status is not None:
            details["http_status"] = http_status
        return DownloadError(
            code="direct_image_download_failed",
            message=f"Image download failed for asset '{asset_id}': {exc}",
            details=details,
            retryable=retryable,
        )


@dataclass(slots=True)
class AssetDeduper:
    parent: "AssetDeduper | None" = field(default=None, repr=False)
    source_ids: set[str] = field(default_factory=set)
    raw_hashes: set[str] = field(default_factory=set)
    perceptual_hashes: set[str] = field(default_factory=set)
    semantic_signatures: set[str] = field(default_factory=set)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    _guard: RLock | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.parent is not None and self.parent._guard is not None:
            self._guard = self.parent._guard
        else:
            self._guard = RLock()

    def child(self) -> "AssetDeduper":
        return AssetDeduper(parent=self)

    def register(self, asset: AssetCandidate) -> None:
        with self._ensure_guard():
            source_id = self._source_id(asset)
            if source_id:
                self.source_ids.add(source_id)
            raw_hash = self._raw_hash(asset)
            if raw_hash:
                self.raw_hashes.add(raw_hash)
            perceptual_hash = self._perceptual_hash(asset)
            if perceptual_hash:
                self.perceptual_hashes.add(perceptual_hash)
            semantic = self._semantic_signature(asset)
            if semantic:
                self.semantic_signatures.add(semantic)

    def filter_candidates(
        self, candidates: list[AssetCandidate]
    ) -> list[AssetCandidate]:
        with self._ensure_guard():
            accepted: list[AssetCandidate] = []
            for candidate in candidates:
                reason = self._duplicate_reason(candidate)
                if reason is not None:
                    self.rejection_counts[reason] = (
                        self.rejection_counts.get(reason, 0) + 1
                    )
                    continue
                self.register(candidate)
                accepted.append(candidate)
            return accepted

    def _duplicate_reason(self, asset: AssetCandidate) -> str | None:
        source_id = self._source_id(asset)
        if source_id and self._contains("source_ids", source_id):
            return "source_id"
        raw_hash = self._raw_hash(asset)
        if raw_hash and self._contains("raw_hashes", raw_hash):
            return "raw_file_hash"
        perceptual_hash = self._perceptual_hash(asset)
        if perceptual_hash and self._contains("perceptual_hashes", perceptual_hash):
            return "perceptual_hash"
        semantic = self._semantic_signature(asset)
        if semantic and self._contains("semantic_signatures", semantic):
            return "semantic_similarity"
        return None

    def _contains(self, bucket_name: str, value: str) -> bool:
        if value in getattr(self, bucket_name):
            return True
        parent = self.parent
        while parent is not None:
            if value in getattr(parent, bucket_name):
                return True
            parent = parent.parent
        return False

    def _ensure_guard(self) -> RLock:
        guard = self._guard
        if guard is None:
            guard = RLock()
            self._guard = guard
        return guard

    def _source_id(self, asset: AssetCandidate) -> str:
        return f"{asset.provider_name}:{asset.asset_id}"

    def _raw_hash(self, asset: AssetCandidate) -> str | None:
        value = asset.metadata.get("sha256") or asset.metadata.get("file_hash")
        if value in (None, ""):
            return None
        return _normalize_text(str(value)) or None

    def _perceptual_hash(self, asset: AssetCandidate) -> str | None:
        value = asset.metadata.get("perceptual_hash") or asset.metadata.get("phash")
        if value in (None, ""):
            return None
        return _normalize_text(str(value)) or None

    def _semantic_signature(self, asset: AssetCandidate) -> str | None:
        value = (
            asset.metadata.get("semantic_signature")
            or asset.metadata.get("title")
            or asset.source_url
            or asset.asset_id
        )
        normalized = _normalize_text(str(value)).casefold()
        return normalized or None


@dataclass(frozen=True, slots=True)
class EffectiveConcurrencyLimits:
    mode: ExecutionConcurrencyMode
    provider_workers: int
    provider_queue_size: int
    download_workers: int
    download_queue_size: int
    video_ranking_workers: int
    video_ranking_queue_size: int

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "provider_workers": self.provider_workers,
            "provider_queue_size": self.provider_queue_size,
            "download_workers": self.download_workers,
            "download_queue_size": self.download_queue_size,
            "video_ranking_workers": self.video_ranking_workers,
            "video_ranking_queue_size": self.video_ranking_queue_size,
        }


@dataclass(frozen=True, slots=True)
class ImageSearchPathResolution:
    primary_provider_ids: tuple[str, ...] = ()
    fallback_provider_ids: tuple[str, ...] = ()
    free_images_only: bool = False


class ParagraphMediaPipeline:
    def __init__(
        self,
        provider_registry: ProviderRegistry,
        manifest_repository: ManifestRepository,
        *,
        provider_settings: ProviderSettings | None = None,
        event_bus: EventBus | None = None,
    ):
        self._provider_registry = provider_registry
        self._manifest_repository = manifest_repository
        self._provider_settings = provider_settings or default_settings().providers
        self._event_bus = event_bus
        self._video_backends: dict[str, CandidateSearchBackend] = {}
        self._image_backends: dict[str, CandidateSearchBackend] = {}
        self._download_backends: dict[str, AssetDownloadBackend] = {}
        self._run_state_guard = Lock()
        self._run_locks: dict[str, RLock] = {}
        self._live_manifests: dict[str, RunManifest] = {}
        self._manifest_entry_indexes: dict[str, dict[int, ParagraphManifestEntry]] = {}
        self._manifest_index_owners: dict[str, int] = {}
        self._run_dedupers: dict[str, AssetDeduper] = {}

    def register_backend(self, backend: CandidateSearchBackend) -> None:
        if backend.capability == ProviderCapability.VIDEO:
            self._video_backends[backend.provider_id] = backend
        elif backend.capability == ProviderCapability.IMAGE:
            self._image_backends[backend.provider_id] = backend
        if hasattr(backend, "download_asset"):
            self._download_backends[backend.provider_id] = cast(
                AssetDownloadBackend, backend
            )
        else:
            self._download_backends.pop(backend.provider_id, None)

    def register_backends(self, backends: Sequence[CandidateSearchBackend]) -> None:
        for backend in backends:
            self.register_backend(backend)

    def available_free_image_provider_ids(self) -> list[str]:
        return [
            provider_id
            for provider_id in self._ordered_enabled_free_image_provider_ids()
            if provider_id in self._image_backends
        ]

    def build_default_free_image_backends(
        self,
        image_search_service: ImageProviderSearchService,
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = "ParagraphMediaPipeline/1.0",
        adult_filter_off: bool = False,
        pexels_api_key: str | None = None,
        pixabay_api_key: str | None = None,
    ) -> list[FreeImageCandidateSearchBackend]:
        self._clear_free_image_backends()
        provider_ids = self._ordered_enabled_free_image_provider_ids()
        wrapped = build_image_provider_clients(
            self._provider_registry,
            provider_ids,
            ImageProviderBuildContext(
                timeout_seconds=timeout_seconds,
                user_agent=user_agent,
                adult_filter_off=adult_filter_off,
                pexels_api_key=pexels_api_key
                or os.getenv("PEXELS_API_KEY", "").strip(),
                pixabay_api_key=pixabay_api_key
                or os.getenv("PIXABAY_API_KEY", "").strip(),
            ),
        )
        backends = [
            FreeImageCandidateSearchBackend(
                provider_id=provider.provider_id,
                capability=ProviderCapability.IMAGE,
                descriptor=provider.descriptor,
                provider=provider,
                image_search_service=image_search_service,
                license_policy=ImageLicensePolicy(
                    commercial_only=self._provider_settings.commercial_only_images,
                    allow_attribution_licenses=self._provider_settings.allow_attribution_licenses,
                ),
                timeout_seconds=timeout_seconds,
                user_agent=user_agent,
            )
            for provider in wrapped
        ]
        self.register_backends(backends)
        return backends

    def _clear_free_image_backends(self) -> None:
        removable_ids = set(DEFAULT_FREE_IMAGE_PROVIDER_IDS)
        for provider_id in removable_ids:
            backend = self._image_backends.pop(provider_id, None)
            self._download_backends.pop(provider_id, None)
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def _ordered_enabled_free_image_provider_ids(self) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for provider_id in self._provider_settings.enabled_providers:
            if (
                provider_id not in DEFAULT_FREE_IMAGE_PROVIDER_IDS
                or provider_id in seen
            ):
                continue
            seen.add(provider_id)
            ordered.append(provider_id)
        return ordered

    def close(self) -> None:
        closed_ids: set[int] = set()
        for backend in [
            *self._video_backends.values(),
            *self._image_backends.values(),
            *self._download_backends.values(),
        ]:
            backend_id = id(backend)
            if backend_id in closed_ids:
                continue
            closed_ids.add(backend_id)
            close = getattr(backend, "close", None)
            if callable(close):
                close()

    def create_manifest(
        self,
        project: Project,
        run: Run,
        paragraphs: list[ParagraphUnit],
        config: MediaSelectionConfig,
        *,
        perf_context_id: str | None = None,
    ) -> RunManifest:
        selected = set(run.selected_paragraphs) if run.selected_paragraphs else None
        entries = [
            ParagraphManifestEntry(
                paragraph_no=paragraph.paragraph_no,
                original_index=paragraph.original_index,
                text=paragraph.text,
                intent=paragraph.intent,
                query_bundle=paragraph.query_bundle,
                slots=self._build_slots(config),
                status="pending",
            )
            for paragraph in paragraphs
            if selected is None or paragraph.paragraph_no in selected
        ]
        manifest = RunManifest(
            run_id=run.run_id,
            project_id=project.project_id,
            project_name=project.name,
            paragraph_entries=entries,
            sourcing_strategy=self._sourcing_strategy_payload(config),
        )
        if perf_context_id:
            manifest.sourcing_strategy["perf_context_id"] = perf_context_id
        self._sync_manifest_entry_index(manifest)
        return self.flush_manifest(manifest)

    def register_live_manifest(self, manifest: RunManifest) -> None:
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            self._live_manifests[manifest.run_id] = manifest
            self._sync_manifest_entry_index(manifest)

    def snapshot_live_run_state(
        self, run_id: str, *, detailed_paragraph_no: int | None = None
    ) -> LiveRunStateSnapshot | None:
        run_lock = self._run_lock(run_id)
        with run_lock:
            manifest = self._live_manifests.get(run_id)
            if manifest is None:
                return None
            return self._build_live_run_state_snapshot(
                manifest, detailed_paragraph_no=detailed_paragraph_no
            )

    def snapshot_manifest(self, run_id: str) -> RunManifest | None:
        run_lock = self._run_lock(run_id)
        with run_lock:
            manifest = self._live_manifests.get(run_id)
            if manifest is not None:
                return RunManifest.from_dict(manifest.to_dict())
        return self.load_manifest(run_id)

    def load_manifest(self, run_id: str) -> RunManifest | None:
        run_lock = self._run_lock(run_id)
        with run_lock:
            manifest = self._manifest_repository.load(run_id)
            if manifest is not None:
                self._sync_manifest_entry_index(manifest)
            return manifest

    def save_manifest(self, manifest: RunManifest) -> RunManifest:
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            manifest.updated_at = utc_now()
            self._sync_manifest_entry_index(manifest)
            if manifest.run_id in self._live_manifests:
                self._live_manifests[manifest.run_id] = manifest
            return self._manifest_repository.save(manifest)

    def flush_manifest(self, manifest: RunManifest) -> RunManifest:
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            return self.save_manifest(self.update_summary(manifest))

    def release_run_state(self, run_id: str) -> None:
        with self._run_state_guard:
            self._live_manifests.pop(run_id, None)
            self._run_dedupers.pop(run_id, None)
            self._manifest_entry_indexes.pop(run_id, None)
            self._manifest_index_owners.pop(run_id, None)
            self._run_locks.pop(run_id, None)

    def process_paragraph(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        config: MediaSelectionConfig,
        *,
        perf_context: PerformanceContext | None = None,
    ) -> AssetSelection:
        paragraph_started_at = perf_counter()
        provider_search_ms = 0
        download_ms = 0
        finalize_ms = 0
        concurrency_limits = self._effective_concurrency_limits(config)
        self._raise_if_cancelled(config)
        self._ensure_run_output_dirs(manifest, config)
        self._emit(
            manifest,
            "paragraph.processing.started",
            EventLevel.INFO,
            f"Paragraph {paragraph.paragraph_no} started",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PROVIDER_SEARCH,
            payload={
                "concurrency_mode": concurrency_limits.mode.value,
                **concurrency_limits.to_payload(),
            },
        )
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            entry = self._entry_for(manifest, paragraph.paragraph_no)
            entry.status = "processing"
            entry.selection = None
            entry.diagnostics = None
            entry.rejection_reasons = []

        diagnostics = ParagraphDiagnostics(
            paragraph_no=paragraph.paragraph_no,
            provider_queries={},
            fanout_limits={
                "video_ranked_candidate_limit": (
                    config.video_selection.ranked_candidate_limit
                ),
                "bounded_downloads": config.bounded_downloads,
                "max_candidates_per_provider": config.max_candidates_per_provider,
                "provider_workers": concurrency_limits.provider_workers,
                "provider_queue_size": concurrency_limits.provider_queue_size,
                "download_workers": concurrency_limits.download_workers,
                "video_ranking_workers": concurrency_limits.video_ranking_workers,
                "video_ranking_queue_size": (
                    concurrency_limits.video_ranking_queue_size
                ),
                "concurrency_mode": concurrency_limits.mode.value,
            },
        )
        selection = AssetSelection(
            paragraph_no=paragraph.paragraph_no,
            media_slots=self._build_slots(config),
            status="processing",
        )
        run_deduper = self._run_deduper(manifest)
        deduper = run_deduper.child()
        search_started_at = perf_counter()
        provider_stage_started_at = perf_counter()

        video_results, early_video_stop = self._collect_results(
            manifest,
            paragraph,
            self._resolve_video_backends(config),
            config,
            deduper,
            concurrency_limits=concurrency_limits,
            search_started_at=search_started_at,
        )
        image_results: list[ProviderResult] = []
        early_image_stop = False
        video_download_errors: list[str] = []

        self._emit(
            manifest,
            "paragraph.relevance.started",
            EventLevel.INFO,
            f"Paragraph {paragraph.paragraph_no} evaluating ranked candidates",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.RELEVANCE,
        )
        if config.video_enabled:
            self._raise_if_cancelled(config)
            primary_video, video_download_errors = self._select_primary_video_asset(
                manifest,
                paragraph,
                video_results,
                config,
                concurrency_limits=concurrency_limits,
            )
            if primary_video is not None:
                selection.primary_asset = primary_video
                selection.reason = (
                    f"Primary video selected from {primary_video.provider_name}"
                )
                diagnostics.selected_from_provider = primary_video.provider_name
            elif video_download_errors and not (
                config.storyblocks_images_enabled or config.free_images_enabled
            ):
                raise DownloadError(
                    code="primary_video_download_failed",
                    message=video_download_errors[-1],
                    details={"errors": list(video_download_errors)},
                )

        primary_image_results, fallback_image_results, early_image_stop = (
            self._collect_image_results(
                manifest,
                paragraph,
                config,
                deduper,
                selection.primary_asset,
                concurrency_limits=concurrency_limits,
                search_started_at=search_started_at,
            )
        )
        provider_search_ms = int(
            round((perf_counter() - provider_stage_started_at) * 1000.0)
        )
        image_results.extend(primary_image_results)
        image_results.extend(fallback_image_results)

        selection.supporting_assets = self._top_assets(
            primary_image_results,
            config.supporting_image_limit,
            config=config,
            manifest=manifest,
            paragraph=paragraph,
            concurrency_limits=concurrency_limits,
            preserve_order=True,
        )
        selection.fallback_assets = self._top_assets(
            fallback_image_results,
            config.fallback_image_limit,
            config=config,
            manifest=manifest,
            paragraph=paragraph,
            concurrency_limits=concurrency_limits,
            preserve_order=True,
        )
        download_started_at = perf_counter()
        selection.primary_asset = self._download_selected_image_asset(
            manifest,
            paragraph,
            selection.primary_asset,
            config,
        )
        selection.supporting_assets = self._download_image_assets(
            manifest,
            paragraph,
            selection.supporting_assets,
            config,
            concurrency_limits=concurrency_limits,
        )
        selection.fallback_assets = self._download_image_assets(
            manifest,
            paragraph,
            selection.fallback_assets,
            config,
            concurrency_limits=concurrency_limits,
        )
        download_ms = int(round((perf_counter() - download_started_at) * 1000.0))
        selection.rejection_reasons.extend(video_download_errors)
        selection.rejection_reasons.extend(self._provider_result_issues(image_results))
        selection.rejection_reasons.extend(
            self._asset_download_issues(selection.supporting_assets)
        )
        selection.rejection_reasons.extend(
            self._asset_download_issues(selection.fallback_assets)
        )
        selection.supporting_assets = self._successful_image_assets(
            selection.supporting_assets
        )
        selection.fallback_assets = self._successful_image_assets(
            selection.fallback_assets
        )
        self._raise_if_cancelled(config)
        finalize_started_at = perf_counter()
        if (
            selection.primary_asset is not None
            and not selection.supporting_assets
            and selection.fallback_assets
        ):
            selection.reason = (
                selection.reason or "Primary video selected with fallback images"
            )
        elif selection.primary_asset is None and selection.supporting_assets:
            selection.reason = "Image-only fallback satisfied the paragraph"

        if (
            selection.primary_asset is not None
            or selection.supporting_assets
            or selection.fallback_assets
        ):
            selection.status = "completed"
        else:
            selection.status = "no_match"
            selection.reason = "No acceptable media candidates found"
        finalize_ms = int(round((perf_counter() - finalize_started_at) * 1000.0))

        diagnostics.provider_results = video_results + image_results
        diagnostics.provider_queries = {
            result.provider_name: [result.query]
            for result in diagnostics.provider_results
            if result.query
        }
        diagnostics.dedupe_rejections = dict(deduper.rejection_counts)
        with run_lock:
            self._reserve_selection_assets(manifest, selection, diagnostics)
        selection.rejection_reasons = self._unique_reason_strings(
            selection.rejection_reasons
        )
        diagnostics.rejected_reasons = list(selection.rejection_reasons)
        diagnostics.early_stop_triggered = early_video_stop or early_image_stop
        selection.provider_results = list(diagnostics.provider_results)
        selection.diagnostics = {
            "provider_queries": diagnostics.provider_queries,
            "fanout_limits": diagnostics.fanout_limits,
            "dedupe_rejections": diagnostics.dedupe_rejections,
            "early_stop_triggered": diagnostics.early_stop_triggered,
        }
        selection.rejection_reasons = list(diagnostics.rejected_reasons)
        selection.media_slots = self._slots_with_selection(
            selection.media_slots, selection
        )

        if selection.status == "no_match":
            self._emit(
                manifest,
                "paragraph.no_results",
                EventLevel.WARNING,
                f"Paragraph {paragraph.paragraph_no} produced no acceptable results",
                paragraph_no=paragraph.paragraph_no,
                stage=RunStage.RELEVANCE,
            )

        self._emit(
            manifest,
            "paragraph.relevance.completed",
            EventLevel.INFO,
            f"Paragraph {paragraph.paragraph_no} relevance evaluation finished",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.RELEVANCE,
            payload={
                "selected_primary_asset_id": selection.primary_asset.asset_id
                if selection.primary_asset is not None
                else "",
                "supporting_assets": len(selection.supporting_assets),
                "fallback_assets": len(selection.fallback_assets),
            },
        )

        candidates_found = sum(
            int(result.diagnostics.get("candidates_found", len(result.candidates)))
            for result in diagnostics.provider_results
        )
        candidates_filtered = sum(
            int(result.diagnostics.get("candidates_filtered", 0))
            for result in diagnostics.provider_results
        )
        downloaded_assets = self._count_downloaded_assets(selection)
        rejected_candidates = len(selection.rejection_reasons)
        persist_started_at = perf_counter()
        with run_lock:
            entry = self._entry_for(manifest, paragraph.paragraph_no)
            entry.intent = paragraph.intent
            entry.query_bundle = paragraph.query_bundle
            entry.selection = selection
            entry.diagnostics = diagnostics
            entry.rejection_reasons = list(selection.rejection_reasons)
            entry.status = selection.status
            entry.slots = list(selection.media_slots)
            self.flush_manifest(manifest)
        persist_ms = int(round((perf_counter() - persist_started_at) * 1000.0))
        paragraph_total_ms = int(
            round((perf_counter() - paragraph_started_at) * 1000.0)
        )
        self._emit(
            manifest,
            "paragraph.persisted",
            EventLevel.INFO,
            f"Paragraph {paragraph.paragraph_no} persisted",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PERSIST,
            provider_name=selection.primary_asset.provider_name
            if selection.primary_asset is not None
            else "",
            query=selection.primary_asset.metadata.get("search_query", "")
            if selection.primary_asset is not None
            else "",
            payload={
                "current_asset_id": selection.primary_asset.asset_id
                if selection.primary_asset is not None
                else "",
                "status": selection.status,
                "provider_search_ms": provider_search_ms,
                "download_ms": download_ms,
                "persist_ms": persist_ms,
                "finalize_ms": finalize_ms,
                "paragraph_total_ms": paragraph_total_ms,
                "candidates_found": candidates_found,
                "candidates_filtered": candidates_filtered,
                "candidates_downloaded": downloaded_assets,
                "candidates_rejected": rejected_candidates,
                "early_stop_triggered": diagnostics.early_stop_triggered,
            },
        )
        if perf_context is not None:
            perf_context.add_timing("provider_search_ms", provider_search_ms)
            perf_context.add_timing("download_ms", download_ms)
            perf_context.add_timing("persist_ms", persist_ms)
            perf_context.add_timing("paragraph_finalize_ms", finalize_ms)
            perf_context.add_timing("paragraph_total_ms", paragraph_total_ms)
            perf_context.increment("candidates_found_total", candidates_found)
            perf_context.increment("candidates_filtered_total", candidates_filtered)
            perf_context.increment("candidates_downloaded_total", downloaded_assets)
            perf_context.increment("candidates_rejected_total", rejected_candidates)
            if diagnostics.early_stop_triggered:
                perf_context.increment("early_stops_total", 1)
            if selection.status == "no_match":
                perf_context.increment("no_match_total", 1)
                reason_key = self._no_match_reason_key(selection.rejection_reasons)
                perf_context.increment(f"no_match_reason_{reason_key}", 1)
            self._emit(
                manifest,
                "paragraph.perf",
                EventLevel.INFO,
                f"Paragraph {paragraph.paragraph_no} performance metrics",
                paragraph_no=paragraph.paragraph_no,
                stage=RunStage.PERSIST,
                payload={
                    "perf_context_id": perf_context.context_id,
                    "provider_search_ms": provider_search_ms,
                    "download_ms": download_ms,
                    "persist_ms": persist_ms,
                    "finalize_ms": finalize_ms,
                    "paragraph_total_ms": paragraph_total_ms,
                    "candidates_found": candidates_found,
                    "candidates_filtered": candidates_filtered,
                    "candidates_downloaded": downloaded_assets,
                    "candidates_rejected": rejected_candidates,
                    "early_stop_triggered": diagnostics.early_stop_triggered,
                    "no_match_reason": self._no_match_reason_key(
                        selection.rejection_reasons
                    )
                    if selection.status == "no_match"
                    else "",
                },
            )
        return selection

    def record_paragraph_failure(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        exc: Exception,
        *,
        perf_context: PerformanceContext | None = None,
        paragraph_total_ms: int = 0,
    ) -> RunManifest:
        run_lock = self._run_lock(manifest.run_id)
        if perf_context is not None:
            perf_context.increment("paragraph_failures_total", 1)
            perf_context.increment(
                f"paragraph_error_{type(exc).__name__.casefold()}",
                1,
            )
            if paragraph_total_ms > 0:
                perf_context.add_timing("paragraph_total_ms", paragraph_total_ms)
        self._emit(
            manifest,
            "paragraph.processing.failed",
            EventLevel.ERROR,
            f"Paragraph {paragraph.paragraph_no} failed and was recorded",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PERSIST,
            payload={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "paragraph_total_ms": max(0, int(paragraph_total_ms)),
                "perf_context_id": perf_context.context_id
                if perf_context is not None
                else "",
            },
        )
        with run_lock:
            entry = self._entry_for(manifest, paragraph.paragraph_no)
            entry.intent = paragraph.intent
            entry.query_bundle = paragraph.query_bundle
            entry.selection = None
            entry.diagnostics = None
            entry.rejection_reasons = [str(exc)]
            entry.status = "failed"
            return self.flush_manifest(manifest)

    def _download_primary_video(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        asset: AssetCandidate,
        config: MediaSelectionConfig,
    ) -> AssetCandidate:
        if asset.kind != AssetKind.VIDEO:
            return asset
        backend = self._download_backends.get(asset.provider_name)
        if backend is None:
            return asset
        self._raise_if_cancelled(config)
        destination_dir = self._shared_video_output_dir(manifest, config)
        filename = self._asset_filename(paragraph, asset, default_extension=".mp4")
        self._emit(
            manifest,
            "asset.download.started",
            EventLevel.INFO,
            f"Downloading primary video for paragraph {paragraph.paragraph_no}",
            paragraph_no=paragraph.paragraph_no,
            provider_name=asset.provider_name,
            query=str(asset.metadata.get("search_query", "")),
            stage=RunStage.DOWNLOAD,
            payload={"current_asset_id": asset.asset_id, "filename": filename},
        )
        retry_profile = self._download_retry_profile(
            config,
            provider_id=asset.provider_name,
        )
        attempts = retry_profile.max_attempts
        downloaded: AssetCandidate | None = None
        for attempt in range(1, attempts + 1):
            started_at = perf_counter()
            elapsed_ms = 0
            try:
                downloaded = self._download_asset_with_policy(
                    backend,
                    asset,
                    destination_dir=destination_dir,
                    filename=filename,
                    config=config,
                )
                elapsed_ms = self._elapsed_ms(started_at)
                downloaded.metadata["download_attempts"] = attempt
            except Exception as exc:
                elapsed_ms = self._elapsed_ms(started_at)
                if is_timeout_exception(exc):
                    self._cleanup_provider_timeout(
                        manifest,
                        paragraph=paragraph,
                        provider_name=asset.provider_name,
                        query=str(asset.metadata.get("search_query", "")),
                        stage=RunStage.DOWNLOAD,
                        backend=backend,
                        elapsed_ms=elapsed_ms,
                    )
                decision = classify_retryable_exception(
                    exc,
                    timeout_error_code="download_timeout",
                    default_error_code="download_failed",
                )
                fatal_storyblocks_error = decision.fatal or (
                    isinstance(exc, DownloadError)
                    and config.fail_fast_storyblocks_errors
                    and self._is_fatal_storyblocks_error(
                        asset.provider_name,
                        ProviderError(
                            code=exc.code,
                            message=exc.message,
                            details=dict(exc.details),
                            retryable=exc.retryable,
                            fatal=exc.fatal,
                        ),
                        config=config,
                    )
                )
                if (
                    attempt < attempts
                    and decision.retryable
                    and not fatal_storyblocks_error
                ):
                    backoff_seconds = sleep_for_retry_attempt(retry_profile, attempt)
                    self._emit(
                        manifest,
                        "asset.download.retry",
                        EventLevel.WARNING,
                        (
                            f"Retrying video download for asset {asset.asset_id} "
                            f"({attempt}/{attempts})"
                        ),
                        paragraph_no=paragraph.paragraph_no,
                        provider_name=asset.provider_name,
                        query=str(asset.metadata.get("search_query", "")),
                        stage=RunStage.DOWNLOAD,
                        payload=self._retry_payload(
                            decision=decision,
                            attempt_count=attempt,
                            attempts_total=attempts,
                            elapsed_ms=elapsed_ms,
                            final_status="retrying",
                            elapsed_key="download_elapsed_ms",
                            backoff_seconds=backoff_seconds,
                            extra={"current_asset_id": asset.asset_id},
                        ),
                    )
                    continue
                self._emit(
                    manifest,
                    "asset.download.failed",
                    EventLevel.ERROR,
                    f"Download failed for asset {asset.asset_id}: {exc}",
                    paragraph_no=paragraph.paragraph_no,
                    provider_name=asset.provider_name,
                    query=str(asset.metadata.get("search_query", "")),
                    stage=RunStage.DOWNLOAD,
                    payload=self._retry_payload(
                        decision=decision,
                        attempt_count=attempt,
                        attempts_total=attempts,
                        elapsed_ms=elapsed_ms,
                        final_status="failed",
                        elapsed_key="download_elapsed_ms",
                        extra={"current_asset_id": asset.asset_id},
                    ),
                )
                raise
            break
        if downloaded is None:
            raise DownloadError(
                code="download_failed",
                message=f"Video download failed for asset '{asset.asset_id}'.",
            )
        self._emit(
            manifest,
            "asset.download.completed",
            EventLevel.INFO,
            f"Downloaded asset {asset.asset_id}",
            paragraph_no=paragraph.paragraph_no,
            provider_name=asset.provider_name,
            query=str(downloaded.metadata.get("search_query", "")),
            stage=RunStage.DOWNLOAD,
            payload={
                "current_asset_id": downloaded.asset_id,
                "local_path": str(downloaded.local_path or ""),
                "attempts": int(downloaded.metadata.get("download_attempts", 0) or 0),
            },
        )
        return downloaded

    def _download_selected_image_asset(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        asset: AssetCandidate | None,
        config: MediaSelectionConfig,
    ) -> AssetCandidate | None:
        if asset is None or asset.kind != AssetKind.IMAGE:
            return asset
        return self._download_image_asset(manifest, paragraph, asset, config)

    def _download_image_assets(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        assets: list[AssetCandidate],
        config: MediaSelectionConfig,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
    ) -> list[AssetCandidate]:
        if not assets:
            return []
        indexed_image_assets = [
            (index, asset)
            for index, asset in enumerate(assets)
            if asset.kind == AssetKind.IMAGE
        ]
        if not indexed_image_assets:
            return list(assets)

        if concurrency_limits.download_workers <= 1 or len(indexed_image_assets) <= 1:
            downloaded = list(assets)
            for index, asset in indexed_image_assets:
                downloaded[index] = self._download_image_asset(
                    manifest,
                    paragraph,
                    asset,
                    config,
                )
            return downloaded

        def download_one(
            item: tuple[int, AssetCandidate],
        ) -> tuple[int, AssetCandidate]:
            index, candidate = item
            return (
                index,
                self._download_image_asset(
                    manifest,
                    paragraph,
                    candidate,
                    config,
                ),
            )

        completed_by_index: dict[int, AssetCandidate] = {}
        queue_wait_total_ms = 0
        queue_depth_max = 0
        with BoundedExecutor[tuple[int, AssetCandidate], tuple[int, AssetCandidate]](
            max_workers=concurrency_limits.download_workers,
            queue_size=concurrency_limits.download_queue_size,
        ) as executor:
            pending: dict[
                Future[tuple[int, AssetCandidate]], tuple[int, AssetCandidate]
            ] = {}
            assets_iter = iter(indexed_image_assets)

            def submit_next() -> bool:
                nonlocal queue_wait_total_ms, queue_depth_max
                try:
                    item = next(assets_iter)
                except StopIteration:
                    return False
                future, stats = executor.submit_with_stats(download_one, item)
                pending[future] = item
                queue_wait_total_ms += stats.wait_ms
                queue_depth_max = max(queue_depth_max, stats.queue_depth)
                return True

            while (
                len(pending) < concurrency_limits.download_queue_size and submit_next()
            ):
                pass

            while pending:
                done, _ = wait(tuple(pending.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    _item = pending.pop(future)
                    index, downloaded_asset = future.result()
                    completed_by_index[index] = downloaded_asset
                while (
                    len(pending) < concurrency_limits.download_queue_size
                    and submit_next()
                ):
                    pass

        downloaded = list(assets)
        for index, asset in completed_by_index.items():
            downloaded[index] = asset
        self._emit(
            manifest,
            "paragraph.download.pool",
            EventLevel.INFO,
            (
                f"Paragraph {paragraph.paragraph_no} image downloads used "
                f"{concurrency_limits.download_workers} workers"
            ),
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.DOWNLOAD,
            payload={
                "download_workers": concurrency_limits.download_workers,
                "download_queue_size": concurrency_limits.download_queue_size,
                "download_queue_wait_ms_total": queue_wait_total_ms,
                "download_queue_depth_max": queue_depth_max,
                "downloaded_assets": len(indexed_image_assets),
            },
        )
        return downloaded

    def _download_image_asset(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        asset: AssetCandidate,
        config: MediaSelectionConfig,
    ) -> AssetCandidate:
        backend = self._download_backends.get(asset.provider_name)
        if backend is None:
            return asset
        self._raise_if_cancelled(config)
        destination_dir = self._shared_image_output_dir(manifest, config)
        filename = self._asset_filename(paragraph, asset, default_extension=".jpg")
        self._emit(
            manifest,
            "asset.download.started",
            EventLevel.INFO,
            f"Downloading image for paragraph {paragraph.paragraph_no}",
            paragraph_no=paragraph.paragraph_no,
            provider_name=asset.provider_name,
            query=str(asset.metadata.get("search_query", "")),
            stage=RunStage.DOWNLOAD,
            payload={"current_asset_id": asset.asset_id, "filename": filename},
        )
        retry_profile = self._download_retry_profile(
            config,
            provider_id=asset.provider_name,
        )
        attempts = retry_profile.max_attempts
        downloaded: AssetCandidate | None = None
        last_error = ""
        for attempt in range(1, attempts + 1):
            started_at = perf_counter()
            elapsed_ms = 0
            try:
                downloaded = self._download_asset_with_policy(
                    backend,
                    asset,
                    destination_dir=destination_dir,
                    filename=filename,
                    config=config,
                )
                elapsed_ms = self._elapsed_ms(started_at)
                downloaded.metadata["download_attempts"] = attempt
            except Exception as exc:
                elapsed_ms = self._elapsed_ms(started_at)
                last_error = str(exc)
                if is_timeout_exception(exc):
                    self._cleanup_provider_timeout(
                        manifest,
                        paragraph=paragraph,
                        provider_name=asset.provider_name,
                        query=str(asset.metadata.get("search_query", "")),
                        stage=RunStage.DOWNLOAD,
                        backend=backend,
                        elapsed_ms=elapsed_ms,
                    )
                decision = classify_retryable_exception(
                    exc,
                    timeout_error_code="download_timeout",
                    default_error_code="download_failed",
                )
                fatal_storyblocks_error = decision.fatal or (
                    isinstance(exc, DownloadError)
                    and config.fail_fast_storyblocks_errors
                    and self._is_fatal_storyblocks_error(
                        asset.provider_name,
                        ProviderError(
                            code=exc.code,
                            message=exc.message,
                            details=dict(exc.details),
                            retryable=exc.retryable,
                            fatal=exc.fatal,
                        ),
                        config=config,
                    )
                )
                if (
                    attempt < attempts
                    and decision.retryable
                    and not fatal_storyblocks_error
                ):
                    backoff_seconds = sleep_for_retry_attempt(retry_profile, attempt)
                    self._emit(
                        manifest,
                        "asset.download.retry",
                        EventLevel.WARNING,
                        (
                            f"Retrying image download for asset {asset.asset_id} "
                            f"({attempt}/{attempts})"
                        ),
                        paragraph_no=paragraph.paragraph_no,
                        provider_name=asset.provider_name,
                        query=str(asset.metadata.get("search_query", "")),
                        stage=RunStage.DOWNLOAD,
                        payload=self._retry_payload(
                            decision=decision,
                            attempt_count=attempt,
                            attempts_total=attempts,
                            elapsed_ms=elapsed_ms,
                            final_status="retrying",
                            elapsed_key="download_elapsed_ms",
                            backoff_seconds=backoff_seconds,
                            extra={"current_asset_id": asset.asset_id},
                        ),
                    )
                    continue
                self._emit(
                    manifest,
                    "asset.download.failed",
                    EventLevel.WARNING,
                    f"Image download failed for asset {asset.asset_id}: {exc}",
                    paragraph_no=paragraph.paragraph_no,
                    provider_name=asset.provider_name,
                    query=str(asset.metadata.get("search_query", "")),
                    stage=RunStage.DOWNLOAD,
                    payload=self._retry_payload(
                        decision=decision,
                        attempt_count=attempt,
                        attempts_total=attempts,
                        elapsed_ms=elapsed_ms,
                        final_status="failed",
                        elapsed_key="download_elapsed_ms",
                        extra={"current_asset_id": asset.asset_id},
                    ),
                )
                asset.metadata["download_status"] = "failed"
                asset.metadata["download_error"] = last_error
                asset.metadata["download_error_code"] = decision.error_code
                asset.metadata["download_attempts"] = attempt
                asset.metadata["download_retryable"] = bool(decision.retryable)
                return asset
            break
        if downloaded is None:
            asset.metadata["download_status"] = "failed"
            asset.metadata["download_error"] = last_error or "image download failed"
            asset.metadata.setdefault("download_attempts", attempts)
            return asset
        self._emit(
            manifest,
            "asset.download.completed",
            EventLevel.INFO,
            f"Downloaded asset {asset.asset_id}",
            paragraph_no=paragraph.paragraph_no,
            provider_name=asset.provider_name,
            query=str(downloaded.metadata.get("search_query", "")),
            stage=RunStage.DOWNLOAD,
            payload={
                "current_asset_id": downloaded.asset_id,
                "local_path": str(downloaded.local_path or ""),
            },
        )
        return downloaded

    def _shared_video_output_dir(
        self,
        manifest: RunManifest,
        config: MediaSelectionConfig,
    ) -> Path:
        return self._run_download_root(manifest, config) / "videos"

    def _shared_image_output_dir(
        self,
        manifest: RunManifest,
        config: MediaSelectionConfig,
    ) -> Path:
        return self._run_download_root(manifest, config) / "images"

    def _ensure_run_output_dirs(
        self,
        manifest: RunManifest,
        config: MediaSelectionConfig,
    ) -> None:
        if config.video_enabled:
            self._shared_video_output_dir(manifest, config).mkdir(
                parents=True, exist_ok=True
            )
        if config.storyblocks_images_enabled or config.free_images_enabled:
            self._shared_image_output_dir(manifest, config).mkdir(
                parents=True, exist_ok=True
            )

    def _output_dir_slug(self, value: str, *, fallback: str, limit: int) -> str:
        normalized = re.sub(r"\s+", "-", str(value or "").strip())
        slug = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
        slug = re.sub(r"[-_]+", "-", slug).strip("-.").casefold()
        if not slug:
            slug = fallback
        return slug[:limit].strip("-.") or fallback

    def _run_download_root(
        self,
        manifest: RunManifest,
        config: MediaSelectionConfig,
    ) -> Path:
        if config.output_root:
            output_name = self._output_dir_slug(
                manifest.project_name,
                fallback=manifest.run_id,
                limit=80,
            )
            root = Path(config.output_root).expanduser() / output_name
        else:
            root = self._manifest_repository.path_for(manifest.run_id).parent
        return root / "downloads"

    def _asset_filename(
        self,
        paragraph: ParagraphUnit,
        asset: AssetCandidate,
        *,
        default_extension: str,
    ) -> str:
        provider_slug = self._filename_slug(
            asset.provider_name, fallback="provider", limit=24
        )
        query_value = str(
            asset.metadata.get("search_query")
            or asset.metadata.get("query_used")
            or asset.metadata.get("title")
            or asset.asset_id
        )
        query_slug = self._filename_slug(query_value, fallback="query", limit=48)
        asset_slug = self._filename_slug(asset.asset_id, fallback="asset", limit=20)
        extension = self._asset_extension(asset, default_extension)
        return (
            f"p{paragraph.paragraph_no:03d}_"
            f"{provider_slug}_"
            f"{query_slug}_"
            f"{asset_slug}{extension}"
        )

    def _asset_extension(self, asset: AssetCandidate, default_extension: str) -> str:
        source_url = str(
            asset.source_url or asset.metadata.get("final_url", "")
        ).strip()
        if source_url:
            path = urlparse(source_url).path
            suffix = Path(path).suffix.lower()
            if suffix and len(suffix) <= 6:
                return ".jpg" if suffix == ".jpeg" else suffix
            guessed, _encoding = mimetypes.guess_type(source_url)
            if guessed:
                extension = mimetypes.guess_extension(guessed) or default_extension
                return ".jpg" if extension == ".jpe" else extension
        return default_extension

    def _filename_slug(self, value: str, *, fallback: str, limit: int) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "")).strip("-").lower()
        if not slug:
            slug = fallback
        return slug[:limit].strip("-") or fallback

    def _raise_if_cancelled(self, config: MediaSelectionConfig) -> None:
        if config.should_cancel is not None and config.should_cancel():
            raise InterruptedError("Run cancelled")

    def update_summary(self, manifest: RunManifest) -> RunManifest:
        processed = 0
        completed = 0
        no_match = 0
        failed = 0
        downloaded_video_files = 0
        downloaded_image_files = 0
        summary_config = self._summary_selection_config(manifest)
        downloads_root = self._run_download_root(manifest, summary_config).resolve(
            strict=False
        )
        videos_dir = self._shared_video_output_dir(manifest, summary_config).resolve(
            strict=False
        )
        images_dir = self._shared_image_output_dir(manifest, summary_config).resolve(
            strict=False
        )
        for entry in manifest.paragraph_entries:
            if entry.status not in {"pending", "processing"}:
                processed += 1
            if entry.status == "completed":
                completed += 1
            if entry.status == "no_match":
                no_match += 1
            if entry.status == "failed":
                failed += 1
            if entry.selection is not None:
                for asset in self._selection_assets(entry.selection):
                    if asset.local_path is None:
                        continue
                    if asset.kind == AssetKind.VIDEO:
                        downloaded_video_files += 1
                    elif asset.kind == AssetKind.IMAGE:
                        downloaded_image_files += 1
        manifest.summary = {
            "paragraphs_total": len(manifest.paragraph_entries),
            "paragraphs_processed": processed,
            "paragraphs_completed": completed,
            "paragraphs_no_match": no_match,
            "paragraphs_failed": failed,
            "downloads_root": str(downloads_root),
            "videos_dir": str(videos_dir),
            "images_dir": str(images_dir),
            "downloaded_video_files": downloaded_video_files,
            "downloaded_image_files": downloaded_image_files,
        }
        manifest.updated_at = utc_now()
        return manifest

    def _summary_selection_config(self, manifest: RunManifest) -> MediaSelectionConfig:
        stored = manifest.sourcing_strategy.get("config", {})
        output_root = ""
        if isinstance(stored, dict):
            output_root = str(stored.get("output_root", "")).strip()
        return MediaSelectionConfig(output_root=output_root)

    def _collect_results(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        backends: list[CandidateSearchBackend],
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
        search_started_at: float,
    ) -> tuple[list[ProviderResult], bool]:
        tasks = self._provider_query_tasks(paragraph, backends)
        if not tasks:
            return [], False
        parallel_allowed = (
            concurrency_limits.mode == ExecutionConcurrencyMode.FREE_IMAGES_PARALLEL
            and concurrency_limits.provider_workers > 1
            and len(tasks) > 1
        )
        if not parallel_allowed:
            return self._collect_results_sequential(
                manifest,
                paragraph,
                tasks,
                config,
                deduper,
                concurrency_limits=concurrency_limits,
                search_started_at=search_started_at,
            )
        return self._collect_results_parallel(
            manifest,
            paragraph,
            tasks,
            config,
            deduper,
            concurrency_limits=concurrency_limits,
            search_started_at=search_started_at,
        )

    def _collect_results_sequential(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        tasks: list[tuple[CandidateSearchBackend, str]],
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
        search_started_at: float,
    ) -> tuple[list[ProviderResult], bool]:
        results: list[ProviderResult] = []
        for backend, query in tasks:
            self._raise_if_cancelled(config)
            if self._search_budget_exhausted(
                manifest,
                paragraph,
                config,
                search_started_at=search_started_at,
            ):
                return results, True
            result = self._search_backend(
                manifest,
                paragraph,
                backend,
                query,
                config.max_candidates_per_provider,
                config=config,
            )
            prepared = self._prepare_provider_result(
                result,
                deduper,
                config,
                manifest=manifest,
                paragraph=paragraph,
                concurrency_limits=concurrency_limits,
            )
            results.append(prepared)
            if self._should_early_stop_video(prepared, config):
                self._emit_early_stop(
                    manifest,
                    paragraph,
                    backend.provider_id,
                    query,
                    reason="quality_threshold",
                )
                return results, True
        return results, False

    def _collect_results_parallel(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        tasks: list[tuple[CandidateSearchBackend, str]],
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
        search_started_at: float,
    ) -> tuple[list[ProviderResult], bool]:
        def run_search_task(
            item: tuple[int, tuple[CandidateSearchBackend, str]],
        ) -> tuple[int, ProviderResult]:
            index, (backend, query) = item
            return (
                index,
                self._search_backend(
                    manifest,
                    paragraph,
                    backend,
                    query,
                    config.max_candidates_per_provider,
                    config=config,
                ),
            )

        queue_wait_total_ms = 0
        queue_depth_max = 0
        early_stop = False
        ordered_results: list[ProviderResult] = []
        submission_window = self._provider_submission_window(
            concurrency_limits,
            config,
        )
        executor = BoundedExecutor[
            tuple[int, tuple[CandidateSearchBackend, str]],
            tuple[int, ProviderResult],
        ](
            max_workers=concurrency_limits.provider_workers,
            queue_size=concurrency_limits.provider_queue_size,
        )
        abandon_pending = False
        try:
            pending: dict[int, Future[tuple[int, ProviderResult]]] = {}
            next_submit = 0
            next_process = 0

            def submit_one(index: int) -> None:
                nonlocal queue_wait_total_ms, queue_depth_max
                future, stats = executor.submit_with_stats(
                    run_search_task,
                    (index, tasks[index]),
                )
                pending[index] = future
                queue_wait_total_ms += stats.wait_ms
                queue_depth_max = max(queue_depth_max, stats.queue_depth)

            while next_submit < len(tasks) and len(pending) < submission_window:
                submit_one(next_submit)
                next_submit += 1

            while next_process < len(tasks):
                if self._search_budget_exhausted(
                    manifest,
                    paragraph,
                    config,
                    search_started_at=search_started_at,
                ):
                    abandon_pending = bool(pending)
                    return ordered_results, True
                future = pending.get(next_process)
                if future is None:
                    break
                _index, result = future.result()
                pending.pop(next_process, None)
                prepared = self._prepare_provider_result(
                    result,
                    deduper,
                    config,
                    manifest=manifest,
                    paragraph=paragraph,
                    concurrency_limits=concurrency_limits,
                )
                ordered_results.append(prepared)
                backend, query = tasks[next_process]
                if self._should_early_stop_video(prepared, config):
                    self._emit_early_stop(
                        manifest,
                        paragraph,
                        backend.provider_id,
                        query,
                        reason="quality_threshold",
                    )
                    early_stop = True
                    abandon_pending = bool(pending)
                    break
                next_process += 1
                while next_submit < len(tasks) and len(pending) < submission_window:
                    submit_one(next_submit)
                    next_submit += 1
        finally:
            executor.shutdown(wait=not abandon_pending, cancel_futures=abandon_pending)

        self._emit(
            manifest,
            "paragraph.provider.pool",
            EventLevel.INFO,
            (
                f"Paragraph {paragraph.paragraph_no} provider search used "
                f"{concurrency_limits.provider_workers} workers"
            ),
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PROVIDER_SEARCH,
            payload={
                "provider_workers": concurrency_limits.provider_workers,
                "provider_queue_size": concurrency_limits.provider_queue_size,
                "provider_queue_wait_ms_total": queue_wait_total_ms,
                "provider_queue_depth_max": queue_depth_max,
                "search_tasks_total": len(tasks),
            },
        )
        return ordered_results, early_stop

    def _collect_image_results(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        primary_video: AssetCandidate | None,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
        search_started_at: float,
    ) -> tuple[list[ProviderResult], list[ProviderResult], bool]:
        image_paths = self._resolve_image_search_paths(config)
        primary_backends = self._ordered_image_backends(
            image_paths.primary_provider_ids
        )
        fallback_backends = self._ordered_image_backends(
            image_paths.fallback_provider_ids
        )

        primary_results = self._collect_image_result_list(
            manifest,
            paragraph,
            primary_backends,
            config,
            deduper,
            concurrency_limits=concurrency_limits,
            search_started_at=search_started_at,
            target_limit=max(0, int(config.supporting_image_limit)),
        )
        fallback_results: list[ProviderResult] = []
        early_stop = False
        primary_limit = max(0, int(config.supporting_image_limit))
        fallback_limit = max(0, int(config.fallback_image_limit))
        if (
            config.early_stop_when_satisfied
            and primary_limit > 0
            and self._total_candidates(primary_results) >= primary_limit
            and (not config.free_images_enabled or fallback_limit <= 0)
        ):
            self._emit_early_stop(
                manifest,
                paragraph,
                primary_results[0].provider_name if primary_results else "",
                "",
                reason="slots_filled",
            )
            return primary_results, [], True

        if fallback_backends and (not early_stop or primary_video is None):
            fallback_results = self._collect_image_result_list(
                manifest,
                paragraph,
                fallback_backends,
                config,
                deduper,
                concurrency_limits=concurrency_limits,
                search_started_at=search_started_at,
                target_limit=fallback_limit,
            )
            if (
                fallback_limit > 0
                and self._total_candidates(fallback_results) >= fallback_limit
                and config.early_stop_when_satisfied
            ):
                self._emit_early_stop(
                    manifest,
                    paragraph,
                    fallback_results[0].provider_name if fallback_results else "",
                    "",
                    reason="fallback_slots_filled",
                )
                early_stop = True

        return primary_results, fallback_results, early_stop

    def _resolve_image_search_paths(
        self,
        config: MediaSelectionConfig,
    ) -> ImageSearchPathResolution:
        free_provider_ids = tuple(
            provider_id
            for provider_id in self._ordered_enabled_free_image_provider_ids()
            if config.free_images_enabled
        )
        storyblocks_enabled = (
            config.storyblocks_images_enabled
            and "storyblocks_image" in self._provider_settings.enabled_providers
        )
        free_images_only = (
            bool(self._provider_settings.free_images_only)
            or self._provider_settings.project_mode == "free_images_only"
        )

        # Image contract:
        # 1. Storyblocks images are always the primary image path when enabled.
        # 2. Free image providers run only as fallback after Storyblocks.
        # 3. `free_images_only` is the explicit exception that promotes free providers
        #    into the primary image path.
        if free_images_only and not storyblocks_enabled:
            return ImageSearchPathResolution(
                primary_provider_ids=free_provider_ids,
                fallback_provider_ids=(),
                free_images_only=True,
            )
        return ImageSearchPathResolution(
            primary_provider_ids=(
                ("storyblocks_image",) if storyblocks_enabled else ()
            ),
            fallback_provider_ids=free_provider_ids,
            free_images_only=False,
        )

    def _ordered_image_backends(
        self,
        provider_ids: Sequence[str],
    ) -> list[CandidateSearchBackend]:
        return [
            self._image_backends[provider_id]
            for provider_id in provider_ids
            if provider_id in self._image_backends
        ]

    def _collect_image_result_list(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        backends: list[CandidateSearchBackend],
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
        search_started_at: float,
        target_limit: int,
    ) -> list[ProviderResult]:
        tasks = self._provider_query_tasks(paragraph, backends)
        if not tasks:
            return []
        parallel_allowed = (
            concurrency_limits.mode == ExecutionConcurrencyMode.FREE_IMAGES_PARALLEL
            and concurrency_limits.provider_workers > 1
            and len(tasks) > 1
        )
        results: list[ProviderResult] = []
        should_early_stop = (
            config.early_stop_when_satisfied and max(0, int(target_limit)) > 0
        )
        if not parallel_allowed:
            for backend, query in tasks:
                self._raise_if_cancelled(config)
                if self._search_budget_exhausted(
                    manifest,
                    paragraph,
                    config,
                    search_started_at=search_started_at,
                ):
                    return results
                result = self._search_backend(
                    manifest,
                    paragraph,
                    backend,
                    query,
                    config.max_candidates_per_provider,
                    config=config,
                )
                results.append(
                    self._prepare_provider_result(
                        result,
                        deduper,
                        config,
                        manifest=manifest,
                        paragraph=paragraph,
                        concurrency_limits=concurrency_limits,
                    )
                )
                if should_early_stop and self._total_candidates(results) >= max(
                    0, int(target_limit)
                ):
                    return results
            return results

        queue_wait_total_ms = 0
        queue_depth_max = 0
        submission_window = self._provider_submission_window(
            concurrency_limits,
            config,
        )
        executor = BoundedExecutor[
            tuple[int, tuple[CandidateSearchBackend, str]],
            tuple[int, ProviderResult],
        ](
            max_workers=concurrency_limits.provider_workers,
            queue_size=concurrency_limits.provider_queue_size,
        )
        abandon_pending = False
        try:
            pending: dict[int, Future[tuple[int, ProviderResult]]] = {}
            next_submit = 0
            next_process = 0

            def run_search_task(
                item: tuple[int, tuple[CandidateSearchBackend, str]],
            ) -> tuple[int, ProviderResult]:
                index, (backend, query) = item
                return (
                    index,
                    self._search_backend(
                        manifest,
                        paragraph,
                        backend,
                        query,
                        config.max_candidates_per_provider,
                        config=config,
                    ),
                )

            def submit_one(index: int) -> None:
                nonlocal queue_wait_total_ms, queue_depth_max
                future, stats = executor.submit_with_stats(
                    run_search_task,
                    (index, tasks[index]),
                )
                pending[index] = future
                queue_wait_total_ms += stats.wait_ms
                queue_depth_max = max(queue_depth_max, stats.queue_depth)

            while next_submit < len(tasks) and len(pending) < submission_window:
                submit_one(next_submit)
                next_submit += 1

            while next_process < len(tasks):
                if self._search_budget_exhausted(
                    manifest,
                    paragraph,
                    config,
                    search_started_at=search_started_at,
                ):
                    abandon_pending = bool(pending)
                    break
                future = pending.get(next_process)
                if future is None:
                    break
                _index, result = future.result()
                pending.pop(next_process, None)
                results.append(
                    self._prepare_provider_result(
                        result,
                        deduper,
                        config,
                        manifest=manifest,
                        paragraph=paragraph,
                        concurrency_limits=concurrency_limits,
                    )
                )
                next_process += 1
                while next_submit < len(tasks) and len(pending) < submission_window:
                    submit_one(next_submit)
                    next_submit += 1
                if should_early_stop and self._total_candidates(results) >= max(
                    0, int(target_limit)
                ):
                    abandon_pending = bool(pending)
                    break
        finally:
            executor.shutdown(wait=not abandon_pending, cancel_futures=abandon_pending)

        self._emit(
            manifest,
            "paragraph.provider.pool",
            EventLevel.INFO,
            (
                f"Paragraph {paragraph.paragraph_no} image search used "
                f"{concurrency_limits.provider_workers} workers"
            ),
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PROVIDER_SEARCH,
            payload={
                "provider_workers": concurrency_limits.provider_workers,
                "provider_queue_size": concurrency_limits.provider_queue_size,
                "provider_queue_wait_ms_total": queue_wait_total_ms,
                "provider_queue_depth_max": queue_depth_max,
                "search_tasks_total": len(tasks),
            },
        )
        return results

    def _provider_query_tasks(
        self,
        paragraph: ParagraphUnit,
        backends: list[CandidateSearchBackend],
    ) -> list[tuple[CandidateSearchBackend, str]]:
        tasks: list[tuple[CandidateSearchBackend, str]] = []
        for backend in backends:
            tasks.extend(
                (backend, query)
                for query in self._queries_for_backend(paragraph, backend)
            )
        return tasks

    def _prepare_provider_result(
        self,
        result: ProviderResult,
        deduper: AssetDeduper,
        config: MediaSelectionConfig,
        *,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        concurrency_limits: EffectiveConcurrencyLimits,
    ) -> ProviderResult:
        raw_candidates = list(result.candidates)
        filtered = deduper.filter_candidates(raw_candidates)
        if result.capability == ProviderCapability.IMAGE:
            result.candidates = filtered
        else:
            ranked = self._rank_video_candidates(
                filtered,
                config,
                manifest=manifest,
                paragraph=paragraph,
                concurrency_limits=concurrency_limits,
            )
            result.candidates = ranked[: config.video_selection.ranked_candidate_limit]
        result.diagnostics["candidates_found"] = len(raw_candidates)
        result.diagnostics["candidates_filtered"] = len(raw_candidates) - len(filtered)
        return result

    def _should_early_stop_video(
        self,
        result: ProviderResult,
        config: MediaSelectionConfig,
    ) -> bool:
        if not config.early_stop_when_satisfied:
            return False
        if result.capability != ProviderCapability.VIDEO:
            return False
        if not result.candidates:
            return False
        return (
            self._asset_rank(result.candidates[0])
            >= config.video_selection.early_stop_quality_threshold
        )

    def _emit_early_stop(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        provider_name: str,
        query: str,
        *,
        reason: str,
    ) -> None:
        self._emit(
            manifest,
            "paragraph.early_stop",
            EventLevel.INFO,
            f"Paragraph {paragraph.paragraph_no} stopped early ({reason})",
            paragraph_no=paragraph.paragraph_no,
            provider_name=provider_name,
            query=query,
            stage=RunStage.PROVIDER_SEARCH,
            payload={"reason": reason},
        )

    def _rank_video_candidates(
        self,
        candidates: list[AssetCandidate],
        config: MediaSelectionConfig,
        *,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        concurrency_limits: EffectiveConcurrencyLimits,
    ) -> list[AssetCandidate]:
        if not candidates:
            return []
        if (
            concurrency_limits.video_ranking_workers <= 1
            or len(candidates) <= 1
            or concurrency_limits.video_ranking_queue_size <= 1
        ):
            ranked = list(candidates)
            ranked.sort(key=self._asset_rank, reverse=True)
            return ranked

        def score_candidate(item: tuple[int, AssetCandidate]) -> tuple[int, float]:
            index, candidate = item
            return index, self._asset_rank(candidate)

        dropped_tasks = 0
        timed_out_tasks = 0
        wait_ms_total = 0
        queue_depth_max = 0
        scores: dict[int, float] = {}
        submitted = 0
        completed = 0
        timed_out = False
        relevance_started_at = perf_counter()
        executor = BoundedExecutor[tuple[int, AssetCandidate], tuple[int, float]](
            max_workers=concurrency_limits.video_ranking_workers,
            queue_size=concurrency_limits.video_ranking_queue_size,
        )
        try:
            pending: dict[Future[tuple[int, float]], int] = {}
            for index, candidate in enumerate(candidates):
                self._raise_if_cancelled(config)
                maybe_submission = executor.try_submit(
                    score_candidate, (index, candidate)
                )
                if maybe_submission is None:
                    dropped_tasks += 1
                    scores[index] = self._raw_asset_rank(candidate)
                    continue
                future, stats = maybe_submission
                pending[future] = index
                submitted += 1
                wait_ms_total += stats.wait_ms
                queue_depth_max = max(queue_depth_max, stats.queue_depth)

            while pending:
                timeout = self._remaining_timeout_seconds(
                    config.video_selection.ranking_timeout_seconds,
                    started_at=relevance_started_at,
                )
                if timeout == 0.0:
                    timed_out = True
                    break
                done, _ = wait(
                    tuple(pending.keys()),
                    timeout=timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    break
                for future in done:
                    _index = pending.pop(future)
                    try:
                        ranked_index, score = future.result()
                    except Exception:
                        ranked_index = _index
                        score = self._raw_asset_rank(candidates[_index])
                    scores[ranked_index] = score
                    completed += 1
            if timed_out:
                timed_out_tasks = len(pending)
                for future, index in list(pending.items()):
                    future.cancel()
                    scores.setdefault(index, self._raw_asset_rank(candidates[index]))
        finally:
            executor.shutdown(wait=not timed_out, cancel_futures=timed_out)

        ranked = list(enumerate(candidates))
        ranked.sort(
            key=lambda item: (
                float(scores.get(item[0], self._raw_asset_rank(item[1]))),
                -item[0],
            ),
            reverse=True,
        )
        if dropped_tasks or timed_out_tasks:
            self._emit(
                manifest,
                "paragraph.video_ranking.degraded",
                EventLevel.WARNING,
                (
                    f"Paragraph {paragraph.paragraph_no} video ranking queue overloaded: "
                    f"{dropped_tasks + timed_out_tasks} tasks downgraded"
                ),
                paragraph_no=paragraph.paragraph_no,
                stage=RunStage.RELEVANCE,
                payload={
                    "video_ranking_workers": (concurrency_limits.video_ranking_workers),
                    "video_ranking_queue_size": (
                        concurrency_limits.video_ranking_queue_size
                    ),
                    "video_ranking_tasks_submitted": submitted,
                    "video_ranking_tasks_completed": completed,
                    "video_ranking_tasks_dropped": dropped_tasks,
                    "video_ranking_tasks_timed_out": timed_out_tasks,
                    "video_ranking_queue_wait_ms_total": wait_ms_total,
                    "video_ranking_queue_depth_max": queue_depth_max,
                    "video_ranking_timed_out": timed_out,
                    "video_ranking_timeout_seconds": float(
                        config.video_selection.ranking_timeout_seconds
                    ),
                },
            )
        return [item[1] for item in ranked]

    def _search_backend(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        backend: CandidateSearchBackend,
        query: str,
        limit: int,
        *,
        config: MediaSelectionConfig,
    ) -> ProviderResult:
        self._emit(
            manifest,
            "provider.search.started",
            EventLevel.INFO,
            f"Provider {backend.provider_id} searching paragraph {paragraph.paragraph_no}",
            paragraph_no=paragraph.paragraph_no,
            provider_name=backend.provider_id,
            query=query,
            stage=RunStage.PROVIDER_SEARCH,
        )
        retry_profile = self._search_retry_profile(config)
        attempts = retry_profile.max_attempts
        for attempt in range(1, attempts + 1):
            started_at = perf_counter()
            elapsed_ms = 0
            try:
                result = self._search_backend_with_timeout(
                    backend,
                    paragraph,
                    query,
                    limit,
                    config=config,
                )
                elapsed_ms = self._elapsed_ms(started_at)
            except Exception as exc:
                if not (
                    is_timeout_exception(exc)
                    or isinstance(exc, (ConfigError, ProviderError, SessionError))
                ):
                    raise
                elapsed_ms = self._elapsed_ms(started_at)
                if is_timeout_exception(exc):
                    self._cleanup_provider_timeout(
                        manifest,
                        paragraph=paragraph,
                        provider_name=backend.provider_id,
                        query=query,
                        stage=RunStage.PROVIDER_SEARCH,
                        backend=backend,
                        elapsed_ms=elapsed_ms,
                    )
                decision = classify_retryable_exception(
                    exc,
                    timeout_error_code="search_timeout",
                    default_error_code="provider_search_failed",
                )
                fatal_storyblocks_error = bool(decision.fatal)
                if isinstance(exc, (ConfigError, ProviderError, SessionError)):
                    fatal_storyblocks_error = fatal_storyblocks_error or (
                        config.fail_fast_storyblocks_errors
                        and self._is_fatal_storyblocks_error(
                            backend.provider_id,
                            exc,
                            config=config,
                        )
                    )
                if (
                    is_timeout_exception(exc)
                    and not fatal_storyblocks_error
                    and config.fail_fast_storyblocks_errors
                    and self._should_fail_fast_storyblocks_provider(
                        backend.provider_id,
                        config,
                    )
                    and attempt >= attempts
                ):
                    raise ProviderError(
                        code="search_timeout",
                        message=str(exc),
                        details={
                            "provider_id": backend.provider_id,
                            "search_elapsed_ms": elapsed_ms,
                            "attempt_count": attempt,
                        },
                    ) from exc
                if (
                    attempt < attempts
                    and decision.retryable
                    and not fatal_storyblocks_error
                ):
                    backoff_seconds = sleep_for_retry_attempt(retry_profile, attempt)
                    self._emit(
                        manifest,
                        "provider.search.retry",
                        EventLevel.WARNING,
                        (
                            f"Provider {backend.provider_id} retrying paragraph "
                            f"{paragraph.paragraph_no} ({attempt}/{attempts})"
                        ),
                        paragraph_no=paragraph.paragraph_no,
                        provider_name=backend.provider_id,
                        query=query,
                        stage=RunStage.PROVIDER_SEARCH,
                        payload=self._retry_payload(
                            decision=decision,
                            attempt_count=attempt,
                            attempts_total=attempts,
                            elapsed_ms=elapsed_ms,
                            final_status="retrying",
                            elapsed_key="search_elapsed_ms",
                            backoff_seconds=backoff_seconds,
                        ),
                    )
                    continue
                self._emit(
                    manifest,
                    "provider.search.failed"
                    if fatal_storyblocks_error
                    else "provider.search.warning",
                    EventLevel.ERROR if fatal_storyblocks_error else EventLevel.WARNING,
                    str(exc),
                    paragraph_no=paragraph.paragraph_no,
                    provider_name=backend.provider_id,
                    query=query,
                    stage=RunStage.PROVIDER_SEARCH,
                    payload=self._retry_payload(
                        decision=decision,
                        attempt_count=attempt,
                        attempts_total=attempts,
                        elapsed_ms=elapsed_ms,
                        final_status="failed",
                        elapsed_key="search_elapsed_ms",
                    ),
                )
                if fatal_storyblocks_error:
                    raise
                return ProviderResult(
                    provider_name=backend.provider_id,
                    capability=backend.capability,
                    query=query,
                    candidates=[],
                    errors=[str(exc)],
                    diagnostics=self._retry_payload(
                        decision=decision,
                        attempt_count=attempt,
                        attempts_total=attempts,
                        elapsed_ms=elapsed_ms,
                        final_status="failed",
                        elapsed_key="search_elapsed_ms",
                    ),
                )

            result.diagnostics.setdefault("attempts", attempt)
            result.diagnostics.setdefault("attempt_count", attempt)
            result.diagnostics.setdefault("retryable", False)
            result.diagnostics.setdefault("final_status", "completed")
            result.diagnostics.setdefault("error_code", "")
            result.diagnostics.setdefault("search_elapsed_ms", elapsed_ms)
            result.diagnostics.setdefault("elapsed_ms", elapsed_ms)
            break
        else:
            raise RuntimeError("Unreachable provider search retry loop")

        elapsed_ms = int(result.diagnostics.get("search_elapsed_ms", 0))
        result.diagnostics.setdefault("search_elapsed_ms", elapsed_ms)
        result.diagnostics.setdefault("elapsed_ms", elapsed_ms)
        if result.errors:
            self._emit(
                manifest,
                "provider.search.warning",
                EventLevel.WARNING,
                result.errors[0],
                paragraph_no=paragraph.paragraph_no,
                provider_name=backend.provider_id,
                query=query,
                stage=RunStage.PROVIDER_SEARCH,
                payload={
                    "search_elapsed_ms": elapsed_ms,
                    "elapsed_ms": elapsed_ms,
                    "attempt_count": int(result.diagnostics.get("attempt_count", 1)),
                    "error_code": str(result.diagnostics.get("error_code", "")),
                    "final_status": str(result.diagnostics.get("final_status", "")),
                },
            )
        if not result.candidates:
            self._emit(
                manifest,
                "provider.search.empty",
                EventLevel.WARNING,
                f"Provider {backend.provider_id} returned no results for paragraph {paragraph.paragraph_no}",
                paragraph_no=paragraph.paragraph_no,
                provider_name=backend.provider_id,
                query=query,
                stage=RunStage.PROVIDER_SEARCH,
                payload={
                    "search_elapsed_ms": elapsed_ms,
                    "elapsed_ms": elapsed_ms,
                    "attempt_count": int(result.diagnostics.get("attempt_count", 1)),
                    "error_code": str(result.diagnostics.get("error_code", "")),
                    "final_status": str(result.diagnostics.get("final_status", "")),
                },
            )
        self._emit(
            manifest,
            "provider.search.completed",
            EventLevel.INFO,
            f"Provider {backend.provider_id} completed search for paragraph {paragraph.paragraph_no}",
            paragraph_no=paragraph.paragraph_no,
            provider_name=backend.provider_id,
            query=query,
            stage=RunStage.PROVIDER_SEARCH,
            payload={
                "candidates_found": len(result.candidates),
                "search_elapsed_ms": elapsed_ms,
                "elapsed_ms": elapsed_ms,
                "attempt_count": int(result.diagnostics.get("attempt_count", 1)),
                "final_status": str(
                    result.diagnostics.get("final_status", "completed")
                ),
                "error_code": str(result.diagnostics.get("error_code", "")),
                "current_asset_id": result.candidates[0].asset_id
                if result.candidates
                else "",
            },
        )
        return result

    def _retry_attempts(self, config: MediaSelectionConfig) -> int:
        return max(1, int(config.retry_budget) + 1)

    def _search_retry_profile(self, config: MediaSelectionConfig):
        return build_retry_profile(
            config.retry_budget,
            base_delay_seconds=0.05,
            max_delay_seconds=0.5,
            jitter_seconds=0.02,
        )

    def _download_retry_profile(
        self,
        config: MediaSelectionConfig,
        *,
        provider_id: str,
    ):
        retry_budget = (
            0 if self._is_storyblocks_provider(provider_id) else config.retry_budget
        )
        return build_retry_profile(
            retry_budget,
            base_delay_seconds=0.1,
            max_delay_seconds=1.0,
            jitter_seconds=0.05,
        )

    def _retry_payload(
        self,
        *,
        decision,
        attempt_count: int,
        attempts_total: int,
        elapsed_ms: int,
        final_status: str,
        elapsed_key: str,
        backoff_seconds: float = 0.0,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt": int(attempt_count),
            "attempt_count": int(attempt_count),
            "retry_budget": max(0, int(attempts_total) - 1),
            "retryable": bool(decision.retryable),
            "final_status": final_status,
            "error_code": str(decision.error_code),
            "elapsed_ms": int(elapsed_ms),
            elapsed_key: int(elapsed_ms),
        }
        if backoff_seconds > 0:
            payload["backoff_ms"] = int(round(backoff_seconds * 1000.0))
        payload.update(dict(decision.details))
        if extra:
            payload.update(extra)
        return payload

    def _is_storyblocks_provider(self, provider_id: str) -> bool:
        return provider_id in {"storyblocks_video", "storyblocks_image"}

    def _should_fail_fast_storyblocks_provider(
        self,
        provider_id: str,
        config: MediaSelectionConfig,
    ) -> bool:
        if provider_id == "storyblocks_video":
            return True
        if provider_id == "storyblocks_image":
            return not config.free_images_enabled
        return False

    def _is_fatal_storyblocks_error(
        self,
        provider_id: str,
        exc: ConfigError | ProviderError | SessionError,
        *,
        config: MediaSelectionConfig,
    ) -> bool:
        if not self._should_fail_fast_storyblocks_provider(provider_id, config):
            return False
        if isinstance(exc, (ConfigError, SessionError)):
            return True
        if not isinstance(exc, ProviderError):
            return False
        fatal_codes = {
            "storyblocks_profile_missing",
            "storyblocks_session_not_ready",
            "storyblocks_login_required",
            "storyblocks_challenge_detected",
            "storyblocks_session_expired",
            "storyblocks_blocked",
            "storyblocks_page_unavailable",
            "storyblocks_detail_url_missing",
        }
        return str(exc.code).strip().casefold() in fatal_codes

    def _download_asset_with_policy(
        self,
        backend: AssetDownloadBackend,
        asset: AssetCandidate,
        *,
        destination_dir: Path,
        filename: str,
        config: MediaSelectionConfig,
    ) -> AssetCandidate:
        storyblocks_policy = self._storyblocks_operation_policy_for_backend(
            backend, asset.provider_name, config
        )
        if storyblocks_policy is not None and self._supports_keyword_argument(
            cast(Any, backend).download_asset,
            "operation_policy",
        ):
            return cast(Any, backend).download_asset(
                asset,
                destination_dir=destination_dir,
                filename=filename,
                operation_policy=storyblocks_policy,
            )
        if self._supports_keyword_argument(
            cast(Any, backend).download_asset,
            "timeout_seconds",
        ):
            return cast(Any, backend).download_asset(
                asset,
                destination_dir=destination_dir,
                filename=filename,
                timeout_seconds=config.download_timeout_seconds,
            )
        return backend.download_asset(
            asset,
            destination_dir=destination_dir,
            filename=filename,
        )

    def _search_backend_with_timeout(
        self,
        backend: CandidateSearchBackend,
        paragraph: ParagraphUnit,
        query: str,
        limit: int,
        *,
        config: MediaSelectionConfig,
    ) -> ProviderResult:
        search = cast(Any, backend).search
        if self._supports_keyword_argument(search, "timeout_seconds"):
            return cast(
                ProviderResult,
                search(
                    paragraph,
                    query,
                    limit,
                    timeout_seconds=config.search_timeout_seconds,
                ),
            )
        return backend.search(paragraph, query, limit)

    def _cleanup_provider_timeout(
        self,
        manifest: RunManifest,
        *,
        paragraph: ParagraphUnit,
        provider_name: str,
        query: str,
        stage: RunStage,
        backend: object,
        elapsed_ms: int,
    ) -> None:
        if not self._is_storyblocks_provider(provider_name):
            return
        session_manager = getattr(backend, "session_manager", None)
        reset_session_state = getattr(session_manager, "reset_session_state", None)
        if not callable(reset_session_state):
            return
        cleanup_status = "completed"
        cleanup_error = ""
        level = EventLevel.WARNING
        try:
            reset_session_state()
        except Exception as exc:
            cleanup_status = "failed"
            cleanup_error = str(exc)
            level = EventLevel.ERROR
        self._emit(
            manifest,
            "provider.timeout.cleaned_up",
            level,
            (
                f"Storyblocks timeout cleanup {cleanup_status} for provider "
                f"{provider_name}"
            ),
            paragraph_no=paragraph.paragraph_no,
            provider_name=provider_name,
            query=query,
            stage=stage,
            payload={
                "timeout_stage": stage.value,
                "cleanup_action": "reset_session_state",
                "cleanup_status": cleanup_status,
                "elapsed_ms": elapsed_ms,
                "cleanup_error": cleanup_error,
            },
        )

    def _supports_keyword_argument(
        self,
        fn: Callable[..., Any],
        parameter_name: str,
    ) -> bool:
        return _supports_keyword_argument(fn, parameter_name)

    def _storyblocks_operation_policy_kwargs(
        self,
        config: MediaSelectionConfig,
    ) -> dict[str, object]:
        return {
            "search_timeout_seconds": max(0.0, float(config.search_timeout_seconds)),
            "download_retries": 0,
            "download_timeout_seconds": max(
                1.0, float(config.download_timeout_seconds)
            ),
        }

    def _instantiate_storyblocks_operation_policy(
        self,
        policy_type: type[object],
        *,
        config: MediaSelectionConfig,
    ) -> object | None:
        kwargs = self._storyblocks_operation_policy_kwargs(config)
        try:
            return policy_type(**kwargs)
        except TypeError:
            legacy_kwargs = dict(kwargs)
            legacy_kwargs.pop("search_timeout_seconds", None)
            try:
                return policy_type(**legacy_kwargs)
            except TypeError:
                return None

    def _storyblocks_operation_policy_for_backend(
        self,
        backend: AssetDownloadBackend,
        provider_id: str,
        config: MediaSelectionConfig,
    ) -> object | None:
        if not self._is_storyblocks_provider(provider_id):
            return None
        current_policy = getattr(backend, "operation_policy", None)
        if current_policy is None:
            return None
        policy_type = type(current_policy)
        return self._instantiate_storyblocks_operation_policy(
            policy_type,
            config=config,
        )

    def _remaining_timeout_seconds(
        self,
        timeout_seconds: float,
        *,
        started_at: float,
    ) -> float | None:
        if timeout_seconds <= 0:
            return None
        remaining = float(timeout_seconds) - (perf_counter() - started_at)
        return max(0.0, remaining)

    def _elapsed_ms(self, started_at: float) -> int:
        return int(round((perf_counter() - started_at) * 1000.0))

    def _provider_submission_window(
        self,
        concurrency_limits: EffectiveConcurrencyLimits,
        config: MediaSelectionConfig,
    ) -> int:
        window = max(1, int(concurrency_limits.provider_queue_size))
        if config.early_stop_when_satisfied:
            window = min(window, max(1, int(concurrency_limits.provider_workers)))
        return max(1, window)

    def _search_budget_exhausted(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        config: MediaSelectionConfig,
        *,
        search_started_at: float,
    ) -> bool:
        budget = max(0.0, float(config.no_match_budget_seconds))
        if budget <= 0.0:
            return False
        elapsed = perf_counter() - search_started_at
        if elapsed < budget:
            return False
        self._emit(
            manifest,
            "paragraph.search_budget.exhausted",
            EventLevel.WARNING,
            f"Paragraph {paragraph.paragraph_no} hit the no-match search budget",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PROVIDER_SEARCH,
            payload={
                "budget_seconds": round(budget, 3),
                "elapsed_seconds": round(elapsed, 3),
            },
        )
        return True

    def _queries_for_backend(
        self, paragraph: ParagraphUnit, backend: CandidateSearchBackend
    ) -> list[str]:
        if paragraph.query_bundle is None:
            return []
        provider_specific = paragraph.query_bundle.provider_queries.get(
            backend.provider_id, []
        )
        if provider_specific:
            return list(provider_specific)
        if backend.capability == ProviderCapability.VIDEO:
            return list(paragraph.query_bundle.video_queries)
        return list(paragraph.query_bundle.image_queries)

    def _resolve_video_backends(
        self, config: MediaSelectionConfig
    ) -> list[CandidateSearchBackend]:
        if not config.video_enabled:
            return []
        ordered = self._provider_registry.resolve_enabled(
            self._provider_settings,
            capability=ProviderCapability.VIDEO,
        )
        return [
            self._video_backends[item.provider_id]
            for item in ordered
            if item.provider_id in self._video_backends
        ]

    def _build_slots(self, config: MediaSelectionConfig) -> list[MediaSlot]:
        slots: list[MediaSlot] = []
        if config.video_enabled:
            slots.append(
                MediaSlot(
                    slot_id="primary_video",
                    kind=AssetKind.VIDEO,
                    role="primary",
                    required=True,
                )
            )
        for index in range(1, config.supporting_image_limit + 1):
            slots.append(
                MediaSlot(
                    slot_id=f"supporting_image_{index}",
                    kind=AssetKind.IMAGE,
                    role="supporting",
                )
            )
        for index in range(1, config.fallback_image_limit + 1):
            slots.append(
                MediaSlot(
                    slot_id=f"fallback_image_{index}",
                    kind=AssetKind.IMAGE,
                    role="fallback",
                )
            )
        return slots

    def _slots_with_selection(
        self, slots: list[MediaSlot], selection: AssetSelection
    ) -> list[MediaSlot]:
        updated: list[MediaSlot] = []
        supporting_iter = iter(selection.supporting_assets)
        fallback_iter = iter(selection.fallback_assets)
        for slot in slots:
            current = MediaSlot(
                slot_id=slot.slot_id,
                kind=slot.kind,
                role=slot.role,
                required=slot.required,
                selected_asset_id=slot.selected_asset_id,
            )
            if slot.slot_id == "primary_video" and selection.primary_asset is not None:
                current.selected_asset_id = selection.primary_asset.asset_id
            elif slot.role == "supporting":
                asset = next(supporting_iter, None)
                current.selected_asset_id = (
                    asset.asset_id if asset is not None else None
                )
            elif slot.role == "fallback":
                asset = next(fallback_iter, None)
                current.selected_asset_id = (
                    asset.asset_id if asset is not None else None
                )
            updated.append(current)
        return updated

    def _choose_best_candidate(
        self, results: list[ProviderResult]
    ) -> AssetCandidate | None:
        candidates: list[AssetCandidate] = []
        for result in results:
            candidates.extend(result.candidates)
        candidates.sort(key=self._asset_rank, reverse=True)
        return candidates[0] if candidates else None

    def _select_primary_video_asset(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        results: list[ProviderResult],
        config: MediaSelectionConfig,
        *,
        concurrency_limits: EffectiveConcurrencyLimits,
    ) -> tuple[AssetCandidate | None, list[str]]:
        errors: list[str] = []
        candidates = self._top_assets(
            results,
            config.video_selection.ranked_candidate_limit,
            config=config,
            manifest=manifest,
            paragraph=paragraph,
            concurrency_limits=concurrency_limits,
        )
        for candidate in candidates:
            if candidate.kind != AssetKind.VIDEO:
                continue
            try:
                downloaded = self._download_primary_video(
                    manifest,
                    paragraph,
                    candidate,
                    config,
                )
            except Exception as exc:
                errors.append(
                    f"{candidate.provider_name}:{candidate.asset_id}:download_failed:{exc}"
                )
                self._emit(
                    manifest,
                    "asset.download.candidate_rejected",
                    EventLevel.WARNING,
                    f"Rejected video candidate {candidate.asset_id} after download failure",
                    paragraph_no=paragraph.paragraph_no,
                    provider_name=candidate.provider_name,
                    query=str(candidate.metadata.get("search_query", "")),
                    stage=RunStage.DOWNLOAD,
                    payload={
                        "current_asset_id": candidate.asset_id,
                        "error": str(exc),
                    },
                )
                continue
            if errors:
                self._emit(
                    manifest,
                    "asset.download.fallback_selected",
                    EventLevel.INFO,
                    f"Selected fallback video candidate {downloaded.asset_id}",
                    paragraph_no=paragraph.paragraph_no,
                    provider_name=downloaded.provider_name,
                    query=str(downloaded.metadata.get("search_query", "")),
                    stage=RunStage.DOWNLOAD,
                    payload={
                        "current_asset_id": downloaded.asset_id,
                        "previous_errors": list(errors),
                    },
                )
            return downloaded, errors
        return None, errors

    def _top_assets(
        self,
        results: list[ProviderResult],
        limit: int,
        *,
        config: MediaSelectionConfig,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        concurrency_limits: EffectiveConcurrencyLimits,
        preserve_order: bool = False,
    ) -> list[AssetCandidate]:
        candidates: list[AssetCandidate] = []
        for result in results:
            candidates.extend(result.candidates)
        if preserve_order:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.kind == AssetKind.IMAGE
            ]
        else:
            candidates = self._rank_video_candidates(
                candidates,
                config,
                manifest=manifest,
                paragraph=paragraph,
                concurrency_limits=concurrency_limits,
            )
        return candidates[:limit]

    def _provider_result_issues(self, results: list[ProviderResult]) -> list[str]:
        issues: list[str] = []
        for result in results:
            if result.errors:
                issues.extend(
                    f"{result.provider_name}:{error}"
                    for error in result.errors
                    if error
                )
            elif not result.candidates:
                issues.append(f"{result.provider_name}:no_results")
        return issues

    def _asset_download_issues(self, assets: list[AssetCandidate]) -> list[str]:
        issues: list[str] = []
        for asset in assets:
            if asset.metadata.get("download_status") != "failed":
                continue
            details = str(
                asset.metadata.get("download_error", "download failed")
            ).strip()
            issues.append(
                f"{asset.provider_name}:{asset.asset_id}:download_failed:{details or 'download failed'}"
            )
        return issues

    def _successful_image_assets(
        self, assets: list[AssetCandidate]
    ) -> list[AssetCandidate]:
        return [
            asset
            for asset in assets
            if asset.metadata.get("download_status") != "failed"
        ]

    def _count_downloaded_assets(self, selection: AssetSelection) -> int:
        downloaded = 0
        assets = [
            selection.primary_asset,
            *selection.supporting_assets,
            *selection.fallback_assets,
        ]
        for asset in assets:
            if asset is None:
                continue
            if asset.local_path is not None:
                downloaded += 1
                continue
            if str(asset.metadata.get("download_status", "")).strip() == "completed":
                downloaded += 1
        return downloaded

    def _reserve_selection_assets(
        self,
        manifest: RunManifest,
        selection: AssetSelection,
        diagnostics: ParagraphDiagnostics,
    ) -> None:
        run_deduper = self._run_deduper(manifest)
        paragraph_deduper = run_deduper.child()
        reserved_primary: AssetCandidate | None = None
        reserved_supporting: list[AssetCandidate] = []
        reserved_fallback: list[AssetCandidate] = []
        reservation_rejections: dict[str, int] = {}

        for role, asset in self._selection_assets_with_roles(selection):
            counts_before = dict(paragraph_deduper.rejection_counts)
            accepted = paragraph_deduper.filter_candidates([asset])
            if accepted:
                accepted_asset = accepted[0]
                if role == "primary":
                    reserved_primary = accepted_asset
                elif role == "supporting":
                    reserved_supporting.append(accepted_asset)
                else:
                    reserved_fallback.append(accepted_asset)
                continue
            deltas = self._dedupe_count_delta(
                counts_before, paragraph_deduper.rejection_counts
            )
            self._merge_rejection_counts(reservation_rejections, deltas)
            selection.rejection_reasons.extend(
                self._asset_dedupe_rejection_reasons(asset, deltas)
            )

        selection.primary_asset = reserved_primary
        selection.supporting_assets = reserved_supporting
        selection.fallback_assets = reserved_fallback
        self._merge_rejection_counts(
            diagnostics.dedupe_rejections, reservation_rejections
        )
        self._register_selection_assets(manifest, selection)
        self._refresh_selection_outcome(
            selection,
            reserved_by_other_paragraph=bool(
                reservation_rejections and not self._selection_assets(selection)
            ),
        )

    def _selection_assets_with_roles(
        self, selection: AssetSelection
    ) -> list[tuple[str, AssetCandidate]]:
        assets: list[tuple[str, AssetCandidate]] = []
        if selection.primary_asset is not None:
            assets.append(("primary", selection.primary_asset))
        assets.extend(("supporting", asset) for asset in selection.supporting_assets)
        assets.extend(("fallback", asset) for asset in selection.fallback_assets)
        return assets

    def _dedupe_count_delta(
        self, before: dict[str, int], after: dict[str, int]
    ) -> dict[str, int]:
        deltas: dict[str, int] = {}
        for reason, count in after.items():
            delta = int(count) - int(before.get(reason, 0))
            if delta > 0:
                deltas[reason] = delta
        return deltas

    def _merge_rejection_counts(
        self, target: dict[str, int], additions: dict[str, int]
    ) -> None:
        for reason, count in additions.items():
            target[reason] = int(target.get(reason, 0)) + int(count)

    def _asset_dedupe_rejection_reasons(
        self, asset: AssetCandidate, counts: dict[str, int]
    ) -> list[str]:
        reasons: list[str] = []
        for reason, count in counts.items():
            if int(count) <= 0:
                continue
            reasons.append(f"{asset.provider_name}:{asset.asset_id}:dedupe:{reason}")
        return reasons

    def _refresh_selection_outcome(
        self,
        selection: AssetSelection,
        *,
        reserved_by_other_paragraph: bool = False,
    ) -> None:
        if self._selection_assets(selection):
            selection.status = "completed"
            if selection.primary_asset is not None:
                if not selection.supporting_assets and selection.fallback_assets:
                    selection.reason = (
                        selection.reason
                        or "Primary video selected with fallback images"
                    )
            elif selection.supporting_assets:
                selection.reason = "Image-only fallback satisfied the paragraph"
            elif selection.fallback_assets:
                selection.reason = "Fallback images satisfied the paragraph"
            elif reserved_by_other_paragraph:
                selection.reason = ""
            return
        selection.status = "no_match"
        if reserved_by_other_paragraph:
            selection.reason = (
                "Selected assets were already reserved by another paragraph"
            )
            return
        if not _normalize_text(selection.reason):
            selection.reason = "No acceptable media candidates found"

    def _no_match_reason_key(self, reasons: list[str]) -> str:
        normalized_reasons = [
            _normalize_text(reason).casefold()
            for reason in reasons
            if _normalize_text(reason)
        ]
        if not normalized_reasons:
            return "no_candidates"
        reason_blob = " | ".join(normalized_reasons)
        if "cancel" in reason_blob:
            return "cancelled"
        if "timeout" in reason_blob:
            return "timeout"
        if "download_failed" in reason_blob:
            return "download_failed"
        if "no_results" in reason_blob:
            return "provider_no_results"
        if "session" in reason_blob or "login" in reason_blob or "auth" in reason_blob:
            return "session_or_auth"
        if "config" in reason_blob:
            return "config_error"
        if "provider" in reason_blob and "error" in reason_blob:
            return "provider_error"
        if "dedupe" in reason_blob or "duplicate" in reason_blob:
            return "dedupe_exhausted"
        return "unknown"

    def _unique_reason_strings(self, reasons: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for reason in reasons:
            normalized = _normalize_text(reason)
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(normalized)
        return unique

    def _asset_rank(self, asset: AssetCandidate) -> float:
        return self._raw_asset_rank(asset)

    def _raw_asset_rank(self, asset: AssetCandidate) -> float:
        raw = asset.metadata.get("rank_hint", 0.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _seed_run_deduper(self, manifest: RunManifest) -> AssetDeduper:
        deduper = AssetDeduper()
        for entry in sorted(
            manifest.paragraph_entries, key=lambda item: item.paragraph_no
        ):
            if entry.selection is None:
                continue
            for asset in self._selection_assets(entry.selection):
                deduper.register(asset)
        return deduper

    def _run_deduper(self, manifest: RunManifest) -> AssetDeduper:
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            deduper = self._run_dedupers.get(manifest.run_id)
            if deduper is not None:
                return deduper
            seeded = self._seed_run_deduper(manifest)
            self._run_dedupers[manifest.run_id] = seeded
            return seeded

    def _register_selection_assets(
        self, manifest: RunManifest, selection: AssetSelection | None
    ) -> None:
        if selection is None:
            return
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            deduper = self._run_deduper(manifest)
            for asset in self._selection_assets(selection):
                deduper.register(asset)

    def _selection_assets(self, selection: AssetSelection) -> list[AssetCandidate]:
        assets = [
            selection.primary_asset,
            *selection.supporting_assets,
            *selection.fallback_assets,
        ]
        return [asset for asset in assets if asset is not None]

    def _build_live_run_state_snapshot(
        self, manifest: RunManifest, *, detailed_paragraph_no: int | None = None
    ) -> LiveRunStateSnapshot:
        paragraph_states: dict[int, LiveParagraphStateSnapshot] = {}
        for entry in manifest.paragraph_entries:
            state = LiveParagraphStateSnapshot(
                paragraph_no=entry.paragraph_no,
                status=entry.status,
                result_note=self._entry_result_note(entry),
            )
            if (
                detailed_paragraph_no is not None
                and entry.paragraph_no == detailed_paragraph_no
            ):
                state.downloaded_files = self._selection_downloaded_file_snapshots(
                    entry.selection
                )
            paragraph_states[entry.paragraph_no] = state
        return LiveRunStateSnapshot(
            run_id=manifest.run_id,
            paragraph_states=paragraph_states,
        )

    def _selection_downloaded_file_snapshots(
        self, selection: AssetSelection | None
    ) -> list[LiveDownloadedFileSnapshot]:
        if selection is None:
            return []
        snapshots: list[LiveDownloadedFileSnapshot] = []
        for role, asset in self._selection_assets_with_roles(selection):
            if asset.local_path is None:
                continue
            snapshots.append(
                LiveDownloadedFileSnapshot(
                    asset_id=asset.asset_id,
                    provider_name=asset.provider_name,
                    kind=asset.kind,
                    role=role,
                    title=str(asset.metadata.get("title", asset.asset_id)),
                    local_path=asset.local_path,
                    exists=asset.local_path.exists(),
                )
            )
        return snapshots

    def _entry_result_note(self, entry: ParagraphManifestEntry) -> str:
        if entry.selection is not None and entry.selection.reason.strip():
            return entry.selection.reason.strip()
        for reason in entry.rejection_reasons:
            if str(reason).strip():
                return str(reason).strip()
        return ""

    def _clone_asset(self, asset: AssetCandidate) -> AssetCandidate:
        return AssetCandidate.from_dict(asset.to_dict())

    def _sync_manifest_entry_index(self, manifest: RunManifest) -> None:
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            self._manifest_entry_indexes[manifest.run_id] = {
                entry.paragraph_no: entry for entry in manifest.paragraph_entries
            }
            self._manifest_index_owners[manifest.run_id] = id(manifest)

    def _entry_for(
        self, manifest: RunManifest, paragraph_no: int
    ) -> ParagraphManifestEntry:
        run_lock = self._run_lock(manifest.run_id)
        with run_lock:
            index = self._manifest_entry_indexes.get(manifest.run_id)
            owner = self._manifest_index_owners.get(manifest.run_id)
            if (
                index is None
                or owner != id(manifest)
                or len(index) != len(manifest.paragraph_entries)
            ):
                self._sync_manifest_entry_index(manifest)
                index = self._manifest_entry_indexes[manifest.run_id]
            entry = index.get(paragraph_no)
            if entry is None:
                self._sync_manifest_entry_index(manifest)
                entry = self._manifest_entry_indexes[manifest.run_id].get(paragraph_no)
            if entry is None:
                raise KeyError(paragraph_no)
            return entry

    def _run_lock(self, run_id: str) -> RLock:
        with self._run_state_guard:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = RLock()
                self._run_locks[run_id] = lock
            return lock

    def _total_candidates(self, results: list[ProviderResult]) -> int:
        return sum(len(result.candidates) for result in results)

    def _resolve_concurrency_mode(
        self, config: MediaSelectionConfig
    ) -> ConcurrencyModeResolution:
        return resolve_execution_concurrency_mode(
            self._provider_settings,
            video_enabled=config.video_enabled,
            storyblocks_images_enabled=config.storyblocks_images_enabled,
            free_images_enabled=config.free_images_enabled,
        )

    def _effective_concurrency_limits(
        self, config: MediaSelectionConfig
    ) -> EffectiveConcurrencyLimits:
        mode_resolution = self._resolve_concurrency_mode(config)
        mode = mode_resolution.mode
        provider_workers = max(1, int(config.provider_workers))
        provider_queue_size = max(1, int(config.provider_queue_size))
        download_workers = max(1, int(config.download_workers))
        download_queue_size = max(1, int(config.bounded_downloads))
        video_ranking_workers = max(1, int(config.video_selection.ranking_workers))
        video_ranking_queue_size = max(
            1,
            int(config.video_selection.ranking_queue_size),
        )
        if mode in {
            ExecutionConcurrencyMode.STORYBLOCKS_SAFE,
            ExecutionConcurrencyMode.MIXED_SAFE,
        }:
            provider_workers = 1
            download_workers = 1
        return EffectiveConcurrencyLimits(
            mode=mode,
            provider_workers=provider_workers,
            provider_queue_size=provider_queue_size,
            download_workers=download_workers,
            download_queue_size=download_queue_size,
            video_ranking_workers=video_ranking_workers,
            video_ranking_queue_size=video_ranking_queue_size,
        )

    def _sourcing_strategy_payload(
        self, config: MediaSelectionConfig
    ) -> dict[str, Any]:
        image_paths = self._resolve_image_search_paths(config)
        mode_resolution = self._resolve_concurrency_mode(config)
        limits = self._effective_concurrency_limits(config)
        video_provider_ids = [
            provider_id
            for provider_id in mode_resolution.selected_provider_ids
            if (descriptor := self._provider_registry.get(provider_id)) is not None
            and descriptor.capability == ProviderCapability.VIDEO
        ]
        return {
            "config": config.to_dict(),
            "video_providers": video_provider_ids,
            "image_primary_providers": [*image_paths.primary_provider_ids],
            "image_fallback_providers": [*image_paths.fallback_provider_ids],
            "image_selection_contract": (
                "free_images_only"
                if image_paths.free_images_only
                else "storyblocks_then_free_fallback"
            ),
            "free_images_only": image_paths.free_images_only,
            "concurrency_mode": mode_resolution.mode.value,
            "selected_provider_ids": list(mode_resolution.selected_provider_ids),
            "effective_concurrency_limits": limits.to_payload(),
        }

    def _emit(
        self,
        manifest: RunManifest,
        name: str,
        level: EventLevel,
        message: str,
        *,
        paragraph_no: int | None = None,
        provider_name: str | None = None,
        query: str | None = None,
        stage: RunStage | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish(
            AppEvent(
                name=name,
                level=level,
                message=message,
                project_id=manifest.project_id,
                run_id=manifest.run_id,
                paragraph_no=paragraph_no,
                provider_name=provider_name,
                query=query,
                stage=stage,
                payload=dict(payload or {}),
            )
        )


class ParagraphMediaRunService:
    def __init__(
        self,
        project_repository: ProjectRepository,
        run_repository: RunRepository,
        manifest_repository: ManifestRepository,
        pipeline: ParagraphMediaPipeline,
        orchestrator: RunOrchestrator,
        session_manager: Any | None = None,
        concurrency_settings: ConcurrencySettings | None = None,
    ):
        self._project_repository = project_repository
        self._run_repository = run_repository
        self._manifest_repository = manifest_repository
        self._pipeline = pipeline
        self._orchestrator = orchestrator
        self._session_manager = session_manager
        self._concurrency_settings = concurrency_settings or ConcurrencySettings()

    def create_run(
        self,
        project_id: str,
        *,
        selected_paragraphs: list[int] | None = None,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        project = self._require_project(project_id)
        selection_config = config or self._default_selection_config()
        self._validate_storyblocks_concurrency(selection_config)
        mode_resolution = self._pipeline._resolve_concurrency_mode(selection_config)
        limits = self._pipeline._effective_concurrency_limits(selection_config)
        paragraphs_total = (
            len(set(selected_paragraphs))
            if selected_paragraphs
            else (
                len(project.script_document.paragraphs)
                if project.script_document is not None
                else 0
            )
        )
        run = self._orchestrator.create_run(
            project_id, selected_paragraphs=selected_paragraphs
        )
        perf_context = ensure_run_performance_context(run)
        if selection_config.output_root:
            run.metadata["output_root"] = selection_config.output_root
        run.metadata["paragraphs_total"] = max(0, int(paragraphs_total))
        run.metadata["concurrency_mode"] = mode_resolution.mode.value
        run.metadata["concurrency_limits"] = limits.to_payload()
        persist_run_performance_context(run, perf_context)
        self._run_repository.save(run)
        manifest = self._pipeline.create_manifest(
            project,
            run,
            project.script_document.paragraphs
            if project.script_document is not None
            else [],
            selection_config,
            perf_context_id=perf_context.context_id,
        )
        project.active_run_id = run.run_id
        self._project_repository.save(project)
        return run, manifest

    def execute(
        self,
        run_id: str,
        *,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        run = self._require_run(run_id)
        perf_context = ensure_run_performance_context(run)
        project = self._require_project(run.project_id)
        if project.script_document is None:
            raise ValueError("Project has no script document")
        selection_config = self._selection_config_for_run(run, config)
        self._validate_storyblocks_concurrency(selection_config)
        manifest = self._manifest_repository.load(run_id)
        if manifest is None:
            manifest = self._pipeline.create_manifest(
                project,
                run,
                project.script_document.paragraphs,
                selection_config,
                perf_context_id=perf_context.context_id,
            )
        active_manifest = cast(RunManifest, manifest)
        self._pipeline.register_live_manifest(active_manifest)

        def processor(paragraph: ParagraphUnit) -> AssetSelection:
            paragraph_started_at = perf_counter()
            cancelled_at_start = self._orchestrator.is_cancel_requested(run.run_id)
            paragraph_config = replace(
                selection_config,
                should_cancel=(
                    lambda cancelled_at_start=cancelled_at_start: cancelled_at_start
                ),
            )
            try:
                return self._pipeline.process_paragraph(
                    active_manifest,
                    paragraph,
                    paragraph_config,
                    perf_context=perf_context,
                )
            except InterruptedError:
                raise
            except Exception as exc:
                paragraph_total_ms = int(
                    round((perf_counter() - paragraph_started_at) * 1000.0)
                )
                self._pipeline.record_paragraph_failure(
                    active_manifest,
                    paragraph,
                    exc,
                    perf_context=perf_context,
                    paragraph_total_ms=paragraph_total_ms,
                )
                raise

        try:
            updated_run = self._orchestrator.execute(
                run,
                project.script_document.paragraphs,
                processor,
                perf_context=perf_context,
            )
            manifest = self._pipeline.load_manifest(run_id) or active_manifest
            self._pipeline.flush_manifest(manifest)
            self._pipeline.release_run_state(updated_run.run_id)
            return updated_run, manifest
        finally:
            if run.status != RunStatus.RUNNING:
                self._pipeline.release_run_state(run.run_id)
            if self._session_manager is not None:
                self._session_manager.close_browsers_owned_by_current_thread()

    def rerun_full_run(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        resolved_project_id = self._resolve_rerun_project_id(
            run_id=run_id,
            project_id=project_id,
        )
        return self.create_and_execute(resolved_project_id, config=config)

    def create_and_execute(
        self,
        project_id: str,
        *,
        selected_paragraphs: list[int] | None = None,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        run, _ = self.create_run(
            project_id, selected_paragraphs=selected_paragraphs, config=config
        )
        return self.execute(run.run_id, config=config)

    def load_manifest(self, run_id: str) -> RunManifest | None:
        return self._pipeline.load_manifest(run_id)

    def snapshot_manifest(self, run_id: str) -> RunManifest | None:
        return self._pipeline.snapshot_manifest(run_id)

    def snapshot_live_run_state(
        self, run_id: str, *, detailed_paragraph_no: int | None = None
    ) -> LiveRunStateSnapshot | None:
        return self._pipeline.snapshot_live_run_state(
            run_id, detailed_paragraph_no=detailed_paragraph_no
        )

    def cancel(self, run_id: str) -> None:
        self._orchestrator.cancel(run_id)

    def _resolve_rerun_project_id(
        self,
        *,
        run_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        if run_id is not None:
            return self._require_run(run_id).project_id
        if project_id is not None:
            return self._require_project(project_id).project_id
        raise ValueError("Either run_id or project_id is required")

    def _require_project(self, project_id: str) -> Project:
        project = self._project_repository.load(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    def _require_run(self, run_id: str) -> Run:
        run = self._run_repository.load(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _selection_config_for_run(
        self, run: Run, config: MediaSelectionConfig | None
    ) -> MediaSelectionConfig:
        selection_config = config or self._default_selection_config()
        stored_output_root = str(run.metadata.get("output_root", "")).strip()
        if stored_output_root:
            selection_config.output_root = stored_output_root
        selection_config.should_cancel = lambda run_id=run.run_id: (
            self._orchestrator.is_cancel_requested(run_id)
        )
        return selection_config

    def _default_selection_config(self) -> MediaSelectionConfig:
        settings = self._concurrency_settings
        return MediaSelectionConfig(
            provider_workers=max(1, int(settings.provider_workers)),
            provider_queue_size=max(1, int(settings.provider_queue_size)),
            bounded_downloads=max(1, int(settings.download_queue_size)),
            download_workers=max(1, int(settings.download_workers)),
            search_timeout_seconds=max(0.0, float(settings.search_timeout_seconds)),
            download_timeout_seconds=max(1.0, float(settings.download_timeout_seconds)),
            retry_budget=max(0, int(settings.retry_budget)),
            fail_fast_storyblocks_errors=bool(settings.fail_fast_storyblocks_errors),
        )

    def _validate_storyblocks_concurrency(self, config: MediaSelectionConfig) -> None:
        mode_resolution = self._pipeline._resolve_concurrency_mode(config)
        if (
            mode_resolution.requires_serial_paragraph_workers
            and self._orchestrator.max_workers > 1
        ):
            raise ConfigError(
                "storyblocks_parallelism_guard",
                "Storyblocks режимы поддерживают только paragraph_workers=1 для безопасной сессии браузера.",
                details={
                    "paragraph_workers": self._orchestrator.max_workers,
                    "concurrency_mode": mode_resolution.mode.value,
                    "selected_provider_ids": list(
                        mode_resolution.selected_provider_ids
                    ),
                },
            )


__all__ = [
    "AssetDeduper",
    "CallbackCandidateSearchBackend",
    "CandidateSearchBackend",
    "FreeImageCandidateSearchBackend",
    "MediaSelectionConfig",
    "ParagraphMediaPipeline",
    "ParagraphMediaRunService",
    "VideoSelectionPolicy",
]
