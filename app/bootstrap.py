from __future__ import annotations

import atexit
from dataclasses import dataclass
from pathlib import Path

from browser import (
    BrowserProfileRegistry,
    BrowserSessionManager,
    ChromiumProfileImportService,
    StoryblocksCandidateSearchBackend,
    StoryblocksDomContractChecker,
    StoryblocksImageSearchAdapter,
    StoryblocksOperationPolicy,
    StoryblocksSessionProbe,
    StoryblocksVideoSearchAdapter,
)
from config import ApplicationSettings, default_settings
from pipeline import (
    ParagraphIntentService,
    ParagraphMediaPipeline,
    ParagraphMediaRunService,
    RunOrchestrator,
    ScriptIngestionService,
)
from providers import (
    ImageProviderSearchService,
    ProviderRegistry,
    build_default_provider_registry,
)
from services import EventBus, EventRecorder, SecretStore, SettingsManager
from services.logging import JsonLineEventLogger, JsonLinePerfLogger
from storage import (
    BrowserProfileRepository,
    ManifestRepository,
    PresetRepository,
    ProjectRepository,
    RunRepository,
    SettingsRepository,
    WorkspaceStorage,
)


@dataclass(slots=True)
class ApplicationContainer:
    settings: ApplicationSettings
    workspace: WorkspaceStorage
    settings_manager: SettingsManager
    project_repository: ProjectRepository
    run_repository: RunRepository
    manifest_repository: ManifestRepository
    provider_registry: ProviderRegistry
    image_provider_search_service: ImageProviderSearchService
    profile_registry: BrowserProfileRegistry
    profile_import_service: ChromiumProfileImportService
    session_manager: BrowserSessionManager
    storyblocks_dom_checker: StoryblocksDomContractChecker
    storyblocks_video_adapter: StoryblocksVideoSearchAdapter
    storyblocks_image_adapter: StoryblocksImageSearchAdapter
    ingestion_service: ScriptIngestionService
    intent_service: ParagraphIntentService
    orchestrator: RunOrchestrator
    media_pipeline: ParagraphMediaPipeline
    media_run_service: ParagraphMediaRunService
    event_bus: EventBus
    event_recorder: EventRecorder

    def close(self) -> None:
        self.media_pipeline.close()
        self.image_provider_search_service.close()
        self.session_manager.close_browsers_owned_by_current_thread()


