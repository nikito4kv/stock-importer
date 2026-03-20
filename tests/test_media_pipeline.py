from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bootstrap import ApplicationContainer, bootstrap_application
from domain.enums import AssetKind, ProviderCapability, RunStage, RunStatus
from domain.models import (
    AssetCandidate,
    AssetSelection,
    ParagraphIntent,
    ParagraphUnit,
    Project,
    QueryBundle,
    ScriptDocument,
)
from pipeline import CallbackCandidateSearchBackend, MediaSelectionConfig
from services.errors import DownloadError, SessionError


def _candidate(
    asset_id: str,
    provider_name: str,
    kind: AssetKind,
    *,
    rank_hint: float,
    title: str,
    source_url: str | None = None,
    sha256: str | None = None,
    perceptual_hash: str | None = None,
    semantic_signature: str | None = None,
) -> AssetCandidate:
    metadata = {
        "title": title,
        "rank_hint": rank_hint,
    }
    if sha256 is not None:
        metadata["sha256"] = sha256
    if perceptual_hash is not None:
        metadata["perceptual_hash"] = perceptual_hash
    if semantic_signature is not None:
        metadata["semantic_signature"] = semantic_signature
    return AssetCandidate(
        asset_id=asset_id,
        provider_name=provider_name,
        kind=kind,
        source_url=source_url or f"https://example.com/{asset_id}",
        license_name="test-license",
        metadata=metadata,
    )


def _paragraph(paragraph_no: int, text: str) -> ParagraphUnit:
    return ParagraphUnit(
        paragraph_no=paragraph_no,
        original_index=paragraph_no,
        text=text,
        intent=ParagraphIntent(
            paragraph_no=paragraph_no,
            subject="river boat",
            action="drifting",
            setting="jungle river",
            primary_video_queries=[f"video query {paragraph_no}"],
            image_queries=[f"image query {paragraph_no}"],
        ),
        query_bundle=QueryBundle(
            video_queries=[f"video query {paragraph_no}"],
            image_queries=[f"image query {paragraph_no}"],
            provider_queries={
                "storyblocks_video": [f"video query {paragraph_no}"],
                "storyblocks_image": [f"storyblocks image query {paragraph_no}"],
                "openverse": [f"free image query {paragraph_no}"],
            },
        ),
    )


