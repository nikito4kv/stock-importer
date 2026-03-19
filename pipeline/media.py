from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha1
import io
import mimetypes
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Callable, Protocol, Sequence, cast
from urllib.parse import urlparse

from config.settings import ProviderSettings, default_settings
from domain.enums import AssetKind, EventLevel, ProviderCapability, RunStage
from domain.models import (
    AssetCandidate,
    AssetSelection,
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
from providers.base import ProviderDescriptor
from providers.images import (
    ImageLicensePolicy,
    ImageProviderBuildContext,
    ImageProviderSearchService,
    SearchCandidate,
    WrappedImageSearchProvider,
    build_image_provider_clients,
)
from providers.registry import ProviderRegistry
from services.events import AppEvent, EventBus
from services.errors import ConfigError, DownloadError, ProviderError, SessionError
from storage.repositories import ManifestRepository, ProjectRepository, RunRepository
from legacy_core.network import open_with_safe_redirects, read_limited

try:
    from PIL import Image, UnidentifiedImageError
except Exception:  # pragma: no cover - Pillow is an optional runtime import
    Image = None
    UnidentifiedImageError = OSError

from .orchestrator import RunOrchestrator


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


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
class MediaSelectionConfig:
    video_enabled: bool = True
    storyblocks_images_enabled: bool = True
    free_images_enabled: bool = True
    supporting_image_limit: int = 1
    fallback_image_limit: int = 1
    max_candidates_per_provider: int = 8
    top_k_to_relevance: int = 24
    bounded_downloads: int = 8
    bounded_relevance_queue: int = 8
    early_stop_when_satisfied: bool = True
    no_match_budget_seconds: float = 20.0
    output_root: str = ""
    should_cancel: Callable[[], bool] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_enabled": self.video_enabled,
            "storyblocks_images_enabled": self.storyblocks_images_enabled,
            "free_images_enabled": self.free_images_enabled,
            "supporting_image_limit": self.supporting_image_limit,
            "fallback_image_limit": self.fallback_image_limit,
            "max_candidates_per_provider": self.max_candidates_per_provider,
            "top_k_to_relevance": self.top_k_to_relevance,
            "bounded_downloads": self.bounded_downloads,
            "bounded_relevance_queue": self.bounded_relevance_queue,
            "early_stop_when_satisfied": self.early_stop_when_satisfied,
            "no_match_budget_seconds": self.no_match_budget_seconds,
            "output_root": self.output_root,
        }