def bootstrap_application(
    workspace_root: str | Path | None = None,
) -> ApplicationContainer:
    settings = default_settings()
    root = (
        Path(workspace_root)
        if workspace_root is not None
        else settings.storage.workspace_root
    )
    workspace = WorkspaceStorage(root)
    paths = workspace.initialize()

    settings_repository = SettingsRepository(paths)
    secret_store = SecretStore(paths.secrets_dir)
    preset_repository = PresetRepository(paths)
    settings_manager = SettingsManager(
        settings_repository, preset_repository, secret_store
    )
    settings = settings_manager.save(settings_manager.load())

    event_bus = EventBus()
    event_recorder = EventRecorder()
    event_logger = JsonLineEventLogger(paths.logs_dir / "app.log")
    perf_logger = JsonLinePerfLogger(paths.logs_dir / "perf.jsonl")
    event_bus.subscribe(event_recorder)
    event_bus.subscribe(event_logger.write)
    event_bus.subscribe(perf_logger.write)
    atexit.register(event_logger.flush)
    atexit.register(perf_logger.flush)

    project_repository = ProjectRepository(paths)
    run_repository = RunRepository(paths)
    manifest_repository = ManifestRepository(paths)
    profile_repository = BrowserProfileRepository(paths)
    provider_registry = build_default_provider_registry()
    image_provider_search_service = ImageProviderSearchService(
        provider_registry, paths.cache_dir
    )
    profile_registry = BrowserProfileRegistry(profile_repository)
    profile_import_service = ChromiumProfileImportService(profile_registry)
    storyblocks_probe = StoryblocksSessionProbe()
    storyblocks_dom_checker = StoryblocksDomContractChecker()
    storyblocks_video_adapter = StoryblocksVideoSearchAdapter(
        settings.browser.storyblocks_base_url
    )
    storyblocks_image_adapter = StoryblocksImageSearchAdapter(
        settings.browser.storyblocks_base_url
    )
    session_manager = BrowserSessionManager(
        profile_registry,
        settings.browser,
        session_probe=storyblocks_probe,
    )
    media_pipeline = ParagraphMediaPipeline(
        provider_registry,
        manifest_repository,
        provider_settings=settings.providers,
        event_bus=event_bus,
    )
    storyblocks_video_descriptor = provider_registry.get("storyblocks_video")
    storyblocks_image_descriptor = provider_registry.get("storyblocks_image")
    if storyblocks_video_descriptor is not None:
        media_pipeline.register_backend(
            StoryblocksCandidateSearchBackend(
                provider_id="storyblocks_video",
                capability=storyblocks_video_descriptor.capability,
                descriptor=storyblocks_video_descriptor,
                session_manager=session_manager,
                search_adapter=storyblocks_video_adapter,
                dom_checker=storyblocks_dom_checker,
                operation_policy=StoryblocksOperationPolicy(
                    search_timeout_seconds=max(
                        0.0, float(settings.concurrency.search_timeout_seconds)
                    ),
                    download_retries=max(0, int(settings.concurrency.retry_budget)),
                    download_timeout_seconds=settings.browser.downloads_timeout_seconds,
                ),
            )
        )
    if storyblocks_image_descriptor is not None:
        media_pipeline.register_backend(
            StoryblocksCandidateSearchBackend(
                provider_id="storyblocks_image",
                capability=storyblocks_image_descriptor.capability,
                descriptor=storyblocks_image_descriptor,
                session_manager=session_manager,
                search_adapter=storyblocks_image_adapter,
                dom_checker=storyblocks_dom_checker,
                operation_policy=StoryblocksOperationPolicy(
                    search_timeout_seconds=max(
                        0.0, float(settings.concurrency.search_timeout_seconds)
                    ),
                    download_retries=max(0, int(settings.concurrency.retry_budget)),
                    download_timeout_seconds=settings.browser.downloads_timeout_seconds,
                ),
            )
        )
    media_pipeline.build_default_free_image_backends(
        image_provider_search_service,
        pexels_api_key=settings_manager.get_secret(
            settings.security.pexels_api_key_secret_name
        ),
        pixabay_api_key=settings_manager.get_secret(
            settings.security.pixabay_api_key_secret_name
        ),
    )
    orchestrator = RunOrchestrator(
        run_repository,
        event_bus,
        max_workers=settings.concurrency.paragraph_workers,
        queue_size=settings.concurrency.queue_size,
    )
    media_run_service = ParagraphMediaRunService(
        project_repository,
        run_repository,
        manifest_repository,
        media_pipeline,
        orchestrator,
        session_manager=session_manager,
        concurrency_settings=settings.concurrency,
    )

    container = ApplicationContainer(
        settings=settings,
        workspace=workspace,
        settings_manager=settings_manager,
        project_repository=project_repository,
        run_repository=run_repository,
        manifest_repository=manifest_repository,
        provider_registry=provider_registry,
        image_provider_search_service=image_provider_search_service,
        profile_registry=profile_registry,
        profile_import_service=profile_import_service,
        session_manager=session_manager,
        storyblocks_dom_checker=storyblocks_dom_checker,
        storyblocks_video_adapter=storyblocks_video_adapter,
        storyblocks_image_adapter=storyblocks_image_adapter,
        ingestion_service=ScriptIngestionService(),
        intent_service=ParagraphIntentService(),
        orchestrator=orchestrator,
        media_pipeline=media_pipeline,
        media_run_service=media_run_service,
        event_bus=event_bus,
        event_recorder=event_recorder,
    )
    atexit.register(container.close)
    return container
