from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bootstrap import ApplicationContainer, bootstrap_application
from browser import (
    BrowserChannelResolver,
    BrowserSessionManager,
    PersistentBrowserHandle,
    StoryblocksDomContractChecker,
    StoryblocksDownloadManager,
    StoryblocksDownloadRequest,
    StoryblocksSessionProbe,
    StoryblocksVideoSearchAdapter,
)
from domain.enums import (
    AssetKind,
    ProviderCapability,
    RunStage,
    RunStatus,
    SessionHealth,
)
from domain.models import (
    AssetCandidate,
    AssetSelection,
    MediaSlot,
    ParagraphDiagnostics,
    ParagraphIntent,
    ParagraphManifestEntry,
    ParagraphUnit,
    Preset,
    Project,
    ProviderResult,
    QueryBundle,
    Run,
    RunManifest,
    ScriptDocument,
)
from pipeline import CallbackCandidateSearchBackend, MediaSelectionConfig
from services import SecretStore, SettingsManager
from services.errors import ConfigError
from storage import (
    ManifestRepository,
    PresetRepository,
    ProjectRepository,
    RunRepository,
    SettingsRepository,
    WorkspaceStorage,
)
from storage.serialization import read_json, write_json
from ui.controller import DesktopGuiController

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "storyblocks"


