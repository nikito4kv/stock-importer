from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from config.settings import ProviderSettings
from domain.enums import ProviderCapability
from domain.project_modes import DEFAULT_FREE_IMAGE_PROVIDER_IDS

from .base import ProviderDescriptor

STORYBLOCKS_PROVIDER_IDS = frozenset({"storyblocks_video", "storyblocks_image"})
STORYBLOCKS_IMAGE_PROVIDER_ID = "storyblocks_image"
FREE_IMAGE_PROVIDER_IDS = frozenset(DEFAULT_FREE_IMAGE_PROVIDER_IDS)


class ExecutionConcurrencyMode(str, Enum):
    STORYBLOCKS_SAFE = "storyblocks_safe"
    FREE_IMAGES_PARALLEL = "free_images_parallel"
    MIXED_SAFE = "mixed_safe"


@dataclass(frozen=True, slots=True)
class ConcurrencyModeResolution:
    mode: ExecutionConcurrencyMode
    selected_provider_ids: tuple[str, ...] = ()
    storyblocks_provider_ids: tuple[str, ...] = ()
    free_image_provider_ids: tuple[str, ...] = ()

    @property
    def uses_storyblocks(self) -> bool:
        return bool(self.storyblocks_provider_ids)

    @property
    def requires_serial_paragraph_workers(self) -> bool:
        return self.mode in {
            ExecutionConcurrencyMode.STORYBLOCKS_SAFE,
            ExecutionConcurrencyMode.MIXED_SAFE,
        }


@dataclass(slots=True)
class ProviderRegistry:
    providers: dict[str, ProviderDescriptor] = field(default_factory=dict)

    def register(self, descriptor: ProviderDescriptor) -> None:
        self.providers[descriptor.provider_id] = descriptor

    def get(self, provider_id: str) -> ProviderDescriptor | None:
        return self.providers.get(provider_id)

    def list_all(self) -> list[ProviderDescriptor]:
        return list(self.providers.values())

    def list_by_capability(
        self, capability: ProviderCapability
    ) -> list[ProviderDescriptor]:
        return [item for item in self.list_all() if item.capability == capability]

    def resolve_enabled(
        self,
        settings: ProviderSettings,
        *,
        capability: ProviderCapability | None = None,
    ) -> list[ProviderDescriptor]:
        descriptors: list[ProviderDescriptor] = []
        seen: set[str] = set()
        for provider_id in settings.enabled_providers:
            descriptor = self.get(provider_id)
            if descriptor is None or provider_id in seen:
                continue
            seen.add(provider_id)
            if capability is not None and descriptor.capability != capability:
                continue
            descriptors.append(descriptor)
        return descriptors

    def default_image_descriptors(self, settings: ProviderSettings) -> list[ProviderDescriptor]:
        defaults = set(settings.default_image_providers)
        ordered = self.resolve_enabled(
            settings,
            capability=ProviderCapability.IMAGE,
        )
        return [item for item in ordered if item.provider_id in defaults]

    def resolve_concurrency_mode(
        self,
        settings: ProviderSettings,
        *,
        video_enabled: bool = True,
        storyblocks_images_enabled: bool = True,
        free_images_enabled: bool = True,
    ) -> ConcurrencyModeResolution:
        enabled = self.resolve_enabled(
            settings,
            capability=None,
        )
        selected: list[ProviderDescriptor] = []
        for descriptor in enabled:
            if (
                descriptor.capability == ProviderCapability.VIDEO
                and not video_enabled
            ):
                continue
            if descriptor.capability != ProviderCapability.IMAGE:
                selected.append(descriptor)
                continue
            is_storyblocks_image = (
                descriptor.provider_id == STORYBLOCKS_IMAGE_PROVIDER_ID
            )
            if is_storyblocks_image and not storyblocks_images_enabled:
                continue
            if (not is_storyblocks_image) and not free_images_enabled:
                continue
            selected.append(descriptor)

        selected_ids = tuple(item.provider_id for item in selected)
        storyblocks_ids = tuple(
            item.provider_id
            for item in selected
            if item.provider_id in STORYBLOCKS_PROVIDER_IDS
        )
        free_image_ids = tuple(
            item.provider_id
            for item in selected
            if item.capability == ProviderCapability.IMAGE
            and item.provider_id in FREE_IMAGE_PROVIDER_IDS
        )

        has_storyblocks = bool(storyblocks_ids)
        has_free_images = bool(free_image_ids)
        if has_storyblocks and has_free_images:
            mode = ExecutionConcurrencyMode.MIXED_SAFE
        elif has_storyblocks:
            mode = ExecutionConcurrencyMode.STORYBLOCKS_SAFE
        else:
            mode = ExecutionConcurrencyMode.FREE_IMAGES_PARALLEL
        return ConcurrencyModeResolution(
            mode=mode,
            selected_provider_ids=selected_ids,
            storyblocks_provider_ids=storyblocks_ids,
            free_image_provider_ids=free_image_ids,
        )


def build_default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        ProviderDescriptor(
            provider_id="storyblocks_video",
            display_name="Storyblocks Video",
            capability=ProviderCapability.VIDEO,
            provider_group="storyblocks_video",
            priority=100,
            requires_auth=True,
            enabled_by_default=True,
            license_policy="storyblocks-license",
            metadata={
                "automation_stack": "playwright_persistent_context",
                "direct_search_path": "/all-video/search/{query_slug}",
                "homepage_search_supported": True,
            },
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="storyblocks_image",
            display_name="Storyblocks Images",
            capability=ProviderCapability.IMAGE,
            provider_group="storyblocks_images",
            priority=100,
            requires_auth=True,
            enabled_by_default=True,
            license_policy="storyblocks-license",
            metadata={
                "automation_stack": "playwright_persistent_context",
                "direct_search_path": "/images/search/{query_slug}",
                "homepage_search_supported": True,
                "supports_free_only_mode": False,
            },
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="pexels",
            display_name="Pexels Images",
            capability=ProviderCapability.IMAGE,
            provider_group="free_stock_api",
            priority=90,
            license_policy="pexels-license",
            legacy=True,
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="pixabay",
            display_name="Pixabay Images",
            capability=ProviderCapability.IMAGE,
            provider_group="free_stock_api",
            priority=85,
            license_policy="pixabay-license",
            legacy=True,
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="openverse",
            display_name="Openverse",
            capability=ProviderCapability.IMAGE,
            provider_group="open_license_repository",
            priority=70,
            license_policy="open-license",
            legacy=True,
        )
    )
    return registry