@dataclass(slots=True)
class CallbackCandidateSearchBackend:
    provider_id: str
    capability: ProviderCapability
    descriptor: ProviderDescriptor
    search_fn: Callable[[ParagraphUnit, str, int], list[AssetCandidate]]

    def search(
        self, paragraph: ParagraphUnit, query: str, limit: int
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

    def search(
        self, paragraph: ParagraphUnit, query: str, limit: int
    ) -> ProviderResult:
        candidates, errors, diagnostics = self.image_search_service.search_provider(
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
        try:
            response, final_url = open_with_safe_redirects(
                source_url,
                timeout_seconds=self.timeout_seconds,
                max_redirects=4,
                accept_header="image/*,*/*;q=0.8",
                user_agent=self.user_agent,
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
            raise DownloadError(
                code="direct_image_download_failed",
                message=f"Image download failed for asset '{asset.asset_id}': {exc}",
                details={"asset_id": asset.asset_id, "provider_id": self.provider_id},
            ) from exc

        destination.write_bytes(payload)
        downloaded = AssetCandidate.from_dict(asset.to_dict())
        downloaded.local_path = destination
        downloaded.metadata["download_status"] = "completed"
        downloaded.metadata["final_url"] = final_url
        downloaded.metadata.update(validation)
        return downloaded


@dataclass(slots=True)
class AssetDeduper:
    source_ids: set[str] = field(default_factory=set)
    raw_hashes: set[str] = field(default_factory=set)
    perceptual_hashes: set[str] = field(default_factory=set)
    semantic_signatures: set[str] = field(default_factory=set)
    rejection_counts: dict[str, int] = field(default_factory=dict)

    def register(self, asset: AssetCandidate) -> None:
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
        accepted: list[AssetCandidate] = []
        for candidate in candidates:
            reason = self._duplicate_reason(candidate)
            if reason is not None:
                self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
                continue
            self.register(candidate)
            accepted.append(candidate)
        return accepted

    def _duplicate_reason(self, asset: AssetCandidate) -> str | None:
        source_id = self._source_id(asset)
        if source_id and source_id in self.source_ids:
            return "source_id"
        raw_hash = self._raw_hash(asset)
        if raw_hash and raw_hash in self.raw_hashes:
            return "raw_file_hash"
        perceptual_hash = self._perceptual_hash(asset)
        if perceptual_hash and perceptual_hash in self.perceptual_hashes:
            return "perceptual_hash"
        semantic = self._semantic_signature(asset)
        if semantic and semantic in self.semantic_signatures:
            return "semantic_similarity"
        return None

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
        return sorted(
            provider_id
            for provider_id in self._image_backends
            if provider_id != "storyblocks_image"
        )

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
        provider_ids = [
            item.provider_id
            for item in self._provider_registry.resolve_enabled(
                self._provider_settings,
                capability=ProviderCapability.IMAGE,
                include_opt_in=self._provider_settings.allow_generic_web_image,
            )
            if item.provider_group != "storyblocks_images"
        ]
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
                allow_generic_web_image=self._provider_settings.allow_generic_web_image,
                free_images_only=self._provider_settings.free_images_only,
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
        removable_ids = {
            item.provider_id
            for item in self._provider_registry.list_by_capability(
                ProviderCapability.IMAGE
            )
            if item.provider_group != "storyblocks_images"
        }
        for provider_id in removable_ids:
            self._image_backends.pop(provider_id, None)
            self._download_backends.pop(provider_id, None)

    def create_manifest(
        self,
        project: Project,
        run: Run,
        paragraphs: list[ParagraphUnit],
        config: MediaSelectionConfig,
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
        return self.save_manifest(self.update_summary(manifest))

    def load_manifest(self, run_id: str) -> RunManifest | None:
        return self._manifest_repository.load(run_id)

    def save_manifest(self, manifest: RunManifest) -> RunManifest:
        manifest.updated_at = utc_now()
        return self._manifest_repository.save(manifest)

    def lock_selection(
        self, run_id: str, paragraph_no: int, selection: AssetSelection
    ) -> RunManifest:
        manifest = self.load_manifest(run_id)
        if manifest is None:
            raise KeyError(run_id)
        entry = self._entry_for(manifest, paragraph_no)
        selection.user_locked = True
        selection.user_decision_status = "locked"
        entry.selection = selection
        entry.user_decision_status = "locked"
        entry.status = "locked"
        entry.slots = self._slots_with_selection(
            entry.slots or self._build_slots(MediaSelectionConfig()), selection
        )
        entry.fallback_options = list(selection.fallback_assets)
        return self.save_manifest(self.update_summary(manifest))

    def process_paragraph(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        config: MediaSelectionConfig,
    ) -> AssetSelection:
        self._raise_if_cancelled(config)
        self._ensure_run_output_dirs(manifest, config)
        self._emit(
            manifest,
            "paragraph.processing.started",
            EventLevel.INFO,
            f"Paragraph {paragraph.paragraph_no} started",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PROVIDER_SEARCH,
        )
        entry = self._entry_for(manifest, paragraph.paragraph_no)
        if entry.selection is not None and entry.selection.user_locked:
            entry.status = "locked"
            entry.user_decision_status = "locked"
            entry.selection.status = "locked"
            self._emit(
                manifest,
                "paragraph.locked",
                EventLevel.INFO,
                f"Paragraph {paragraph.paragraph_no} kept locked user selection",
                paragraph_no=paragraph.paragraph_no,
                stage=RunStage.PERSIST,
                payload={"user_locked": True},
            )
            self.save_manifest(self.update_summary(manifest))
            return entry.selection

        diagnostics = ParagraphDiagnostics(
            paragraph_no=paragraph.paragraph_no,
            provider_queries={},
            fanout_limits={
                "top_k_to_relevance": config.top_k_to_relevance,
                "bounded_downloads": config.bounded_downloads,
                "bounded_relevance_queue": config.bounded_relevance_queue,
                "max_candidates_per_provider": config.max_candidates_per_provider,
            },
        )
        selection = AssetSelection(
            paragraph_no=paragraph.paragraph_no,
            media_slots=self._build_slots(config),
            status="processing",
        )
        deduper = self._build_deduper(
            manifest, exclude_paragraph_no=paragraph.paragraph_no
        )
        search_started_at = perf_counter()
        entry.intent = paragraph.intent
        entry.query_bundle = paragraph.query_bundle

        video_results, early_video_stop = self._collect_results(
            manifest,
            paragraph,
            self._resolve_video_backends(config),
            config,
            deduper,
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
            )
            if primary_video is not None:
                selection.primary_asset = primary_video
                selection.reason = (
                    f"Primary video selected from {primary_video.provider_name}"
                )
                selection.status = "partial_success"
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
                search_started_at=search_started_at,
            )
        )
        image_results.extend(primary_image_results)
        image_results.extend(fallback_image_results)

        selection.supporting_assets = self._top_assets(
            primary_image_results,
            config.supporting_image_limit,
        )
        selection.fallback_assets = self._top_assets(
            fallback_image_results,
            config.fallback_image_limit,
        )
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
        )
        selection.fallback_assets = self._download_image_assets(
            manifest,
            paragraph,
            selection.fallback_assets,
            config,
        )
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
            selection.status = "selected"
        else:
            selection.status = "no_match"
            selection.reason = "No acceptable media candidates found"

        diagnostics.provider_results = video_results + image_results
        diagnostics.provider_queries = {
            result.provider_name: [result.query]
            for result in diagnostics.provider_results
            if result.query
        }
        selection.rejection_reasons = self._unique_reason_strings(
            selection.rejection_reasons
        )
        diagnostics.rejected_reasons = list(selection.rejection_reasons)
        diagnostics.early_stop_triggered = early_video_stop or early_image_stop
        diagnostics.dedupe_rejections = dict(deduper.rejection_counts)
        selection.provider_results = list(diagnostics.provider_results)
        selection.diagnostics = {
            "provider_queries": diagnostics.provider_queries,
            "fanout_limits": diagnostics.fanout_limits,
            "dedupe_rejections": diagnostics.dedupe_rejections,
            "early_stop_triggered": diagnostics.early_stop_triggered,
        }
        selection.rejection_reasons = list(diagnostics.rejected_reasons)
        selection.user_decision_status = entry.user_decision_status or "auto_selected"
        selection.media_slots = self._slots_with_selection(
            selection.media_slots, selection
        )

        entry.selection = selection
        entry.diagnostics = diagnostics
        entry.fallback_options = list(selection.fallback_assets)
        entry.rejection_reasons = list(selection.rejection_reasons)
        entry.user_decision_status = selection.user_decision_status
        entry.status = selection.status
        entry.slots = list(selection.media_slots)

        if selection.status == "no_match":
            self._emit(
                manifest,
                "paragraph.no_results",
                EventLevel.WARNING,
                f"Paragraph {paragraph.paragraph_no} produced no acceptable results",
                paragraph_no=paragraph.paragraph_no,
                stage=RunStage.RELEVANCE,
            )
        elif selection.primary_asset is None:
            self._emit(
                manifest,
                "paragraph.awaiting_manual_decision",
                EventLevel.WARNING,
                f"Paragraph {paragraph.paragraph_no} needs manual decision",
                paragraph_no=paragraph.paragraph_no,
                stage=RunStage.PERSIST,
                payload={
                    "supporting_assets": len(selection.supporting_assets),
                    "fallback_assets": len(selection.fallback_assets),
                },
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

        self.save_manifest(self.update_summary(manifest))
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
            },
        )
        return selection

    def record_paragraph_failure(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        exc: Exception,
    ) -> RunManifest:
        entry = self._entry_for(manifest, paragraph.paragraph_no)
        entry.intent = paragraph.intent
        entry.query_bundle = paragraph.query_bundle
        entry.selection = None
        entry.diagnostics = None
        entry.fallback_options = []
        entry.rejection_reasons = [str(exc)]
        entry.user_decision_status = "needs_review"
        entry.status = "failed"
        self._emit(
            manifest,
            "paragraph.processing.failed",
            EventLevel.ERROR,
            f"Paragraph {paragraph.paragraph_no} failed and was recorded",
            paragraph_no=paragraph.paragraph_no,
            stage=RunStage.PERSIST,
            payload={"error": str(exc)},
        )
        return self.save_manifest(self.update_summary(manifest))

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
        try:
            downloaded = backend.download_asset(
                asset, destination_dir=destination_dir, filename=filename
            )
        except Exception as exc:
            error_code = (
                exc.code if isinstance(exc, DownloadError) else "download_failed"
            )
            details = dict(exc.details) if isinstance(exc, DownloadError) else {}
            self._emit(
                manifest,
                "asset.download.failed",
                EventLevel.ERROR,
                f"Download failed for asset {asset.asset_id}: {exc}",
                paragraph_no=paragraph.paragraph_no,
                provider_name=asset.provider_name,
                query=str(asset.metadata.get("search_query", "")),
                stage=RunStage.DOWNLOAD,
                payload={
                    "current_asset_id": asset.asset_id,
                    "error_code": error_code,
                    **details,
                },
            )
            raise
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
    ) -> list[AssetCandidate]:
        return [
            self._download_image_asset(manifest, paragraph, asset, config)
            if asset.kind == AssetKind.IMAGE
            else asset
            for asset in assets
        ]

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
        try:
            downloaded = backend.download_asset(
                asset,
                destination_dir=destination_dir,
                filename=filename,
            )
        except Exception as exc:
            self._emit(
                manifest,
                "asset.download.failed",
                EventLevel.WARNING,
                f"Image download failed for asset {asset.asset_id}: {exc}",
                paragraph_no=paragraph.paragraph_no,
                provider_name=asset.provider_name,
                query=str(asset.metadata.get("search_query", "")),
                stage=RunStage.DOWNLOAD,
                payload={"current_asset_id": asset.asset_id},
            )
            asset.metadata["download_status"] = "failed"
            asset.metadata["download_error"] = str(exc)
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
        matched = 0
        no_match = 0
        failed = 0
        locked = 0
        primary_videos = 0
        supporting_images = 0
        fallback_images = 0
        for entry in manifest.paragraph_entries:
            if entry.status not in {"pending", "processing"}:
                processed += 1
            if entry.status in {"selected", "locked", "partial_success"}:
                matched += 1
            if entry.status == "no_match":
                no_match += 1
            if entry.status == "failed":
                failed += 1
            if entry.user_decision_status == "locked":
                locked += 1
            if entry.selection is not None:
                if entry.selection.primary_asset is not None:
                    primary_videos += 1
                supporting_images += len(entry.selection.supporting_assets)
                fallback_images += len(entry.selection.fallback_assets)
        manifest.summary = {
            "paragraphs_total": len(manifest.paragraph_entries),
            "paragraphs_processed": processed,
            "paragraphs_completed": processed,
            "paragraphs_matched": matched,
            "paragraphs_no_match": no_match,
            "paragraphs_failed": failed,
            "locked_paragraphs": locked,
            "primary_videos": primary_videos,
            "supporting_images": supporting_images,
            "fallback_images": fallback_images,
        }
        manifest.updated_at = utc_now()
        return manifest

    def _collect_results(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        backends: list[CandidateSearchBackend],
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        *,
        search_started_at: float,
    ) -> tuple[list[ProviderResult], bool]:
        results: list[ProviderResult] = []
        early_stop = False
        for backend in backends:
            self._raise_if_cancelled(config)
            queries = self._queries_for_backend(paragraph, backend)
            for query in queries:
                if self._search_budget_exhausted(
                    manifest,
                    paragraph,
                    config,
                    search_started_at=search_started_at,
                ):
                    return results, True
                self._raise_if_cancelled(config)
                result = self._search_backend(
                    manifest,
                    paragraph,
                    backend,
                    query,
                    config.max_candidates_per_provider,
                )
                filtered = deduper.filter_candidates(list(result.candidates))
                filtered.sort(key=self._asset_rank, reverse=True)
                result.candidates = filtered[: config.top_k_to_relevance]
                results.append(result)
                if (
                    config.early_stop_when_satisfied
                    and result.candidates
                    and backend.capability == ProviderCapability.VIDEO
                ):
                    early_stop = True
                    self._emit(
                        manifest,
                        "paragraph.early_stop",
                        EventLevel.INFO,
                        f"Paragraph {paragraph.paragraph_no} stopped early after provider {backend.provider_id}",
                        paragraph_no=paragraph.paragraph_no,
                        provider_name=backend.provider_id,
                        query=query,
                        stage=RunStage.PROVIDER_SEARCH,
                    )
                    break
            if early_stop:
                break
        return results, early_stop

    def _collect_image_results(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        primary_video: AssetCandidate | None,
        *,
        search_started_at: float,
    ) -> tuple[list[ProviderResult], list[ProviderResult], bool]:
        strategy = self._provider_registry.resolve_image_strategy(
            self._provider_settings
        )
        primary_backends = [
            self._image_backends[item.provider_id]
            for item in strategy["primary"]
            if item.provider_id in self._image_backends
        ]
        fallback_backends = [
            self._image_backends[item.provider_id]
            for item in strategy["fallback"]
            if item.provider_id in self._image_backends
        ]

        if not config.storyblocks_images_enabled:
            primary_backends = [
                backend
                for backend in primary_backends
                if backend.provider_id != "storyblocks_image"
            ]
        if not config.free_images_enabled:
            primary_backends = [
                backend
                for backend in primary_backends
                if backend.provider_id == "storyblocks_image"
            ]
            fallback_backends = []

        primary_results: list[ProviderResult] = []
        fallback_results: list[ProviderResult] = []
        early_stop = False

        for result in self._collect_image_result_list(
            manifest,
            paragraph,
            primary_backends,
            config,
            deduper,
            search_started_at=search_started_at,
        ):
            self._raise_if_cancelled(config)
            primary_results.append(result)
            if (
                self._total_candidates(primary_results) >= config.supporting_image_limit
                and config.early_stop_when_satisfied
            ):
                early_stop = True
                break

        if config.free_images_enabled and (not early_stop or primary_video is None):
            for result in self._collect_image_result_list(
                manifest,
                paragraph,
                fallback_backends,
                config,
                deduper,
                search_started_at=search_started_at,
            ):
                self._raise_if_cancelled(config)
                fallback_results.append(result)
                if (
                    self._total_candidates(fallback_results)
                    >= config.fallback_image_limit
                    and config.early_stop_when_satisfied
                ):
                    early_stop = True
                    break

        return primary_results, fallback_results, early_stop

    def _collect_image_result_list(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        backends: list[CandidateSearchBackend],
        config: MediaSelectionConfig,
        deduper: AssetDeduper,
        *,
        search_started_at: float,
    ) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        for backend in backends:
            self._raise_if_cancelled(config)
            for query in self._queries_for_backend(paragraph, backend):
                if self._search_budget_exhausted(
                    manifest,
                    paragraph,
                    config,
                    search_started_at=search_started_at,
                ):
                    return results
                self._raise_if_cancelled(config)
                result = self._search_backend(
                    manifest,
                    paragraph,
                    backend,
                    query,
                    config.max_candidates_per_provider,
                )
                filtered = deduper.filter_candidates(list(result.candidates))
                filtered.sort(key=self._asset_rank, reverse=True)
                result.candidates = filtered[: config.top_k_to_relevance]
                results.append(result)
        return results

    def _search_backend(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        backend: CandidateSearchBackend,
        query: str,
        limit: int,
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
        search_started_at = perf_counter()
        try:
            result = backend.search(paragraph, query, limit)
        except (ConfigError, ProviderError, SessionError) as exc:
            elapsed_ms = int(round((perf_counter() - search_started_at) * 1000.0))
            message = str(exc)
            fatal_storyblocks_error = (
                backend.provider_id == "storyblocks_video"
                and isinstance(
                    exc,
                    (ConfigError, ProviderError, SessionError),
                )
            )
            self._emit(
                manifest,
                "provider.search.failed"
                if fatal_storyblocks_error
                else "provider.search.warning",
                EventLevel.ERROR if fatal_storyblocks_error else EventLevel.WARNING,
                message,
                paragraph_no=paragraph.paragraph_no,
                provider_name=backend.provider_id,
                query=query,
                stage=RunStage.PROVIDER_SEARCH,
                payload={
                    "error_code": exc.code,
                    "search_elapsed_ms": elapsed_ms,
                    **dict(exc.details),
                },
            )
            if fatal_storyblocks_error:
                raise
            return ProviderResult(
                provider_name=backend.provider_id,
                capability=backend.capability,
                query=query,
                candidates=[],
                errors=[message],
                diagnostics={
                    "error_code": exc.code,
                    "search_elapsed_ms": elapsed_ms,
                    **dict(exc.details),
                },
            )
        elapsed_ms = int(round((perf_counter() - search_started_at) * 1000.0))
        result.diagnostics.setdefault("search_elapsed_ms", elapsed_ms)
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
                payload={"search_elapsed_ms": elapsed_ms},
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
                payload={"search_elapsed_ms": elapsed_ms},
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
                "current_asset_id": result.candidates[0].asset_id
                if result.candidates
                else "",
            },
        )
        return result

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
            include_opt_in=False,
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
                user_locked=selection.user_locked,
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
        candidates = self._top_assets(results, 1)
        return candidates[0] if candidates else None

    def _select_primary_video_asset(
        self,
        manifest: RunManifest,
        paragraph: ParagraphUnit,
        results: list[ProviderResult],
        config: MediaSelectionConfig,
    ) -> tuple[AssetCandidate | None, list[str]]:
        errors: list[str] = []
        candidates = self._top_assets(results, config.top_k_to_relevance)
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
        self, results: list[ProviderResult], limit: int
    ) -> list[AssetCandidate]:
        candidates: list[AssetCandidate] = []
        for result in results:
            candidates.extend(result.candidates)
        candidates.sort(key=self._asset_rank, reverse=True)
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
        raw = asset.metadata.get("rank_hint", 0.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _build_deduper(
        self, manifest: RunManifest, *, exclude_paragraph_no: int | None = None
    ) -> AssetDeduper:
        deduper = AssetDeduper()
        for entry in sorted(
            manifest.paragraph_entries, key=lambda item: item.paragraph_no
        ):
            if (
                exclude_paragraph_no is not None
                and entry.paragraph_no == exclude_paragraph_no
            ):
                continue
            if entry.selection is None:
                continue
            for asset in [
                entry.selection.primary_asset,
                *entry.selection.supporting_assets,
                *entry.selection.fallback_assets,
            ]:
                if asset is not None:
                    deduper.register(asset)
        return deduper

    def _entry_for(
        self, manifest: RunManifest, paragraph_no: int
    ) -> ParagraphManifestEntry:
        for entry in manifest.paragraph_entries:
            if entry.paragraph_no == paragraph_no:
                return entry
        raise KeyError(paragraph_no)

    def _total_candidates(self, results: list[ProviderResult]) -> int:
        return sum(len(result.candidates) for result in results)

    def _sourcing_strategy_payload(
        self, config: MediaSelectionConfig
    ) -> dict[str, Any]:
        strategy = self._provider_registry.resolve_image_strategy(
            self._provider_settings
        )
        return {
            "config": config.to_dict(),
            "video_providers": [
                item.provider_id
                for item in self._provider_registry.resolve_enabled(
                    self._provider_settings, capability=ProviderCapability.VIDEO
                )
            ],
            "image_primary_providers": [
                item.provider_id for item in strategy["primary"]
            ],
            "image_fallback_providers": [
                item.provider_id for item in strategy["fallback"]
            ],
            "free_images_only": self._provider_settings.free_images_only,
            "mixed_image_fallback": self._provider_settings.mixed_image_fallback,
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
    ):
        self._project_repository = project_repository
        self._run_repository = run_repository
        self._manifest_repository = manifest_repository
        self._pipeline = pipeline
        self._orchestrator = orchestrator
        self._session_manager = session_manager

    def create_run(
        self,
        project_id: str,
        *,
        selected_paragraphs: list[int] | None = None,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        project = self._require_project(project_id)
        selection_config = config or MediaSelectionConfig()
        run = self._orchestrator.create_run(
            project_id, selected_paragraphs=selected_paragraphs
        )
        if selection_config.output_root:
            run.metadata["output_root"] = selection_config.output_root
            self._run_repository.save(run)
        manifest = self._pipeline.create_manifest(
            project,
            run,
            project.script_document.paragraphs
            if project.script_document is not None
            else [],
            selection_config,
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
        project = self._require_project(run.project_id)
        if project.script_document is None:
            raise ValueError("Project has no script document")
        selection_config = self._selection_config_for_run(run, config)
        manifest = self._manifest_repository.load(run_id)
        if manifest is None:
            manifest = self._pipeline.create_manifest(
                project,
                run,
                project.script_document.paragraphs,
                selection_config,
            )
        active_manifest = cast(RunManifest, manifest)

        def processor(paragraph: ParagraphUnit) -> AssetSelection:
            try:
                return self._pipeline.process_paragraph(
                    active_manifest, paragraph, selection_config
                )
            except InterruptedError:
                raise
            except Exception as exc:
                self._pipeline.record_paragraph_failure(active_manifest, paragraph, exc)
                raise

        try:
            updated_run = self._orchestrator.execute(
                run, project.script_document.paragraphs, processor
            )
            manifest = self._pipeline.load_manifest(run_id) or active_manifest
            self._pipeline.save_manifest(self._pipeline.update_summary(manifest))
            return updated_run, manifest
        finally:
            if self._session_manager is not None:
                self._session_manager.close_browsers_owned_by_current_thread()

    def resume(
        self, run_id: str, *, config: MediaSelectionConfig | None = None
    ) -> tuple[Run, RunManifest]:
        run = self._require_run(run_id)
        project = self._require_project(run.project_id)
        if project.script_document is None:
            raise ValueError("Project has no script document")
        selection_config = self._selection_config_for_run(run, config)
        manifest = self._manifest_repository.load(run_id)
        if manifest is None:
            manifest = self._pipeline.create_manifest(
                project,
                run,
                project.script_document.paragraphs,
                selection_config,
            )
        active_manifest = cast(RunManifest, manifest)

        def processor(paragraph: ParagraphUnit) -> AssetSelection:
            try:
                return self._pipeline.process_paragraph(
                    active_manifest, paragraph, selection_config
                )
            except InterruptedError:
                raise
            except Exception as exc:
                self._pipeline.record_paragraph_failure(active_manifest, paragraph, exc)
                raise

        try:
            updated_run = self._orchestrator.resume(
                run_id, project.script_document.paragraphs, processor
            )
            manifest = self._pipeline.load_manifest(run_id) or active_manifest
            self._pipeline.save_manifest(self._pipeline.update_summary(manifest))
            return updated_run, manifest
        finally:
            if self._session_manager is not None:
                self._session_manager.close_browsers_owned_by_current_thread()

    def retry_failed_only(
        self, run_id: str, *, config: MediaSelectionConfig | None = None
    ) -> tuple[Run, RunManifest]:
        run = self._require_run(run_id)
        if not run.failed_paragraphs:
            raise ValueError("Run has no failed paragraphs")
        return self.create_and_execute(
            run.project_id,
            selected_paragraphs=list(run.failed_paragraphs),
            config=config,
        )

    def rerun_selected(
        self,
        project_id: str,
        paragraph_numbers: list[int],
        *,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        return self.create_and_execute(
            project_id, selected_paragraphs=paragraph_numbers, config=config
        )

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

    def lock_selection(
        self, run_id: str, paragraph_no: int, selection: AssetSelection
    ) -> RunManifest:
        return self._pipeline.lock_selection(run_id, paragraph_no, selection)

    def load_manifest(self, run_id: str) -> RunManifest | None:
        return self._pipeline.load_manifest(run_id)

    def pause_after_current(self, run_id: str) -> None:
        self._orchestrator.pause_after_current(run_id)

    def cancel(self, run_id: str) -> None:
        self._orchestrator.cancel(run_id)

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
        selection_config = config or MediaSelectionConfig()
        stored_output_root = str(run.metadata.get("output_root", "")).strip()
        if stored_output_root:
            selection_config.output_root = stored_output_root
        selection_config.should_cancel = (
            lambda run_id=run.run_id: self._orchestrator.is_cancel_requested(run_id)
        )
        return selection_config


__all__ = [
    "AssetDeduper",
    "CallbackCandidateSearchBackend",
    "CandidateSearchBackend",
    "FreeImageCandidateSearchBackend",
    "MediaSelectionConfig",
    "ParagraphMediaPipeline",
    "ParagraphMediaRunService",
]
