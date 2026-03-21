from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import ProviderSettings
from domain.enums import ProviderCapability

from .base import ProviderDescriptor


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

    def default_image_descriptors(
        self, settings: ProviderSettings
    ) -> list[ProviderDescriptor]:
        defaults = set(settings.default_image_providers)
        ordered = self.resolve_enabled(
            settings,
            capability=ProviderCapability.IMAGE,
        )
        return [item for item in ordered if item.provider_id in defaults]


def build_default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        ProviderDescriptor(
            provider_id="storyblocks_video",
            display_name="Storyblocks Video",
            capability=ProviderCapability.VIDEO,
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
            license_policy="pexels-license",
            legacy=True,
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="pixabay",
            display_name="Pixabay Images",
            capability=ProviderCapability.IMAGE,
            license_policy="pixabay-license",
            legacy=True,
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="openverse",
            display_name="Openverse",
            capability=ProviderCapability.IMAGE,
            license_policy="open-license",
            legacy=True,
        )
    )
    return registry