def _fixture(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


def _candidate(
    asset_id: str, provider_name: str, kind: AssetKind, rank_hint: float
) -> AssetCandidate:
    return AssetCandidate(
        asset_id=asset_id,
        provider_name=provider_name,
        kind=kind,
        source_url=f"https://example.com/{asset_id}",
        local_path=Path("cache") / f"{asset_id}.bin",
        license_name="test-license",
        metadata={
            "title": asset_id,
            "rank_hint": rank_hint,
            "semantic_signature": asset_id,
        },
    )


def _paragraph(paragraph_no: int, text: str) -> ParagraphUnit:
    return ParagraphUnit(
        paragraph_no=paragraph_no,
        original_index=paragraph_no + 1,
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


class _FakePage:
    def __init__(self, html: str = "", url: str = "about:blank"):
        self._html = html
        self.url = url

    def goto(self, url: str) -> None:
        self.url = url

    def content(self) -> str:
        return self._html


class _FakeContextFactory:
    def __init__(self, page: _FakePage):
        self._page = page

    def launch(self, plan) -> PersistentBrowserHandle:
        return PersistentBrowserHandle(
            context=object(), page=self._page, close_callback=lambda: None
        )


class _ZeroByteDownloadDriver:
    def download(self, request: StoryblocksDownloadRequest) -> bytes:
        return b""


class Phase9ReliabilityTests(unittest.TestCase):
    def _create_project(
        self,
        container: ApplicationContainer,
        temp_dir: str,
        *,
        paragraph_count: int = 3,
    ) -> Project:
        project = Project(
            project_id="phase9-project",
            name="Phase 9",
            workspace_path=container.workspace.paths.projects_dir,
            script_document=ScriptDocument(
                source_path=Path(temp_dir) / "story.docx",
                header_text="HEADER",
                paragraphs=[
                    _paragraph(index, f"Paragraph {index}")
                    for index in range(1, paragraph_count + 1)
                ],
            ),
        )
        return container.project_repository.save(project)

    def _register_video_backend(self, container: ApplicationContainer) -> None:
        descriptor = container.provider_registry.get("storyblocks_video")
        assert descriptor is not None
        container.media_pipeline.register_backend(
            CallbackCandidateSearchBackend(
                provider_id="storyblocks_video",
                capability=ProviderCapability.VIDEO,
                descriptor=descriptor,
                search_fn=lambda paragraph, query, limit: [
                    _candidate(
                        f"video-{paragraph.paragraph_no}",
                        "storyblocks_video",
                        AssetKind.VIDEO,
                        10.0,
                    )
                ],
            )
        )

    def _register_image_backends(self, container: ApplicationContainer) -> None:
        storyblocks_descriptor = container.provider_registry.get("storyblocks_image")
        free_descriptor = container.provider_registry.get("openverse")
        assert storyblocks_descriptor is not None
        assert free_descriptor is not None
        container.media_pipeline.register_backend(
            CallbackCandidateSearchBackend(
                provider_id="storyblocks_image",
                capability=ProviderCapability.IMAGE,
                descriptor=storyblocks_descriptor,
                search_fn=lambda paragraph, query, limit: [
                    _candidate(
                        f"sb-image-{paragraph.paragraph_no}",
                        "storyblocks_image",
                        AssetKind.IMAGE,
                        9.0,
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
                        7.0,
                    )
                ],
            )
        )

    def test_domain_models_roundtrip_preserves_nested_types_and_legacy_run_mapping(
        self,
    ) -> None:
        selection = AssetSelection(
            paragraph_no=1,
            primary_asset=_candidate(
                "video-1", "storyblocks_video", AssetKind.VIDEO, 10.0
            ),
            supporting_assets=[
                _candidate("image-1", "storyblocks_image", AssetKind.IMAGE, 8.0)
            ],
            media_slots=[
                MediaSlot(
                    slot_id="primary_video",
                    kind=AssetKind.VIDEO,
                    role="primary",
                    required=True,
                )
            ],
            provider_results=[
                ProviderResult(
                    provider_name="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    query="river boat",
                    candidates=[
                        _candidate(
                            "video-1", "storyblocks_video", AssetKind.VIDEO, 10.0
                        )
                    ],
                )
            ],
            rejection_reasons=["none"],
            diagnostics={"score": 0.9},
            reason="User approved selection",
            status="completed",
        )
        manifest = RunManifest(
            run_id="run-1",
            project_id="project-1",
            project_name="Phase 9",
            paragraph_entries=[
                ParagraphManifestEntry(
                    paragraph_no=1,
                    original_index=2,
                    text="Paragraph 1",
                    intent=ParagraphIntent(paragraph_no=1, subject="river boat"),
                    query_bundle=QueryBundle(
                        video_queries=["river boat"], image_queries=["river boat photo"]
                    ),
                    slots=[
                        MediaSlot(
                            slot_id="primary_video",
                            kind=AssetKind.VIDEO,
                            role="primary",
                        )
                    ],
                    selection=selection,
                    diagnostics=ParagraphDiagnostics(
                        paragraph_no=1, selected_from_provider="storyblocks_video"
                    ),
                    status="completed",
                )
            ],
            summary={"paragraphs_total": 1, "paragraphs_completed": 1},
        )
        legacy_run_payload = {
            "run_id": "run-1",
            "project_id": "project-1",
            "status": "paused",
            "stage": RunStage.PERSIST.value,
            "checkpoint": {
                "run_id": "run-1",
                "stage": RunStage.PERSIST.value,
                "current_paragraph_no": 1,
            },
        }
        legacy_decision_key = "user_decision" + "_status"
        legacy_fallback_key = "fallback" + "_options"

        legacy_manifest_payload = manifest.to_dict()
        self.assertNotIn(
            "user_locked",
            legacy_manifest_payload["paragraph_entries"][0]["selection"],
        )
        legacy_manifest_payload["paragraph_entries"][0]["status"] = "locked"
        legacy_manifest_payload["paragraph_entries"][0][legacy_decision_key] = "locked"
        legacy_manifest_payload["paragraph_entries"][0][legacy_fallback_key] = [
            _candidate("fallback-1", "openverse", AssetKind.IMAGE, 5.0).to_dict()
        ]
        selection_payload = legacy_manifest_payload["paragraph_entries"][0]["selection"]
        selection_payload["status"] = "selected"
        selection_payload[legacy_decision_key] = "locked"
        selection_payload["user_locked"] = True

        restored_manifest = RunManifest.from_dict(legacy_manifest_payload)
        restored_run = Run.from_dict(legacy_run_payload)

        restored_selection = restored_manifest.paragraph_entries[0].selection
        self.assertIsNotNone(restored_selection)
        assert restored_selection is not None
        self.assertFalse(hasattr(restored_selection, "user_locked"))
        self.assertFalse(
            hasattr(restored_manifest.paragraph_entries[0].slots[0], "user_locked")
        )
        self.assertEqual(restored_selection.primary_asset.kind, AssetKind.VIDEO)
        self.assertIsInstance(restored_selection.primary_asset.local_path, Path)
        self.assertEqual(
            restored_selection.provider_results[0].capability, ProviderCapability.VIDEO
        )
        self.assertEqual(restored_run.status, RunStatus.CANCELLED)
        self.assertEqual(restored_run.stage, RunStage.PERSIST)
        self.assertFalse(hasattr(restored_run, "checkpoint"))
        self.assertEqual(restored_run.metadata.get("legacy_status"), "paused")

    def test_settings_manager_merges_partial_snapshot_and_secrets_roundtrip(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = WorkspaceStorage(temp_dir).initialize()
            manager = SettingsManager(
                SettingsRepository(paths),
                PresetRepository(paths),
                SecretStore(paths.secrets_dir),
            )
            settings = manager.get_or_create()
            manager.save_preset(
                Preset(
                    name="phase9",
                    settings_snapshot={
                        "browser": {"slow_mode": False},
                        "providers": {
                            "enabled_providers": ["openverse"],
                            "free_images_only": True,
                        },
                        "concurrency": {"queue_size": 2},
                    },
                )
            )

            applied = manager.apply_preset(settings, "phase9")
            manager.set_secret("phase9_secret", "secret-value-123456")

            self.assertFalse(applied.browser.slow_mode)
            self.assertEqual(applied.providers.enabled_providers, ["openverse"])
            self.assertTrue(applied.providers.free_images_only)
            self.assertEqual(applied.concurrency.queue_size, 2)
            self.assertEqual(
                applied.browser.launch_timeout_ms, settings.browser.launch_timeout_ms
            )
            self.assertEqual(manager.get_secret("phase9_secret"), "secret-value-123456")

    def test_write_json_preserves_previous_payload_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            write_json(path, {"stable": True})

            with patch(
                "storage.serialization.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(OSError):
                    write_json(path, {"stable": False})

            self.assertEqual(read_json(path), {"stable": True})
            self.assertEqual(sorted(path.parent.glob("*.tmp")), [])

    def test_repositories_roundtrip_across_workspace_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = WorkspaceStorage(temp_dir).initialize()
            project_repository = ProjectRepository(paths)
            run_repository = RunRepository(paths)
            manifest_repository = ManifestRepository(paths)
            preset_repository = PresetRepository(paths)

            project = project_repository.save(
                Project(
                    project_id="project-storage",
                    name="Storage",
                    workspace_path=paths.projects_dir,
                    script_document=ScriptDocument(
                        source_path=Path(temp_dir) / "story.docx", header_text="HEADER"
                    ),
                )
            )
            run = run_repository.save(
                Run(
                    run_id="run-storage",
                    project_id=project.project_id,
                    status=RunStatus.RUNNING,
                )
            )
            manifest = manifest_repository.save(
                RunManifest(
                    run_id=run.run_id,
                    project_id=project.project_id,
                    project_name=project.name,
                )
            )
            preset_repository.save(
                Preset(
                    name="storage-preset",
                    settings_snapshot={"browser": {"slow_mode": False}},
                )
            )

            reloaded_paths = WorkspaceStorage(temp_dir).initialize()
            self.assertEqual(
                ProjectRepository(reloaded_paths).load(project.project_id).name,
                "Storage",
            )
            self.assertEqual(
                RunRepository(reloaded_paths).load(run.run_id).status,
                RunStatus.RUNNING,
            )
            self.assertEqual(
                ManifestRepository(reloaded_paths).load(manifest.run_id).project_name,
                "Storage",
            )
            self.assertEqual(
                PresetRepository(reloaded_paths).list_names(), ["storage-preset"]
            )

    def test_saved_storyblocks_fixtures_cover_probe_contract_and_adapter_regressions(
        self,
    ) -> None:
        probe = StoryblocksSessionProbe()
        checker = StoryblocksDomContractChecker()
        adapter = StoryblocksVideoSearchAdapter()

        self.assertEqual(
            probe.inspect_document(_fixture("ready.html")).health, SessionHealth.READY
        )
        self.assertEqual(
            probe.inspect_document(_fixture("login_required.html")).health,
            SessionHealth.LOGIN_REQUIRED,
        )
        self.assertEqual(
            probe.inspect_document(_fixture("challenge.html")).health,
            SessionHealth.CHALLENGE,
        )
        self.assertEqual(
            probe.inspect_document(_fixture("expired.html")).health,
            SessionHealth.EXPIRED,
        )
        self.assertEqual(
            probe.inspect_document(_fixture("blocked.html")).health,
            SessionHealth.BLOCKED,
        )
        self.assertTrue(
            checker.validate_markup(_fixture("valid_search_results.html")).valid
        )
        self.assertFalse(
            checker.validate_markup(_fixture("broken_contract.html")).valid
        )

        candidates = adapter.parse_result_cards(
            _fixture("valid_search_results.html"), "river boat"
        )
        self.assertEqual(
            [candidate.asset_id for candidate in candidates], ["asset-123"]
        )
        self.assertEqual(candidates[0].metadata["search_query"], "river boat")

    def test_session_manager_rejects_missing_browser_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            container.profile_registry.get_or_create_singleton()
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [], "msedge": []},
                    which_func=lambda _: None,
                ),
            )

            with self.assertRaises(ConfigError) as ctx:
                manager.check_browser_channel()

            self.assertEqual(ctx.exception.code, "browser_channel_unavailable")

    def test_empty_search_results_produce_no_match_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("storyblocks_video")
            assert descriptor is not None
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: [],
                )
            )

            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=True,
                    storyblocks_images_enabled=False,
                    free_images_enabled=False,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(manifest.paragraph_entries[0].selection.status, "no_match")

    def test_timeout_error_is_recorded_as_run_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=1)
            descriptor = container.provider_registry.get("storyblocks_video")
            assert descriptor is not None
            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: (_ for _ in ()).throw(
                        TimeoutError("network timeout")
                    ),
                )
            )

            run, _manifest = container.media_run_service.create_and_execute(
                project.project_id
            )

            self.assertEqual(run.status, RunStatus.FAILED)
            self.assertEqual(run.failed_paragraphs, [1])
            self.assertIn("network timeout", run.last_error or "")

    def test_download_manager_flags_incomplete_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = StoryblocksDownloadManager(
                _ZeroByteDownloadDriver(), max_retries=1
            )
            record = manager.download_one(
                StoryblocksDownloadRequest(
                    asset_id="asset-9",
                    detail_url="https://www.storyblocks.com/video/stock/asset-9",
                    destination_dir=Path(temp_dir),
                    filename="asset-9.mp4",
                )
            )

            self.assertEqual(record.status, "failed")
            self.assertEqual(record.attempts, 2)
            self.assertIn("did not complete cleanly", record.error or "")

    def test_run_repository_normalizes_legacy_paused_payload_with_checkpoint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = WorkspaceStorage(temp_dir).initialize()
            repository = RunRepository(paths)
            legacy_run_id = "legacy-paused-run"
            write_json(
                repository.path_for(legacy_run_id),
                {
                    "run_id": legacy_run_id,
                    "project_id": "legacy-project",
                    "status": "paused",
                    "stage": "persist",
                    "selected_paragraphs": [2],
                    "completed_paragraphs": [1],
                    "failed_paragraphs": [3],
                    "checkpoint": {
                        "run_id": legacy_run_id,
                        "stage": "persist",
                        "current_paragraph_no": 1,
                        "selected_paragraphs": [2],
                        "completed_paragraphs": [1],
                        "failed_paragraphs": [3],
                    },
                },
            )

            run = repository.load(legacy_run_id)

            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run.status, RunStatus.CANCELLED)
            self.assertEqual(run.stage, RunStage.PERSIST)
            self.assertEqual(run.selected_paragraphs, [2])
            self.assertEqual(run.completed_paragraphs, [1])
            self.assertEqual(run.failed_paragraphs, [3])
            self.assertEqual(run.metadata.get("legacy_status"), "paused")
            self.assertFalse(hasattr(run, "checkpoint"))

    def test_failing_event_listener_does_not_break_run_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=1)
            self._register_video_backend(container)

            def failing_listener(_event) -> None:
                raise RuntimeError("listener boom")

            container.event_bus.subscribe(failing_listener)
            run, manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    storyblocks_images_enabled=False,
                    free_images_enabled=False,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(manifest.run_id, run.run_id)

    def test_missing_manifest_is_recreated_for_same_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=1)
            self._register_video_backend(container)

            run, _manifest = container.media_run_service.create_run(project.project_id)
            manifest_path = container.manifest_repository.path_for(run.run_id)
            manifest_path.unlink()

            executed_run, manifest = container.media_run_service.execute(run.run_id)

            self.assertEqual(executed_run.run_id, run.run_id)
            self.assertTrue(manifest_path.exists())
            self.assertEqual(manifest.run_id, run.run_id)
            self.assertEqual(len(container.run_repository.list_all()), 1)
            container.close()

    def test_legacy_manifest_manual_fields_normalize_for_new_ui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = DesktopGuiController.create(temp_dir)
            container = controller.application.container
            project = self._create_project(container, temp_dir, paragraph_count=2)
            assert project.script_document is not None
            run = container.run_repository.save(
                Run(
                    run_id="legacy-run",
                    project_id=project.project_id,
                    status=RunStatus.COMPLETED,
                    stage=RunStage.COMPLETE,
                )
            )
            saved_file = Path(temp_dir) / "downloads" / "legacy-video.mp4"
            saved_file.parent.mkdir(parents=True, exist_ok=True)
            saved_file.write_bytes(b"legacy-video")
            first = project.script_document.paragraphs[0]
            second = project.script_document.paragraphs[1]
            legacy_decision_key = "user_decision" + "_status"
            legacy_fallback_key = "fallback" + "_options"
            write_json(
                container.manifest_repository.path_for(run.run_id),
                {
                    "run_id": run.run_id,
                    "project_id": project.project_id,
                    "project_name": project.name,
                    "paragraph_entries": [
                        {
                            "paragraph_no": first.paragraph_no,
                            "original_index": first.original_index,
                            "text": first.text,
                            "status": "locked",
                            legacy_decision_key: "locked",
                            legacy_fallback_key: [],
                            "rejection_reasons": [],
                            "selection": {
                                "paragraph_no": first.paragraph_no,
                                "primary_asset": _candidate(
                                    "legacy-video",
                                    "storyblocks_video",
                                    AssetKind.VIDEO,
                                    100.0,
                                ).to_dict()
                                | {"local_path": str(saved_file)},
                                "supporting_assets": [],
                                "fallback_assets": [],
                                "media_slots": [],
                                "provider_results": [],
                                "rejection_reasons": [],
                                "diagnostics": {},
                                "reason": "Legacy locked selection",
                                "status": "selected",
                                "user_locked": True,
                                legacy_decision_key: "locked",
                            },
                        },
                        {
                            "paragraph_no": second.paragraph_no,
                            "original_index": second.original_index,
                            "text": second.text,
                            "status": "needs_review",
                            legacy_decision_key: "needs_review",
                            legacy_fallback_key: [],
                            "rejection_reasons": ["network timeout"],
                            "selection": None,
                        },
                    ],
                    "summary": {
                        "paragraphs_total": 2,
                        "paragraphs_processed": 2,
                    },
                },
            )

            items = controller.build_paragraph_workbench(project.project_id, run.run_id)
            first_item = next(
                item for item in items if item.paragraph_no == first.paragraph_no
            )
            second_item = next(
                item for item in items if item.paragraph_no == second.paragraph_no
            )

            self.assertEqual(first_item.status, "completed")
            self.assertEqual(first_item.result_note, "Legacy locked selection")
            self.assertEqual(len(first_item.downloaded_files), 1)
            self.assertEqual(first_item.downloaded_files[0].local_path, str(saved_file))
            self.assertEqual(second_item.status, "failed")
            self.assertIn("network timeout", second_item.result_note)

    def test_free_image_only_and_mixed_mode_work_without_legacy_free_video(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=1)
            self._register_image_backends(container)

            free_only_run, free_only_manifest = (
                container.media_run_service.create_and_execute(
                    project.project_id,
                    config=MediaSelectionConfig(
                        video_enabled=False,
                        storyblocks_images_enabled=False,
                        free_images_enabled=True,
                        supporting_image_limit=1,
                        fallback_image_limit=1,
                    ),
                )
            )
            mixed_run, mixed_manifest = container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    video_enabled=False,
                    storyblocks_images_enabled=True,
                    free_images_enabled=True,
                    supporting_image_limit=1,
                    fallback_image_limit=1,
                ),
            )

            self.assertEqual(free_only_run.status, RunStatus.COMPLETED)
            self.assertEqual(mixed_run.status, RunStatus.COMPLETED)
            free_only_selection = free_only_manifest.paragraph_entries[0].selection
            mixed_selection = mixed_manifest.paragraph_entries[0].selection
            self.assertIsNotNone(free_only_selection)
            self.assertIsNotNone(mixed_selection)
            assert free_only_selection is not None
            assert mixed_selection is not None
            self.assertEqual(
                free_only_selection.fallback_assets[0].provider_name, "openverse"
            )
            self.assertEqual(
                mixed_selection.supporting_assets[0].provider_name, "storyblocks_image"
            )
            self.assertEqual(
                mixed_selection.fallback_assets[0].provider_name, "openverse"
            )
            container.close()

    def test_high_load_free_image_mode_stays_stable_with_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=12)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]
            container.orchestrator.configure(max_workers=1, queue_size=4)
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
                            7.0,
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
                    provider_workers=4,
                    provider_queue_size=8,
                    download_workers=4,
                    bounded_downloads=8,
                    early_stop_when_satisfied=False,
                ),
            )
            elapsed = time.perf_counter() - started_at

            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(manifest.summary["paragraphs_completed"], 12)
            self.assertLess(elapsed, 6.0)

    def test_cancel_under_free_image_load_remains_responsive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            project = self._create_project(container, temp_dir, paragraph_count=8)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]
            container.orchestrator.configure(max_workers=1, queue_size=3)

            def slow_search(paragraph: ParagraphUnit, query: str, limit: int):
                time.sleep(0.05)
                return [
                    _candidate(
                        f"openverse-{paragraph.paragraph_no}",
                        "openverse",
                        AssetKind.IMAGE,
                        7.0,
                    )
                ]

            container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    search_fn=slow_search,
                )
            )

            config = MediaSelectionConfig(
                video_enabled=False,
                storyblocks_images_enabled=False,
                free_images_enabled=True,
                supporting_image_limit=0,
                fallback_image_limit=1,
                provider_workers=3,
                provider_queue_size=6,
                early_stop_when_satisfied=False,
            )

            run, _ = container.media_run_service.create_run(
                project.project_id,
                config=config,
            )
            container.media_run_service.cancel(run.run_id)
            cancelled_run, _ = container.media_run_service.execute(
                run.run_id,
                config=config,
            )
            self.assertEqual(cancelled_run.status, RunStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
