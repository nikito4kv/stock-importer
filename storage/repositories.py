from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from config.settings import (
    SUPPORTED_DESKTOP_STACK,
    AiSettings,
    ApplicationSettings,
    BrowserSettings,
    ConcurrencySettings,
    ProviderSettings,
    SecuritySettings,
    StorageSettings,
    default_settings,
)
from domain.models import BrowserProfile, Preset, Project, Run, RunManifest
from domain.project_modes import (
    DEFAULT_FREE_IMAGE_PROVIDER_IDS,
    infer_project_mode,
    normalize_free_image_provider_ids,
    normalize_project_mode,
    provider_ids_for_mode,
)

from .serialization import read_json, write_json
from .workspace import WorkspacePaths

T = TypeVar("T")
SUPPORTED_PROVIDER_IDS = frozenset(
    {
        "storyblocks_video",
        "storyblocks_image",
        "pexels",
        "pixabay",
        "openverse",
    }
)
SUPPORTED_IMAGE_PROVIDER_IDS = frozenset(
    {
        "storyblocks_image",
        "pexels",
        "pixabay",
        "openverse",
    }
)
SUPPORTED_VIDEO_PROVIDER_IDS = frozenset({"storyblocks_video"})


def _normalize_provider_ids(
    raw_provider_ids: object,
    *,
    allowed_provider_ids: frozenset[str],
) -> list[str]:
    if not isinstance(raw_provider_ids, (list, tuple)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_provider_ids:
        provider_id = str(item).strip().casefold()
        if (
            not provider_id
            or provider_id not in allowed_provider_ids
            or provider_id in seen
        ):
            continue
        seen.add(provider_id)
        normalized.append(provider_id)
    return normalized


class JsonModelRepository(Generic[T]):
    def __init__(self, model_cls: type[T]):
        self._model_cls = model_cls

    def load(self, path: Path) -> T | None:
        data = read_json(path)
        if not data:
            return None
        return self._model_cls.from_dict(data)

    def save(self, path: Path, model: T) -> T:
        write_json(path, model.to_dict())
        return model


@dataclass
class SettingsRepository:
    paths: WorkspacePaths

    def _path(self) -> Path:
        return self.paths.config_dir / "settings.json"

    def load(self) -> ApplicationSettings:
        data = read_json(self._path())
        if not data:
            return default_settings()
        storage = data.get("storage", {})
        browser = data.get("browser", {})
        providers = data.get("providers", {})
        normalized_enabled_providers = _normalize_provider_ids(
            providers.get("enabled_providers"),
            allowed_provider_ids=SUPPORTED_PROVIDER_IDS,
        )
        normalized_default_video_providers = _normalize_provider_ids(
            providers.get("default_video_providers"),
            allowed_provider_ids=SUPPORTED_VIDEO_PROVIDER_IDS,
        )
        normalized_default_image_providers = _normalize_provider_ids(
            providers.get("default_image_providers"),
            allowed_provider_ids=SUPPORTED_IMAGE_PROVIDER_IDS,
        )
        normalized_free_providers = normalize_free_image_provider_ids(
            [
                provider_id
                for provider_id in normalized_enabled_providers
                if provider_id in DEFAULT_FREE_IMAGE_PROVIDER_IDS
            ]
        )
        if not normalized_free_providers:
            normalized_free_providers = normalize_free_image_provider_ids(
                [
                    provider_id
                    for provider_id in normalized_default_image_providers
                    if provider_id in DEFAULT_FREE_IMAGE_PROVIDER_IDS
                ]
            )
        project_mode = providers.get("project_mode")
        if not project_mode:
            legacy_mixed_image_fallback = bool(
                providers.get("mixed_image_fallback", True)
            )
            storyblocks_images_enabled = (
                "storyblocks_image" in normalized_enabled_providers
                and not bool(providers.get("free_images_only", False))
            )
            free_images_enabled = bool(normalized_free_providers)
            video_enabled = bool(
                normalized_default_video_providers
                or ("storyblocks_video" in normalized_enabled_providers)
            )
            if video_enabled and storyblocks_images_enabled and free_images_enabled:
                project_mode = (
                    "sb_video_plus_free_images"
                    if legacy_mixed_image_fallback
                    else "sb_video_plus_sb_images"
                )
            else:
                project_mode = infer_project_mode(
                    video_enabled=video_enabled,
                    storyblocks_images_enabled=storyblocks_images_enabled,
                    free_images_enabled=free_images_enabled,
                )
        resolved_project_mode = normalize_project_mode(project_mode)
        resolved_enabled_providers = provider_ids_for_mode(
            resolved_project_mode,
            free_image_provider_ids=normalized_free_providers,
        )
        resolved_default_video_providers = [
            provider_id
            for provider_id in resolved_enabled_providers
            if provider_id == "storyblocks_video"
        ]
        resolved_default_image_providers = [
            provider_id
            for provider_id in resolved_enabled_providers
            if provider_id != "storyblocks_video"
        ]
        concurrency = data.get("concurrency", {})
        ai = data.get("ai", {})
        security = data.get("security", {})
        return ApplicationSettings(
            desktop_stack=SUPPORTED_DESKTOP_STACK,
            ui_theme=str(data.get("ui_theme", "dark")),
            workspace_name=str(data.get("workspace_name", "vid-img-downloader")),
            storage=StorageSettings(
                workspace_root=Path(storage.get("workspace_root", "workspace")),
                cache_root=Path(storage.get("cache_root", "workspace/cache")),
                logs_root=Path(storage.get("logs_root", "workspace/logs")),
                secrets_root=Path(storage.get("secrets_root", "workspace/secrets")),
            ),
            browser=BrowserSettings(
                automation_stack=str(browser.get("automation_stack", "playwright")),
                profile_root=Path(
                    browser.get("profile_root", "workspace/browser_profiles")
                ),
                preferred_channels=list(
                    browser.get("preferred_channels", ["chrome", "msedge"])
                ),
                slow_mode=bool(browser.get("slow_mode", True)),
                action_delay_ms=int(browser.get("action_delay_ms", 900)),
                launch_timeout_ms=int(browser.get("launch_timeout_ms", 45000)),
                navigation_timeout_ms=int(browser.get("navigation_timeout_ms", 30000)),
                downloads_timeout_seconds=float(
                    browser.get("downloads_timeout_seconds", 120.0)
                ),
                storyblocks_base_url=str(
                    browser.get("storyblocks_base_url", "https://www.storyblocks.com")
                ),
            ),
            providers=ProviderSettings(
                project_mode=resolved_project_mode,
                default_video_providers=resolved_default_video_providers,
                default_image_providers=resolved_default_image_providers,
                enabled_providers=resolved_enabled_providers,
                commercial_only_images=bool(
                    providers.get("commercial_only_images", True)
                ),
                allow_attribution_licenses=bool(
                    providers.get("allow_attribution_licenses", False)
                ),
                free_images_only=resolved_project_mode == "free_images_only",
                supporting_image_limit=max(
                    0, int(providers.get("supporting_image_limit", 1))
                ),
                fallback_image_limit=max(
                    0, int(providers.get("fallback_image_limit", 1))
                ),
                no_match_budget_seconds=max(
                    0.0, float(providers.get("no_match_budget_seconds", 20.0))
                ),
            ),
            ai=AiSettings(
                full_script_context_enabled=bool(
                    ai.get("full_script_context_enabled", True)
                ),
                full_script_context_char_budget=max(
                    1000, int(ai.get("full_script_context_char_budget", 12000))
                ),
            ),
            concurrency=ConcurrencySettings(
                paragraph_workers=int(concurrency.get("paragraph_workers", 1)),
                provider_workers=int(concurrency.get("provider_workers", 4)),
                provider_queue_size=int(concurrency.get("provider_queue_size", 8)),
                download_workers=int(concurrency.get("download_workers", 4)),
                download_queue_size=int(concurrency.get("download_queue_size", 8)),
                search_timeout_seconds=max(
                    0.0, float(concurrency.get("search_timeout_seconds", 20.0))
                ),
                download_timeout_seconds=max(
                    1.0, float(concurrency.get("download_timeout_seconds", 120.0))
                ),
                retry_budget=max(0, int(concurrency.get("retry_budget", 2))),
                fail_fast_storyblocks_errors=bool(
                    concurrency.get("fail_fast_storyblocks_errors", True)
                ),
                queue_size=int(concurrency.get("queue_size", 1)),
            ),
            security=SecuritySettings(
                secret_backend=str(security.get("secret_backend", "dpapi")),
                storyblocks_session_secret_name=str(
                    security.get(
                        "storyblocks_session_secret_name", "storyblocks_session"
                    )
                ),
                gemini_api_key_secret_name=str(
                    security.get("gemini_api_key_secret_name", "gemini_api_key")
                ),
                pexels_api_key_secret_name=str(
                    security.get("pexels_api_key_secret_name", "pexels_api_key")
                ),
                pixabay_api_key_secret_name=str(
                    security.get("pixabay_api_key_secret_name", "pixabay_api_key")
                ),
            ),
        )

    def save(self, settings: ApplicationSettings) -> ApplicationSettings:
        default_video_providers = _normalize_provider_ids(
            settings.providers.default_video_providers,
            allowed_provider_ids=SUPPORTED_VIDEO_PROVIDER_IDS,
        )
        default_image_providers = _normalize_provider_ids(
            settings.providers.default_image_providers,
            allowed_provider_ids=SUPPORTED_IMAGE_PROVIDER_IDS,
        )
        enabled_providers = _normalize_provider_ids(
            settings.providers.enabled_providers,
            allowed_provider_ids=SUPPORTED_PROVIDER_IDS,
        )
        payload = {
            "desktop_stack": SUPPORTED_DESKTOP_STACK,
            "ui_theme": settings.ui_theme,
            "workspace_name": settings.workspace_name,
            "storage": {
                "workspace_root": str(settings.storage.workspace_root),
                "cache_root": str(settings.storage.cache_root),
                "logs_root": str(settings.storage.logs_root),
                "secrets_root": str(settings.storage.secrets_root),
            },
            "browser": {
                "automation_stack": settings.browser.automation_stack,
                "profile_root": str(settings.browser.profile_root),
                "preferred_channels": list(settings.browser.preferred_channels),
                "slow_mode": settings.browser.slow_mode,
                "action_delay_ms": settings.browser.action_delay_ms,
                "launch_timeout_ms": settings.browser.launch_timeout_ms,
                "navigation_timeout_ms": settings.browser.navigation_timeout_ms,
                "downloads_timeout_seconds": settings.browser.downloads_timeout_seconds,
                "storyblocks_base_url": settings.browser.storyblocks_base_url,
            },
            "providers": {
                "project_mode": settings.providers.project_mode,
                "default_video_providers": default_video_providers,
                "default_image_providers": default_image_providers,
                "enabled_providers": enabled_providers,
                "commercial_only_images": settings.providers.commercial_only_images,
                "allow_attribution_licenses": settings.providers.allow_attribution_licenses,
                "free_images_only": settings.providers.free_images_only,
                "supporting_image_limit": settings.providers.supporting_image_limit,
                "fallback_image_limit": settings.providers.fallback_image_limit,
                "no_match_budget_seconds": settings.providers.no_match_budget_seconds,
            },
            "ai": {
                "full_script_context_enabled": settings.ai.full_script_context_enabled,
                "full_script_context_char_budget": settings.ai.full_script_context_char_budget,
            },
            "concurrency": {
                "paragraph_workers": settings.concurrency.paragraph_workers,
                "provider_workers": settings.concurrency.provider_workers,
                "provider_queue_size": settings.concurrency.provider_queue_size,
                "download_workers": settings.concurrency.download_workers,
                "download_queue_size": settings.concurrency.download_queue_size,
                "search_timeout_seconds": settings.concurrency.search_timeout_seconds,
                "download_timeout_seconds": settings.concurrency.download_timeout_seconds,
                "retry_budget": settings.concurrency.retry_budget,
                "fail_fast_storyblocks_errors": settings.concurrency.fail_fast_storyblocks_errors,
                "queue_size": settings.concurrency.queue_size,
            },
            "security": {
                "secret_backend": settings.security.secret_backend,
                "storyblocks_session_secret_name": settings.security.storyblocks_session_secret_name,
                "gemini_api_key_secret_name": settings.security.gemini_api_key_secret_name,
                "pexels_api_key_secret_name": settings.security.pexels_api_key_secret_name,
                "pixabay_api_key_secret_name": settings.security.pixabay_api_key_secret_name,
            },
        }
        write_json(self._path(), payload)
        return settings


@dataclass
class ProjectRepository:
    paths: WorkspacePaths

    def __post_init__(self) -> None:
        self._repository = JsonModelRepository(Project)

    def path_for(self, project_id: str) -> Path:
        return self.paths.projects_dir / project_id / "project.json"

    def load(self, project_id: str) -> Project | None:
        return self._repository.load(self.path_for(project_id))

    def save(self, project: Project) -> Project:
        return self._repository.save(self.path_for(project.project_id), project)

    def list_all(self) -> list[Project]:
        projects: list[Project] = []
        for path in sorted(self.paths.projects_dir.glob("*/project.json")):
            project = self._repository.load(path)
            if project is not None:
                projects.append(project)
        return projects


@dataclass
class RunRepository:
    paths: WorkspacePaths

    def __post_init__(self) -> None:
        self._repository = JsonModelRepository(Run)

    def path_for(self, run_id: str) -> Path:
        return self.paths.runs_dir / run_id / "run.json"

    def load(self, run_id: str) -> Run | None:
        return self._repository.load(self.path_for(run_id))

    def save(self, run: Run) -> Run:
        return self._repository.save(self.path_for(run.run_id), run)

    def list_all(self) -> list[Run]:
        runs: list[Run] = []
        for path in sorted(self.paths.runs_dir.glob("*/run.json")):
            run = self._repository.load(path)
            if run is not None:
                runs.append(run)
        return runs


@dataclass
class ManifestRepository:
    paths: WorkspacePaths

    def __post_init__(self) -> None:
        self._repository = JsonModelRepository(RunManifest)

    def path_for(self, run_id: str) -> Path:
        return self.paths.runs_dir / run_id / "manifest.json"

    def load(self, run_id: str) -> RunManifest | None:
        return self._repository.load(self.path_for(run_id))

    def save(self, manifest: RunManifest) -> RunManifest:
        return self._repository.save(self.path_for(manifest.run_id), manifest)

    def list_all(self) -> list[RunManifest]:
        manifests: list[RunManifest] = []
        for path in sorted(self.paths.runs_dir.glob("*/manifest.json")):
            manifest = self._repository.load(path)
            if manifest is not None:
                manifests.append(manifest)
        return manifests


@dataclass
class PresetRepository:
    paths: WorkspacePaths

    def __post_init__(self) -> None:
        self._repository = JsonModelRepository(Preset)

    def path_for(self, preset_name: str) -> Path:
        return self.paths.presets_dir / f"{preset_name}.json"

    def load(self, preset_name: str) -> Preset | None:
        return self._repository.load(self.path_for(preset_name))

    def save(self, preset: Preset) -> Preset:
        return self._repository.save(self.path_for(preset.name), preset)

    def list_names(self) -> list[str]:
        return sorted(path.stem for path in self.paths.presets_dir.glob("*.json"))

    def list_all(self) -> list[Preset]:
        presets: list[Preset] = []
        for path in sorted(self.paths.presets_dir.glob("*.json")):
            preset = self._repository.load(path)
            if preset is not None:
                presets.append(preset)
        return presets


@dataclass
class BrowserProfileRepository:
    paths: WorkspacePaths

    def __post_init__(self) -> None:
        self._repository = JsonModelRepository(BrowserProfile)

    def path_for(self, profile_id: str) -> Path:
        return self.paths.browser_profiles_dir / f"{profile_id}.json"

    def load(self, profile_id: str) -> BrowserProfile | None:
        return self._repository.load(self.path_for(profile_id))

    def save(self, profile: BrowserProfile) -> BrowserProfile:
        return self._repository.save(self.path_for(profile.profile_id), profile)

    def list_all(self) -> list[BrowserProfile]:
        profiles: list[BrowserProfile] = []
        for path in sorted(self.paths.browser_profiles_dir.glob("*.json")):
            profile = self._repository.load(path)
            if profile is not None:
                profiles.append(profile)
        return profiles
