from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from config.settings import ProviderSettings
from domain.project_modes import DEFAULT_FREE_IMAGE_PROVIDER_IDS

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


def resolve_execution_concurrency_mode(
    settings: ProviderSettings,
    *,
    video_enabled: bool = True,
    storyblocks_images_enabled: bool = True,
    free_images_enabled: bool = True,
) -> ConcurrencyModeResolution:
    selected_ids: list[str] = []
    seen: set[str] = set()
    for provider_id in settings.enabled_providers:
        normalized_id = str(provider_id).strip().casefold()
        if not normalized_id or normalized_id in seen:
            continue
        seen.add(normalized_id)
        if normalized_id == "storyblocks_video":
            if video_enabled:
                selected_ids.append(normalized_id)
            continue
        if normalized_id == STORYBLOCKS_IMAGE_PROVIDER_ID:
            if storyblocks_images_enabled:
                selected_ids.append(normalized_id)
            continue
        if normalized_id in FREE_IMAGE_PROVIDER_IDS and free_images_enabled:
            selected_ids.append(normalized_id)

    selected = tuple(selected_ids)
    storyblocks_ids = tuple(
        provider_id
        for provider_id in selected
        if provider_id in STORYBLOCKS_PROVIDER_IDS
    )
    free_image_ids = tuple(
        provider_id
        for provider_id in selected
        if provider_id in FREE_IMAGE_PROVIDER_IDS
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
        selected_provider_ids=selected,
        storyblocks_provider_ids=storyblocks_ids,
        free_image_provider_ids=free_image_ids,
    )
