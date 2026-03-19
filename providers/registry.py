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
        return sorted(
            self.providers.values(),
            key=lambda item: (-item.priority, item.provider_id),
        )

    def list_by_capability(self, capability: ProviderCapability) -> list[ProviderDescriptor]:
        return [item for item in self.list_all() if item.capability == capability]

    def list_by_group(
        self,
        provider_group: str,
        capability: ProviderCapability | None = None,
    ) -> list[ProviderDescriptor]:
        return [
            item
            for item in self.list_all()
            if item.provider_group == provider_group
            and (capability is None or item.capability == capability)
        ]

    def resolve_enabled(
        self,
        settings: ProviderSettings,
        *,
        capability: ProviderCapability | None = None,
        include_opt_in: bool = False,
    ) -> list[ProviderDescriptor]:
        enabled_ids = set(settings.enabled_providers)
        priority_map = {provider_id: idx for idx, provider_id in enumerate(settings.image_provider_priority)}

        descriptors: list[ProviderDescriptor] = []
        for descriptor in self.list_all():
            if capability is not None and descriptor.capability != capability:
                continue
            if descriptor.provider_id not in enabled_ids:
                continue
            if descriptor.provider_group == "generic_web_image" and not settings.allow_generic_web_image and not include_opt_in:
                continue
            if descriptor.opt_in and not include_opt_in and descriptor.provider_id not in settings.default_image_providers:
                continue
            if descriptor.provider_group == "storyblocks_images" and settings.free_images_only:
                continue
            descriptors.append(descriptor)

        def sort_key(item: ProviderDescriptor) -> tuple[int, int, str]:
            priority_index = priority_map.get(item.provider_id, len(priority_map) + 100)
            return (priority_index, -item.priority, item.provider_id)

        return sorted(descriptors, key=sort_key)

    def default_image_descriptors(self, settings: ProviderSettings) -> list[ProviderDescriptor]:
        defaults = set(settings.default_image_providers)
        ordered = self.resolve_enabled(settings, capability=ProviderCapability.IMAGE, include_opt_in=False)
        return [item for item in ordered if item.provider_id in defaults]

    def resolve_image_strategy(self, settings: ProviderSettings) -> dict[str, list[ProviderDescriptor]]:
        enabled = self.resolve_enabled(settings, capability=ProviderCapability.IMAGE, include_opt_in=False)
        if settings.free_images_only:
            free_only = [item for item in enabled if item.provider_group != "storyblocks_images"]
            return {"primary": free_only, "fallback": [], "separate": []}

        if settings.mixed_image_fallback:
            primary = [item for item in enabled if item.provider_group == "storyblocks_images"]
            fallback = [item for item in enabled if item.provider_group != "storyblocks_images"]
            return {"primary": primary, "fallback": fallback, "separate": []}

        return {"primary": enabled, "fallback": [], "separate": []}


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
    registry.register(
        ProviderDescriptor(
            provider_id="wikimedia",
            display_name="Wikimedia Commons",
            capability=ProviderCapability.IMAGE,
            provider_group="open_license_repository",
            priority=65,
            license_policy="open-license",
            legacy=True,
        )
    )
    registry.register(
        ProviderDescriptor(
            provider_id="bing",
            display_name="Bing Image Search",
            capability=ProviderCapability.IMAGE,
            provider_group="generic_web_image",
            priority=20,
            enabled_by_default=False,
            opt_in=True,
            legacy=True,
            license_policy="unknown",
            metadata={
                "quality_risk": "high",
                "default_enabled": False,
            },
        )
    )
    return registry