class MediaPipelineTests(unittest.TestCase):
    def _create_project(
        self, temp_dir: str, paragraph_count: int = 2
    ) -> tuple[ApplicationContainer, Project]:
        container = bootstrap_application(temp_dir)
        container.media_pipeline._provider_settings.enabled_providers = [
            "storyblocks_video",
            "storyblocks_image",
        ]
        paragraphs = [
            _paragraph(index, f"Paragraph {index}")
            for index in range(1, paragraph_count + 1)
        ]
        project = Project(
            project_id="project-media",
            name="Media Project",
            workspace_path=container.workspace.paths.projects_dir,
            script_document=ScriptDocument(
                source_path=Path(temp_dir) / "story.docx",
                header_text="HEADER",
                paragraphs=paragraphs,
            ),
        )
        return container, container.project_repository.save(project)

    def test_unified_pipeline_builds_manifest_with_primary_supporting_and_fallback_assets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            image_descriptor = container.provider_registry.get("storyblocks_image")
            free_descriptor = container.provider_registry.get("openverse")
            assert video_descriptor is not None
            assert image_descriptor is not None
            assert free_descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "storyblocks_video",
                "storyblocks_image",
                "openverse",
            ]

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=10 - paragraph.paragraph_no,
                            title=f"Video {paragraph.paragraph_no}",
                            semantic_signature=f"video-{paragraph.paragraph_no}",
                        )
                    ],
                )
            )
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_image",
                    capability=ProviderCapability.IMAGE,
                    descriptor=image_descriptor,
                    search_fn=lambda paragraph, query, limit: []
                    if paragraph.paragraph_no == 2
                    else [
                        _candidate(
                            f"sb-image-{paragraph.paragraph_no}",
                            "storyblocks_image",
                            AssetKind.IMAGE,
                            rank_hint=8.0,
                            title=f"Storyblocks image {paragraph.paragraph_no}",
                        )
                    ],
                )
            )
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=free_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"free-image-{paragraph.paragraph_no}",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=6.0,
                            title=f"Free image {paragraph.paragraph_no}",
                        )
                    ],
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    supporting_image_limit=1,
                    fallback_image_limit=1,
                    max_candidates_per_provider=4,
                    top_k_to_relevance=3,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(
                [entry.paragraph_no for entry in manifest.paragraph_entries], [1, 2]
            )
            first = manifest.paragraph_entries[0]
            second = manifest.paragraph_entries[1]
            assert first.selection is not None
            assert second.selection is not None
            assert first.selection.primary_asset is not None
            self.assertEqual(
                first.selection.primary_asset.provider_name, "storyblocks_video"
            )
            self.assertEqual(
                first.selection.supporting_assets[0].provider_name, "storyblocks_image"
            )
            self.assertEqual(
                second.selection.fallback_assets[0].provider_name, "openverse"
            )
            self.assertEqual(first.slots[0].slot_id, "primary_video")
            self.assertEqual(manifest.summary["primary_videos"], 2)

    def test_pipeline_downloads_storyblocks_primary_video_to_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            class DownloadingBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    local_path = destination_dir / filename
                    local_path.write_bytes(b"video-bytes")
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline.register_backend(
                DownloadingBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title=f"Video {paragraph.paragraph_no}",
                        )
                    ],
                )
            )

            output_root = Path(temp_dir) / "exports"
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(output_root=str(output_root)),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            assert entry.selection.primary_asset is not None
            self.assertIsNotNone(entry.selection.primary_asset.local_path)
            assert entry.selection.primary_asset.local_path is not None
            self.assertTrue(entry.selection.primary_asset.local_path.exists())
            self.assertIn(
                str(output_root / "media-project"),
                str(entry.selection.primary_asset.local_path),
            )
            self.assertIn(
                str(output_root / "media-project" / "downloads" / "videos"),
                str(entry.selection.primary_asset.local_path),
            )
            self.assertNotIn(
                "paragraph_001", str(entry.selection.primary_asset.local_path)
            )
            self.assertIn(
                "p001_storyblocks-video_video-query-1",
                entry.selection.primary_asset.local_path.name,
            )

    def test_pipeline_downloads_selected_images_to_shared_images_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            image_descriptor = container.provider_registry.get("openverse")
            assert image_descriptor is not None

            class DownloadingImageBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    local_path = destination_dir / filename
                    local_path.write_bytes(b"image-bytes")
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline._provider_settings.free_images_only = True
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]
            container.media_pipeline.register_backend(
                DownloadingImageBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=image_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"image-{paragraph.paragraph_no}",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=8.0,
                            title=f"Image {paragraph.paragraph_no}",
                            source_url=f"https://example.com/image-{paragraph.paragraph_no}.jpg",
                        )
                    ],
                )
            )

            output_root = Path(temp_dir) / "exports"
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                    output_root=str(output_root),
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            self.assertTrue(entry.selection.supporting_assets)
            image = entry.selection.supporting_assets[0]
            assert image.local_path is not None
            self.assertTrue(image.local_path.exists())
            self.assertIn(
                str(output_root / "media-project" / "downloads" / "images"),
                str(image.local_path),
            )
            self.assertNotIn("paragraph_001", str(image.local_path))
            self.assertIn("p001_openverse_free-image-query-1", image.local_path.name)

    def test_free_image_backend_rejects_html_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, _project = self._create_project(temp_dir, paragraph_count=1)
            backend = container.media_pipeline._download_backends.get("openverse")
            self.assertIsNotNone(backend)
            asset = _candidate(
                "image-html",
                "openverse",
                AssetKind.IMAGE,
                rank_hint=8.0,
                title="Broken image",
                source_url="https://example.com/not-image",
            )

            class _FakeResponse(io.BytesIO):
                def __init__(self, payload: bytes, headers: dict[str, str]):
                    super().__init__(payload)
                    self.headers = headers

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    self.close()
                    return None

            assert backend is not None
            with patch(
                "pipeline.media.open_with_safe_redirects",
                return_value=(
                    _FakeResponse(
                        b"<html>login wall</html>",
                        {"Content-Type": "text/html; charset=utf-8"},
                    ),
                    asset.source_url,
                ),
            ):
                with self.assertRaises(DownloadError) as ctx:
                    backend.download_asset(
                        asset,
                        destination_dir=Path(temp_dir),
                        filename="broken.jpg",
                    )

            self.assertEqual(ctx.exception.code, "direct_image_download_failed")

    def test_pipeline_output_root_uses_project_name_instead_of_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            class DownloadingBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    local_path = destination_dir / filename
                    local_path.write_bytes(b"video-bytes")
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline.register_backend(
                DownloadingBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title=f"Video {paragraph.paragraph_no}",
                        )
                    ],
                )
            )

            output_root = Path(temp_dir) / "exports"
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(output_root=str(output_root)),
            )

            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            assert entry.selection.primary_asset is not None
            assert entry.selection.primary_asset.local_path is not None

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(
                entry.selection.primary_asset.local_path.parent.name, "videos"
            )
            self.assertEqual(
                entry.selection.primary_asset.local_path.parents[2].name,
                "media-project",
            )
            self.assertNotIn(run.run_id, str(entry.selection.primary_asset.local_path))

    def test_pipeline_output_root_preserves_cyrillic_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            project.name = "Мой сценарий"
            project = container.project_repository.save(project)

            class DownloadingBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    local_path = destination_dir / filename
                    local_path.write_bytes(b"video-bytes")
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline.register_backend(
                DownloadingBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title=f"Video {paragraph.paragraph_no}",
                        )
                    ],
                )
            )

            output_root = Path(temp_dir) / "exports"
            _run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(output_root=str(output_root)),
            )

            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            assert entry.selection.primary_asset is not None
            assert entry.selection.primary_asset.local_path is not None

            self.assertEqual(
                entry.selection.primary_asset.local_path.parents[2].name,
                "мой-сценарий",
            )

    def test_user_locked_selection_is_preserved_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "dup-video",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title="Duplicate video",
                            sha256="same-hash",
                            perceptual_hash="same-phash",
                            semantic_signature="same-semantic",
                        )
                    ],
                )
            )

            run, manifest = container.media_run_service.create_run(project.project_id)
            locked = AssetSelection(
                paragraph_no=1,
                primary_asset=_candidate(
                    "locked-video",
                    "storyblocks_video",
                    AssetKind.VIDEO,
                    rank_hint=100.0,
                    title="Locked video",
                    semantic_signature="locked-video",
                ),
                reason="User locked selection",
                user_locked=True,
                status="locked",
                user_decision_status="locked",
            )
            container.media_run_service.lock_selection(run.run_id, 1, locked)
            completed_run, manifest = container.media_run_service.execute(run.run_id)

            self.assertEqual(completed_run.status, RunStatus.COMPLETED)
            first = manifest.paragraph_entries[0]
            assert first.selection is not None
            assert first.selection.primary_asset is not None
            self.assertTrue(first.selection.user_locked)
            self.assertEqual(first.selection.primary_asset.asset_id, "locked-video")

    def test_dedupe_rejects_duplicate_assets_across_paragraphs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"dup-video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title="Duplicate video",
                            sha256="same-hash",
                            perceptual_hash="same-phash",
                            semantic_signature="same-semantic",
                        )
                    ],
                )
            )

            completed_run, manifest = container.media_run_service.create_and_execute(
                project.project_id
            )

            self.assertEqual(completed_run.status, RunStatus.COMPLETED)
            first = manifest.paragraph_entries[0]
            second = manifest.paragraph_entries[1]
            assert first.selection is not None
            assert second.selection is not None
            assert second.diagnostics is not None
            assert first.selection.primary_asset is not None
            self.assertEqual(first.selection.primary_asset.asset_id, "dup-video-1")
            self.assertIsNone(second.selection.primary_asset)
            self.assertEqual(
                second.diagnostics.dedupe_rejections.get("raw_file_hash"), 1
            )

    def test_pause_resume_and_retry_failed_only_keep_manifest_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=3)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None
            state = {"fail": True}

            def flaky_search(
                paragraph: ParagraphUnit, query: str, limit: int
            ) -> list[AssetCandidate]:
                if paragraph.paragraph_no == 2 and state["fail"]:
                    raise RuntimeError("provider offline")
                return [
                    _candidate(
                        f"video-{paragraph.paragraph_no}",
                        "storyblocks_video",
                        AssetKind.VIDEO,
                        rank_hint=10.0,
                        title=f"Video {paragraph.paragraph_no}",
                    )
                ]

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=flaky_search,
                )
            )

            run, _ = container.media_run_service.create_run(project.project_id)
            container.media_run_service.pause_after_current(run.run_id)
            paused_run, paused_manifest = container.media_run_service.execute(
                run.run_id
            )

            self.assertEqual(paused_run.status, RunStatus.PAUSED)
            self.assertEqual(paused_manifest.summary["paragraphs_completed"], 1)
            self.assertNotIn(run.run_id, container.media_pipeline._run_dedupers)
            self.assertNotIn(
                run.run_id, container.media_pipeline._manifest_entry_indexes
            )

            resumed_run, failed_manifest = container.media_run_service.resume(
                run.run_id
            )
            self.assertEqual(resumed_run.status, RunStatus.FAILED)
            self.assertEqual(sorted(resumed_run.completed_paragraphs), [1, 3])
            self.assertEqual(resumed_run.failed_paragraphs, [2])
            self.assertEqual(failed_manifest.summary["paragraphs_failed"], 1)
            assert failed_manifest.paragraph_entries[0].selection is not None
            assert (
                failed_manifest.paragraph_entries[0].selection.primary_asset is not None
            )
            self.assertEqual(
                failed_manifest.paragraph_entries[0].selection.primary_asset.asset_id,
                "video-1",
            )
            self.assertEqual(failed_manifest.paragraph_entries[1].status, "failed")
            assert failed_manifest.paragraph_entries[2].selection is not None
            assert (
                failed_manifest.paragraph_entries[2].selection.primary_asset is not None
            )
            self.assertEqual(
                failed_manifest.paragraph_entries[2].selection.primary_asset.asset_id,
                "video-3",
            )

            state["fail"] = False
            retried_run, retried_manifest = (
                container.media_run_service.retry_failed_only(run.run_id)
            )

            self.assertEqual(retried_run.status, RunStatus.COMPLETED)
            self.assertEqual(
                [entry.paragraph_no for entry in retried_manifest.paragraph_entries],
                [2],
            )
            assert retried_manifest.paragraph_entries[0].selection is not None
            assert (
                retried_manifest.paragraph_entries[0].selection.primary_asset
                is not None
            )
            self.assertEqual(
                retried_manifest.paragraph_entries[0].selection.primary_asset.asset_id,
                "video-2",
            )

    def test_storyblocks_video_download_falls_back_to_next_candidate_when_top_download_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            class FallbackDownloadingBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    if asset.asset_id == "video-a":
                        raise DownloadError(
                            code="storyblocks_download_failed",
                            message="first candidate download failed",
                        )
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    local_path = destination_dir / filename
                    local_path.write_bytes(b"video-b")
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline.register_backend(
                FallbackDownloadingBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "video-a",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title="Video A",
                        ),
                        _candidate(
                            "video-b",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=8.0,
                            title="Video B",
                        ),
                    ],
                )
            )

            output_root = Path(temp_dir) / "exports"
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(output_root=str(output_root)),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            assert entry.selection.primary_asset is not None
            self.assertEqual(entry.selection.primary_asset.asset_id, "video-b")
            self.assertTrue(
                any(
                    "video-a" in reason and "download failed" in reason
                    for reason in entry.rejection_reasons
                )
            )

    def test_storyblocks_video_search_failure_marks_only_current_paragraph_failed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=3)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            def flaky_search(
                paragraph: ParagraphUnit, query: str, limit: int
            ) -> list[AssetCandidate]:
                if paragraph.paragraph_no == 2:
                    raise SessionError(
                        code="storyblocks_session_expired",
                        message="session expired on paragraph 2",
                    )
                return [
                    _candidate(
                        f"video-{paragraph.paragraph_no}",
                        "storyblocks_video",
                        AssetKind.VIDEO,
                        rank_hint=9.0,
                        title=f"Video {paragraph.paragraph_no}",
                    )
                ]

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=flaky_search,
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id
            )

            self.assertEqual(run.status, RunStatus.FAILED)
            self.assertEqual(sorted(run.completed_paragraphs), [1, 3])
            self.assertEqual(run.failed_paragraphs, [2])
            self.assertEqual(manifest.paragraph_entries[1].status, "failed")
            self.assertEqual(manifest.paragraph_entries[2].status, "selected")

    def test_image_enabled_mode_creates_shared_images_dir_even_when_no_images_are_selected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            image_descriptor = container.provider_registry.get("storyblocks_image")
            assert image_descriptor is not None

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_image",
                    capability=ProviderCapability.IMAGE,
                    descriptor=image_descriptor,
                    search_fn=lambda paragraph, query, limit: [],
                )
            )

            output_root = Path(temp_dir) / "exports"
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=True,
                    free_images_enabled=False,
                    output_root=str(output_root),
                ),
            )

            images_dir = output_root / "media-project" / "downloads" / "images"
            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertTrue(images_dir.exists())
            self.assertTrue(images_dir.is_dir())
            self.assertIn(
                "storyblocks_image:no_results",
                manifest.paragraph_entries[0].rejection_reasons,
            )

    def test_free_image_download_failure_is_recorded_in_manifest_rejection_reasons(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            image_descriptor = container.provider_registry.get("openverse")
            assert image_descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]

            class BrokenDownloadingImageBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    raise DownloadError(
                        code="direct_image_download_failed",
                        message="image download failed",
                    )

            container.media_pipeline.register_backend(
                BrokenDownloadingImageBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=image_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"image-{paragraph.paragraph_no}",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=8.0,
                            title="Broken image",
                        )
                    ],
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertTrue(
                any(
                    reason.startswith("openverse:") and "download_failed" in reason
                    for reason in manifest.paragraph_entries[0].rejection_reasons
                )
            )

    def test_pipeline_seeds_run_deduper_once_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=3)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title=f"Video {paragraph.paragraph_no}",
                            semantic_signature=f"video-{paragraph.paragraph_no}",
                        )
                    ],
                )
            )

            with patch.object(
                container.media_pipeline,
                "_seed_run_deduper",
                wraps=container.media_pipeline._seed_run_deduper,
            ) as seed_deduper:
                run, _manifest = container.media_run_service.create_and_execute(
                    project.project_id,
                    config=MediaSelectionConfig(
                        storyblocks_images_enabled=False,
                        free_images_enabled=False,
                    ),
                )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(seed_deduper.call_count, 1)
            self.assertNotIn(run.run_id, container.media_pipeline._run_dedupers)
            self.assertNotIn(
                run.run_id, container.media_pipeline._manifest_entry_indexes
            )

    def test_resume_keeps_deduper_seeded_after_locked_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=3)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "video-1"
                            if paragraph.paragraph_no == 3
                            else f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title=f"Video {paragraph.paragraph_no}",
                            semantic_signature=(
                                "video-1"
                                if paragraph.paragraph_no == 3
                                else f"video-{paragraph.paragraph_no}"
                            ),
                        )
                    ],
                )
            )

            run, manifest = container.media_run_service.create_run(project.project_id)
            first = manifest.paragraph_entries[0]
            first.selection = AssetSelection(
                paragraph_no=1,
                primary_asset=_candidate(
                    "video-1",
                    "storyblocks_video",
                    AssetKind.VIDEO,
                    rank_hint=9.0,
                    title="Video 1",
                    semantic_signature="video-1",
                ),
                status="selected",
            )
            first.status = "selected"

            second = manifest.paragraph_entries[1]
            second.selection = AssetSelection(
                paragraph_no=2,
                primary_asset=_candidate(
                    "video-2",
                    "storyblocks_video",
                    AssetKind.VIDEO,
                    rank_hint=9.0,
                    title="Video 2",
                    semantic_signature="video-2",
                ),
                status="locked",
                user_locked=True,
                user_decision_status="locked",
            )
            second.status = "locked"
            second.user_decision_status = "locked"

            container.media_pipeline.save_manifest(manifest)
            run.completed_paragraphs = [1]
            container.run_repository.save(run)

            resumed_run, resumed_manifest = container.media_run_service.resume(
                run.run_id,
                config=MediaSelectionConfig(
                    storyblocks_images_enabled=False,
                    free_images_enabled=False,
                ),
            )

            self.assertEqual(resumed_run.status, RunStatus.COMPLETED)
            third = resumed_manifest.paragraph_entries[2]
            assert third.selection is not None
            assert third.diagnostics is not None
            self.assertEqual(third.status, "no_match")
            self.assertIsNone(third.selection.primary_asset)
            self.assertEqual(third.diagnostics.dedupe_rejections.get("source_id"), 1)

    def test_execute_persists_run_without_extra_final_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            assert video_descriptor is not None
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"video-{paragraph.paragraph_no}",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title=f"Video {paragraph.paragraph_no}",
                        )
                    ],
                )
            )
            run, _manifest = container.media_run_service.create_run(project.project_id)
            with patch.object(
                container.run_repository,
                "save",
                wraps=container.run_repository.save,
            ) as save_run:
                completed_run, _ = container.media_run_service.execute(run.run_id)

            self.assertEqual(completed_run.status, RunStatus.COMPLETED)
            self.assertEqual(save_run.call_count, 2)

    def test_cancelled_run_persists_checkpoint_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=2)
            run, _manifest = container.media_run_service.create_run(project.project_id)
            container.media_run_service.cancel(run.run_id)

            cancelled_run, cancelled_manifest = container.media_run_service.execute(
                run.run_id
            )

            self.assertEqual(cancelled_run.status, RunStatus.CANCELLED)
            self.assertIsNotNone(cancelled_run.checkpoint)
            assert cancelled_run.checkpoint is not None
            self.assertEqual(cancelled_run.checkpoint.stage, RunStage.PERSIST)
            self.assertEqual(cancelled_run.checkpoint.completed_paragraphs, [])
            self.assertEqual(cancelled_run.checkpoint.failed_paragraphs, [])
            self.assertEqual(cancelled_manifest.summary["paragraphs_processed"], 0)

    def test_free_image_mode_allows_parallel_workers_above_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]
            container.orchestrator.configure(max_workers=3, queue_size=3)
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            f"openverse-{paragraph.paragraph_no}",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=8.0,
                            title=f"Openverse {paragraph.paragraph_no}",
                        )
                    ],
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(len(run.completed_paragraphs), 1)
            self.assertEqual(manifest.summary["paragraphs_completed"], 1)

    def test_free_image_mode_uses_parallel_provider_pool_and_records_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            openverse_descriptor = container.provider_registry.get("openverse")
            wikimedia_descriptor = container.provider_registry.get("wikimedia")
            assert openverse_descriptor is not None
            assert wikimedia_descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse",
                "wikimedia",
            ]

            active_calls = 0
            max_active_calls = 0
            guard = threading.Lock()

            def slow_search(
                paragraph: ParagraphUnit,
                query: str,
                limit: int,
            ) -> list[AssetCandidate]:
                nonlocal active_calls, max_active_calls
                with guard:
                    active_calls += 1
                    max_active_calls = max(max_active_calls, active_calls)
                try:
                    time.sleep(0.08)
                    return [
                        _candidate(
                            f"{query}-{paragraph.paragraph_no}",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=6.0,
                            title="parallel",
                        )
                    ]
                finally:
                    with guard:
                        active_calls -= 1

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=openverse_descriptor,
                    search_fn=slow_search,
                )
            )
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="wikimedia",
                    capability=ProviderCapability.IMAGE,
                    descriptor=wikimedia_descriptor,
                    search_fn=slow_search,
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                    early_stop_when_satisfied=False,
                    provider_workers=4,
                    provider_queue_size=4,
                    supporting_image_limit=0,
                    fallback_image_limit=1,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertGreaterEqual(max_active_calls, 2)
            self.assertEqual(run.metadata.get("concurrency_mode"), "free_images_parallel")
            self.assertEqual(
                manifest.sourcing_strategy.get("concurrency_mode"),
                "free_images_parallel",
            )

    def test_parallel_image_downloads_keep_deterministic_asset_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = ["openverse"]

            class OrderedDownloadingBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    if asset.asset_id == "img-high":
                        time.sleep(0.12)
                    else:
                        time.sleep(0.02)
                    local_path = destination_dir / filename
                    local_path.write_bytes(asset.asset_id.encode("utf-8"))
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline.register_backend(
                OrderedDownloadingBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "img-high",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=9.0,
                            title="high",
                        ),
                        _candidate(
                            "img-low",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=8.0,
                            title="low",
                        ),
                    ],
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                    supporting_image_limit=0,
                    fallback_image_limit=2,
                    download_workers=2,
                    bounded_downloads=2,
                    early_stop_when_satisfied=False,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            self.assertEqual(
                [asset.asset_id for asset in entry.selection.fallback_assets],
                ["img-high", "img-low"],
            )

    def test_fallback_search_uses_fallback_limit_for_early_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            openverse_descriptor = container.provider_registry.get("openverse")
            wikimedia_descriptor = container.provider_registry.get("wikimedia")
            assert openverse_descriptor is not None
            assert wikimedia_descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse",
                "wikimedia",
            ]

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=openverse_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "fallback-a",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=9.0,
                            title="Fallback A",
                        )
                    ],
                )
            )
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="wikimedia",
                    capability=ProviderCapability.IMAGE,
                    descriptor=wikimedia_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "fallback-b",
                            "wikimedia",
                            AssetKind.IMAGE,
                            rank_hint=8.5,
                            title="Fallback B",
                        )
                    ],
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                    supporting_image_limit=0,
                    fallback_image_limit=2,
                    early_stop_when_satisfied=True,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            self.assertEqual(
                [asset.asset_id for asset in entry.selection.fallback_assets],
                ["fallback-a", "fallback-b"],
            )

    def test_storyblocks_image_session_error_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("storyblocks_image")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "storyblocks_image"
            ]
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_image",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: (_ for _ in ()).throw(
                        SessionError(
                            code="storyblocks_session_expired",
                            message="Storyblocks session expired",
                        )
                    ),
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=True,
                    free_images_enabled=False,
                    retry_budget=0,
                ),
            )

            self.assertEqual(run.status, RunStatus.FAILED)
            self.assertEqual(run.failed_paragraphs, [1])
            self.assertEqual(manifest.paragraph_entries[0].status, "failed")
            self.assertIn("Storyblocks session expired", run.last_error or "")

    def test_search_timeout_uses_configured_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("storyblocks_video")
            assert descriptor is not None
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: (
                        time.sleep(0.2),
                        [
                            _candidate(
                                "slow-video",
                                "storyblocks_video",
                                AssetKind.VIDEO,
                                rank_hint=9.0,
                                title="Slow Video",
                            )
                        ],
                    )[1],
                )
            )

            started_at = time.perf_counter()
            run, _manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    storyblocks_images_enabled=False,
                    free_images_enabled=False,
                    search_timeout_seconds=0.05,
                    retry_budget=0,
                ),
            )
            elapsed = time.perf_counter() - started_at

            self.assertEqual(run.status, RunStatus.FAILED)
            self.assertLess(elapsed, 0.15)
            self.assertIn("search timeout exceeded", run.last_error or "")

    def test_download_timeout_marks_image_candidate_failed_quickly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = ["openverse"]

            class SlowDownloadingBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    time.sleep(0.2)
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    local_path = destination_dir / filename
                    local_path.write_bytes(b"slow-image")
                    downloaded = AssetCandidate.from_dict(asset.to_dict())
                    downloaded.local_path = local_path
                    downloaded.metadata["download_status"] = "completed"
                    return downloaded

            container.media_pipeline.register_backend(
                SlowDownloadingBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "slow-image",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=9.0,
                            title="Slow Image",
                        )
                    ],
                )
            )

            started_at = time.perf_counter()
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=False,
                    free_images_enabled=True,
                    supporting_image_limit=0,
                    fallback_image_limit=1,
                    download_timeout_seconds=0.05,
                    retry_budget=0,
                ),
            )
            elapsed = time.perf_counter() - started_at

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertLess(elapsed, 0.15)
            self.assertEqual(manifest.paragraph_entries[0].status, "no_match")
            self.assertTrue(
                any(
                    "download timeout exceeded" in reason
                    for reason in manifest.paragraph_entries[0].rejection_reasons
                )
            )

    def test_relevance_timeout_degrades_without_blocking_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = ["openverse"]
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "img-1",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=9.0,
                            title="Image 1",
                        ),
                        _candidate(
                            "img-2",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=8.0,
                            title="Image 2",
                        ),
                        _candidate(
                            "img-3",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=7.0,
                            title="Image 3",
                        ),
                        _candidate(
                            "img-4",
                            "openverse",
                            AssetKind.IMAGE,
                            rank_hint=6.0,
                            title="Image 4",
                        ),
                    ],
                )
            )

            def slow_rank(asset: AssetCandidate) -> float:
                time.sleep(0.05)
                return float(asset.metadata["rank_hint"])

            started_at = time.perf_counter()
            with patch.object(container.media_pipeline, "_asset_rank", side_effect=slow_rank):
                run, manifest = container.media_run_service.create_and_execute(
                    project.project_id,
                    config=MediaSelectionConfig(
                        video_enabled=False,
                        storyblocks_images_enabled=False,
                        free_images_enabled=True,
                        supporting_image_limit=0,
                        fallback_image_limit=2,
                        relevance_workers=2,
                        bounded_relevance_queue=4,
                        relevance_timeout_seconds=0.01,
                        early_stop_when_satisfied=False,
                    ),
                )
            elapsed = time.perf_counter() - started_at

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertLess(elapsed, 0.15)
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            self.assertEqual(
                [asset.asset_id for asset in entry.selection.fallback_assets],
                ["img-1", "img-2"],
            )
            degraded_events = [
                event
                for event in container.event_recorder.by_run(run.run_id)
                if event.name == "paragraph.relevance.degraded"
            ]
            self.assertTrue(degraded_events)
            self.assertTrue(degraded_events[-1].payload.get("relevance_timed_out"))
            self.assertGreater(
                int(degraded_events[-1].payload.get("relevance_tasks_timed_out", 0)),
                0,
            )

    def test_no_match_reason_counter_uses_low_cardinality_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir, paragraph_count=1)
            video_descriptor = container.provider_registry.get("storyblocks_video")
            image_descriptor = container.provider_registry.get("storyblocks_image")
            assert video_descriptor is not None
            assert image_descriptor is not None

            class BrokenVideoBackend(CallbackCandidateSearchBackend):
                def download_asset(
                    self, asset: AssetCandidate, *, destination_dir: Path, filename: str
                ) -> AssetCandidate:
                    raise DownloadError(
                        code="storyblocks_download_failed",
                        message=f"asset {asset.asset_id} download failed",
                    )

            container.media_pipeline.register_backend(
                BrokenVideoBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=video_descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        _candidate(
                            "video-a",
                            "storyblocks_video",
                            AssetKind.VIDEO,
                            rank_hint=9.0,
                            title="Video A",
                        )
                    ],
                )
            )
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_image",
                    capability=ProviderCapability.IMAGE,
                    descriptor=image_descriptor,
                    search_fn=lambda paragraph, query, limit: [],
                )
            )

            run, _manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    storyblocks_images_enabled=True,
                    free_images_enabled=False,
                ),
            )
            self.assertEqual(run.status, RunStatus.COMPLETED)
            reloaded = container.run_repository.load(run.run_id)
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            perf_context = reloaded.metadata.get("performance_context")
            self.assertIsInstance(perf_context, dict)
            assert isinstance(perf_context, dict)
            counters = dict(perf_context.get("counters") or {})
            reason_keys = [key for key in counters if key.startswith("no_match_reason_")]
            self.assertIn("no_match_reason_download_failed", reason_keys)
            self.assertFalse(any("video_a" in key for key in reason_keys))


if __name__ == "__main__":
    unittest.main()
