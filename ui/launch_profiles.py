from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from providers import ExecutionConcurrencyMode

LaunchProfileId = Literal["normal", "fast", "custom"]


@dataclass(frozen=True, slots=True)
class LaunchProfileDefinition:
    launch_profile_id: LaunchProfileId
    label: str
    action_delay_ms: int
    launch_timeout_ms: int
    navigation_timeout_ms: int
    downloads_timeout_seconds: float
    provider_workers: int
    provider_queue_size: int
    download_workers: int
    download_queue_size: int
    search_timeout_seconds: float
    retry_budget: int
    fail_fast_storyblocks_errors: bool
    no_match_budget_seconds: float

    @property
    def slow_mode(self) -> bool:
        return self.action_delay_ms > 0


@dataclass(frozen=True, slots=True)
class LaunchProfileCustomTiming:
    action_delay_ms: int
    launch_timeout_ms: int
    navigation_timeout_ms: int
    downloads_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ResolvedLaunchProfile:
    launch_profile_id: LaunchProfileId
    label: str
    slow_mode: bool
    action_delay_ms: int
    launch_timeout_ms: int
    navigation_timeout_ms: int
    downloads_timeout_seconds: float
    paragraph_workers: int
    queue_size: int
    provider_workers: int
    provider_queue_size: int
    download_workers: int
    download_queue_size: int
    search_timeout_seconds: float
    retry_budget: int
    fail_fast_storyblocks_errors: bool
    no_match_budget_seconds: float


LAUNCH_PROFILE_LABELS: dict[str, str] = {
    "normal": "Обычный",
    "fast": "Быстрый",
    "custom": "Custom",
}

_PROFILE_DEFINITIONS: dict[str, LaunchProfileDefinition] = {
    "normal": LaunchProfileDefinition(
        launch_profile_id="normal",
        label=LAUNCH_PROFILE_LABELS["normal"],
        action_delay_ms=900,
        launch_timeout_ms=45000,
        navigation_timeout_ms=30000,
        downloads_timeout_seconds=120.0,
        provider_workers=4,
        provider_queue_size=8,
        download_workers=4,
        download_queue_size=8,
        search_timeout_seconds=20.0,
        retry_budget=2,
        fail_fast_storyblocks_errors=True,
        no_match_budget_seconds=20.0,
    ),
    "fast": LaunchProfileDefinition(
        launch_profile_id="fast",
        label=LAUNCH_PROFILE_LABELS["fast"],
        action_delay_ms=0,
        launch_timeout_ms=30000,
        navigation_timeout_ms=20000,
        downloads_timeout_seconds=90.0,
        provider_workers=4,
        provider_queue_size=8,
        download_workers=4,
        download_queue_size=8,
        search_timeout_seconds=12.0,
        retry_budget=1,
        fail_fast_storyblocks_errors=True,
        no_match_budget_seconds=10.0,
    ),
}

_ORCHESTRATOR_LIMITS: dict[ExecutionConcurrencyMode, tuple[int, int]] = {
    ExecutionConcurrencyMode.STORYBLOCKS_SAFE: (1, 1),
    ExecutionConcurrencyMode.MIXED_SAFE: (1, 1),
    ExecutionConcurrencyMode.FREE_IMAGES_PARALLEL: (2, 2),
}

_FAST_ORCHESTRATOR_LIMITS: dict[ExecutionConcurrencyMode, tuple[int, int]] = {
    ExecutionConcurrencyMode.STORYBLOCKS_SAFE: (1, 1),
    ExecutionConcurrencyMode.MIXED_SAFE: (1, 1),
    ExecutionConcurrencyMode.FREE_IMAGES_PARALLEL: (4, 4),
}


def list_launch_profile_ids() -> list[LaunchProfileId]:
    return ["normal", "fast", "custom"]


def normalize_launch_profile_id(value: str | None) -> LaunchProfileId:
    candidate = (value or "").strip().casefold()
    if candidate in {"normal", "fast", "custom"}:
        return candidate  # type: ignore[return-value]
    return "normal"


def get_launch_profile_label(value: str | None) -> str:
    launch_profile_id = normalize_launch_profile_id(value)
    return LAUNCH_PROFILE_LABELS[launch_profile_id]


