from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ConcurrencySettings:
    paragraph_workers: int = 1
    provider_workers: int = 4
    provider_queue_size: int = 8
    download_workers: int = 4
    download_queue_size: int = 8
    relevance_workers: int = 2
    relevance_queue_size: int = 8
    search_timeout_seconds: float = 20.0
    download_timeout_seconds: float = 120.0
    relevance_timeout_seconds: float = 10.0
    retry_budget: int = 2
    early_stop_quality_threshold: float = 8.0
    fail_fast_storyblocks_errors: bool = True
    queue_size: int = 8


@dataclass(slots=True)
class BrowserSettings:
    automation_stack: str = "playwright"
    profile_root: Path = Path("workspace/browser_profiles")
    preferred_channels: list[str] = field(default_factory=lambda: ["chrome", "msedge"])
    slow_mode: bool = True
    action_delay_ms: int = 900
    launch_timeout_ms: int = 45000
    navigation_timeout_ms: int = 30000
    downloads_timeout_seconds: float = 120.0
    storyblocks_base_url: str = "https://www.storyblocks.com"


@dataclass(slots=True)
class StorageSettings:
    workspace_root: Path = Path("workspace")
    cache_root: Path = Path("workspace/cache")
    logs_root: Path = Path("workspace/logs")
    secrets_root: Path = Path("workspace/secrets")


@dataclass(slots=True)
class ProviderSettings:
    project_mode: str = "sb_video_only"
    default_video_providers: list[str] = field(
        default_factory=lambda: ["storyblocks_video"]
    )
    default_image_providers: list[str] = field(
        default_factory=lambda: [
            "storyblocks_image",
            "pexels",
            "pixabay",
            "openverse",
            "wikimedia",
        ]
    )
    enabled_providers: list[str] = field(
        default_factory=lambda: [
            "storyblocks_video",
            "storyblocks_image",
            "pexels",
            "pixabay",
            "openverse",
            "wikimedia",
            "bing",
        ]
    )
    image_provider_priority: list[str] = field(
        default_factory=lambda: [
            "storyblocks_image",
            "pexels",
            "pixabay",
            "openverse",
            "wikimedia",
            "bing",
        ]
    )
    allow_generic_web_image: bool = False
    commercial_only_images: bool = True
    allow_attribution_licenses: bool = False
    free_images_only: bool = False
    mixed_image_fallback: bool = True
    supporting_image_limit: int = 1
    fallback_image_limit: int = 1
    no_match_budget_seconds: float = 20.0


@dataclass(slots=True)
class AiSettings:
    full_script_context_enabled: bool = True
    full_script_context_char_budget: int = 12000


@dataclass(slots=True)
class SecuritySettings:
    secret_backend: str = "dpapi"
    storyblocks_session_secret_name: str = "storyblocks_session"
    gemini_api_key_secret_name: str = "gemini_api_key"
    pexels_api_key_secret_name: str = "pexels_api_key"
    pixabay_api_key_secret_name: str = "pixabay_api_key"


@dataclass(slots=True)
class ApplicationSettings:
    desktop_stack: str = "pyside6"
    ui_theme: str = "dark"
    workspace_name: str = "vid-img-downloader"
    storage: StorageSettings = field(default_factory=StorageSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    providers: ProviderSettings = field(default_factory=ProviderSettings)
    ai: AiSettings = field(default_factory=AiSettings)
    concurrency: ConcurrencySettings = field(default_factory=ConcurrencySettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)


def default_settings() -> ApplicationSettings:
    return ApplicationSettings()
