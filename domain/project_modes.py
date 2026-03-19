from __future__ import annotations

from dataclasses import dataclass


DEFAULT_FREE_IMAGE_PROVIDER_IDS: tuple[str, ...] = (
    "pexels",
    "pixabay",
    "openverse",
    "wikimedia",
)
OPT_IN_FREE_IMAGE_PROVIDER_IDS: tuple[str, ...] = ("bing",)
ALL_FREE_IMAGE_PROVIDER_IDS: tuple[str, ...] = (
    DEFAULT_FREE_IMAGE_PROVIDER_IDS + OPT_IN_FREE_IMAGE_PROVIDER_IDS
)


@dataclass(frozen=True, slots=True)
class ProjectModeDefinition:
    mode_id: str
    label: str
    description: str
    video_enabled: bool
    storyblocks_images_enabled: bool
    free_images_enabled: bool
    requires_storyblocks: bool


PROJECT_MODES: dict[str, ProjectModeDefinition] = {
    "sb_video_only": ProjectModeDefinition(
        mode_id="sb_video_only",
        label="Только видео Storyblocks",
        description="Основное видео берется из Storyblocks без слотов изображений.",
        video_enabled=True,
        storyblocks_images_enabled=False,
        free_images_enabled=False,
        requires_storyblocks=True,
    ),
    "sb_video_plus_sb_images": ProjectModeDefinition(
        mode_id="sb_video_plus_sb_images",
        label="Видео Storyblocks + изображения Storyblocks",
        description="Основное видео и дополнительные изображения берутся из Storyblocks.",
        video_enabled=True,
        storyblocks_images_enabled=True,
        free_images_enabled=False,
        requires_storyblocks=True,
    ),
    "sb_video_plus_free_images": ProjectModeDefinition(
        mode_id="sb_video_plus_free_images",
        label="Видео Storyblocks + бесплатные изображения",
        description="Основное видео берется из Storyblocks, а изображения из бесплатных источников.",
        video_enabled=True,
        storyblocks_images_enabled=False,
        free_images_enabled=True,
        requires_storyblocks=True,
    ),
    "sb_images_plus_free_images": ProjectModeDefinition(
        mode_id="sb_images_plus_free_images",
        label="Изображения Storyblocks + бесплатные изображения",
        description="Проект только с изображениями, где Storyblocks основной источник, а бесплатные источники служат запасным вариантом.",
        video_enabled=False,
        storyblocks_images_enabled=True,
        free_images_enabled=True,
        requires_storyblocks=True,
    ),
    "free_images_only": ProjectModeDefinition(
        mode_id="free_images_only",
        label="Только бесплатные изображения",
        description="Проект только с изображениями без авторизации в Storyblocks.",
        video_enabled=False,
        storyblocks_images_enabled=False,
        free_images_enabled=True,
        requires_storyblocks=False,
    ),
}


def list_project_modes() -> list[ProjectModeDefinition]:
    return list(PROJECT_MODES.values())


def get_project_mode(mode_id: str) -> ProjectModeDefinition:
    normalized = normalize_project_mode(mode_id)
    return PROJECT_MODES[normalized]


def normalize_project_mode(mode_id: str | None) -> str:
    candidate = (mode_id or "").strip().casefold()
    if candidate in PROJECT_MODES:
        return candidate
    return "sb_video_only"


def infer_project_mode(
    *,
    video_enabled: bool,
    storyblocks_images_enabled: bool,
    free_images_enabled: bool,
    mixed_image_fallback: bool | None = None,
) -> str:
    for definition in PROJECT_MODES.values():
        if (
            definition.video_enabled == bool(video_enabled)
            and definition.storyblocks_images_enabled
            == bool(storyblocks_images_enabled)
            and definition.free_images_enabled == bool(free_images_enabled)
        ):
            return definition.mode_id
    if video_enabled and storyblocks_images_enabled and free_images_enabled:
        return (
            "sb_video_plus_free_images"
            if mixed_image_fallback
            else "sb_video_plus_sb_images"
        )
    if not video_enabled and storyblocks_images_enabled and not free_images_enabled:
        return "sb_images_plus_free_images"
    if not any((video_enabled, storyblocks_images_enabled, free_images_enabled)):
        return "sb_video_only"
    raise ValueError(
        "Unsupported mode combination. Use one of the approved MVP modes from docs/phase-0/mode-matrix.md."
    )


def normalize_free_image_provider_ids(
    provider_ids: list[str] | tuple[str, ...] | None,
) -> list[str]:
    requested = [
        str(item).strip().casefold()
        for item in (provider_ids or [])
        if str(item).strip()
    ]
    normalized: list[str] = []
    seen: set[str] = set()
    for provider_id in requested:
        if provider_id not in ALL_FREE_IMAGE_PROVIDER_IDS or provider_id in seen:
            continue
        normalized.append(provider_id)
        seen.add(provider_id)
    return normalized


def provider_ids_for_mode(
    mode_id: str,
    *,
    free_image_provider_ids: list[str] | tuple[str, ...] | None = None,
    allow_generic_web_image: bool = False,
) -> list[str]:
    definition = get_project_mode(mode_id)
    provider_ids: list[str] = []
    if definition.video_enabled:
        provider_ids.append("storyblocks_video")
    if definition.storyblocks_images_enabled:
        provider_ids.append("storyblocks_image")
    if definition.free_images_enabled:
        selected_free = normalize_free_image_provider_ids(
            free_image_provider_ids
        ) or list(DEFAULT_FREE_IMAGE_PROVIDER_IDS)
        for provider_id in selected_free:
            if provider_id == "bing" and not allow_generic_web_image:
                continue
            provider_ids.append(provider_id)
    return list(dict.fromkeys(provider_ids))