def default_custom_timing() -> LaunchProfileCustomTiming:
    base = _PROFILE_DEFINITIONS["normal"]
    return LaunchProfileCustomTiming(
        action_delay_ms=base.action_delay_ms,
        launch_timeout_ms=base.launch_timeout_ms,
        navigation_timeout_ms=base.navigation_timeout_ms,
        downloads_timeout_seconds=base.downloads_timeout_seconds,
    )


def resolve_launch_profile(
    launch_profile_id: str | None,
    concurrency_mode: ExecutionConcurrencyMode,
    *,
    custom_timing: LaunchProfileCustomTiming | None = None,
) -> ResolvedLaunchProfile:
    normalized_id = normalize_launch_profile_id(launch_profile_id)
    base_profile_id: LaunchProfileId = (
        "normal" if normalized_id == "custom" else normalized_id
    )
    definition = _PROFILE_DEFINITIONS[base_profile_id]
    paragraph_workers, queue_size = _orchestrator_limits(
        base_profile_id, concurrency_mode
    )
    action_delay_ms = definition.action_delay_ms
    launch_timeout_ms = definition.launch_timeout_ms
    navigation_timeout_ms = definition.navigation_timeout_ms
    downloads_timeout_seconds = definition.downloads_timeout_seconds
    if normalized_id == "custom":
        overrides = custom_timing or default_custom_timing()
        action_delay_ms = max(0, int(overrides.action_delay_ms))
        launch_timeout_ms = max(1000, int(overrides.launch_timeout_ms))
        navigation_timeout_ms = max(1000, int(overrides.navigation_timeout_ms))
        downloads_timeout_seconds = max(1.0, float(overrides.downloads_timeout_seconds))
    return ResolvedLaunchProfile(
        launch_profile_id=normalized_id,
        label=get_launch_profile_label(normalized_id),
        slow_mode=action_delay_ms > 0,
        action_delay_ms=action_delay_ms,
        launch_timeout_ms=launch_timeout_ms,
        navigation_timeout_ms=navigation_timeout_ms,
        downloads_timeout_seconds=downloads_timeout_seconds,
        paragraph_workers=paragraph_workers,
        queue_size=queue_size,
        provider_workers=definition.provider_workers,
        provider_queue_size=definition.provider_queue_size,
        download_workers=definition.download_workers,
        download_queue_size=definition.download_queue_size,
        search_timeout_seconds=definition.search_timeout_seconds,
        retry_budget=definition.retry_budget,
        fail_fast_storyblocks_errors=definition.fail_fast_storyblocks_errors,
        no_match_budget_seconds=definition.no_match_budget_seconds,
    )


def custom_timing_from_resolved(
    profile: ResolvedLaunchProfile,
) -> LaunchProfileCustomTiming:
    return LaunchProfileCustomTiming(
        action_delay_ms=profile.action_delay_ms,
        launch_timeout_ms=profile.launch_timeout_ms,
        navigation_timeout_ms=profile.navigation_timeout_ms,
        downloads_timeout_seconds=profile.downloads_timeout_seconds,
    )


def describe_custom_timing_overrides(
    custom_timing: LaunchProfileCustomTiming,
) -> list[str]:
    base = default_custom_timing()
    lines: list[str] = []
    if custom_timing.action_delay_ms != base.action_delay_ms:
        lines.append(f"задержка действий {custom_timing.action_delay_ms} мс")
    if custom_timing.launch_timeout_ms != base.launch_timeout_ms:
        lines.append(f"таймаут запуска {custom_timing.launch_timeout_ms} мс")
    if custom_timing.navigation_timeout_ms != base.navigation_timeout_ms:
        lines.append(f"таймаут навигации {custom_timing.navigation_timeout_ms} мс")
    if (
        abs(custom_timing.downloads_timeout_seconds - base.downloads_timeout_seconds)
        > 1e-9
    ):
        lines.append(
            f"таймаут скачивания {custom_timing.downloads_timeout_seconds:.1f} с"
        )
    return lines


def _orchestrator_limits(
    launch_profile_id: LaunchProfileId,
    concurrency_mode: ExecutionConcurrencyMode,
) -> tuple[int, int]:
    if launch_profile_id == "fast":
        return _FAST_ORCHESTRATOR_LIMITS[concurrency_mode]
    return _ORCHESTRATOR_LIMITS[concurrency_mode]
