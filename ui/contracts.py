from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UiNotification:
    title: str
    message: str
    severity: str = "info"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UiErrorPayload:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UiPresetViewModel:
    name: str
    updated_at: str


@dataclass(slots=True)
class UiImportableBrowserProfileOption:
    browser_name: str
    browser_label: str
    profile_name: str
    profile_dir: str
    user_data_root: str
    display_label: str
    locked: bool = False


@dataclass(slots=True)
class UiProjectSummary:
    project_id: str
    name: str
    source_path: str
    paragraphs_total: int
    header_text: str = ""
    numbering_issues: list[str] = field(default_factory=list)
    active_run_id: str | None = None
    updated_at: str = ""


@dataclass(slots=True)
class UiRunHistoryItem:
    run_id: str
    project_id: str
    status: str
    created_at: str
    finished_at: str = ""
    paragraphs_completed: int = 0
    paragraphs_failed: int = 0
    stage: str = ""


@dataclass(slots=True)
class UiDownloadedFileItem:
    asset_id: str
    provider_name: str
    kind: str
    role: str
    title: str
    local_path: str
    exists: bool = False


@dataclass(slots=True)
class UiParagraphWorkbenchItem:
    paragraph_no: int
    original_index: int
    text: str
    numbering_valid: bool
    validation_issues: list[str]
    status: str
    result_note: str
    intent_summary: str
    current_stage: str = ""
    current_provider_name: str = ""
    current_query: str = ""
    video_queries: list[str] = field(default_factory=list)
    image_queries: list[str] = field(default_factory=list)
    downloaded_files: list[UiDownloadedFileItem] = field(default_factory=list)


@dataclass(slots=True)
class UiSessionPanelViewModel:
    health: str = "unknown"
    account: str = ""
    browser_ready: bool = False
    native_login_running: bool = False
    native_debug_port: int | None = None
    current_url: str = ""
    manual_prompt: str = ""
    last_error: str = ""
    indicator_tone: str = "neutral"
    imported_source: str = ""
    imported_profile_name: str = ""
    imported_at: str = ""
    manual_ready_override: bool = False
    manual_ready_override_note: str = ""
    reason_code: str = ""
    diagnostic_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UiRunPreviewViewModel:
    project_name: str
    output_dir: str
    paragraphs_total: int
    selected_paragraphs: int
    mode_id: str = "sb_video_only"
    mode_label: str = "Только видео Storyblocks"
    providers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    session_health: str = "unknown"


@dataclass(slots=True)
class UiRunProgressViewModel:
    run_id: str
    status: str
    eta_text: str = ""
    current_stage: str = ""
    current_paragraph_no: int | None = None
    current_provider_name: str = ""
    current_query: str = ""
    current_asset_id: str = ""
    paragraphs_total: int = 0
    paragraphs_processed: int = 0
    paragraphs_completed: int = 0
    project_progress_total: int = 0
    project_progress_completed: int = 0
    paragraphs_no_match: int = 0
    paragraph_progress_total: int = 0
    paragraph_progress_completed: int = 0
    paragraphs_failed: int = 0
    downloads_root: str = ""
    videos_dir: str = ""
    images_dir: str = ""
    downloaded_video_files: int = 0
    downloaded_image_files: int = 0
    percent_complete: float = 0.0
    live_state: str = "idle"
    can_cancel: bool = False


@dataclass(slots=True)
class UiLiveRunStateViewModel:
    active_run_id: str | None = None
    status_text: str = "готово"
    paragraph_items: list[UiParagraphWorkbenchItem] = field(default_factory=list)
    run_progress: UiRunProgressViewModel | None = None


@dataclass(slots=True)
class UiLiveSnapshotViewModel:
    active_run_id: str | None = None
    status_text: str = "готово"
    run_progress: UiRunProgressViewModel | None = None


@dataclass(slots=True)
class UiQuickLaunchSettingsViewModel:
    project_name: str = ""
    script_path: str = ""
    output_dir: str = ""
    paragraph_selection_text: str = ""
    selected_paragraphs: list[int] = field(default_factory=list)
    mode_id: str = "sb_video_only"
    launch_profile_id: str = "normal"
    strictness: str = "balanced"
    provider_ids: list[str] = field(default_factory=list)
    supporting_image_limit: int = 1
    fallback_image_limit: int = 1
    manual_prompt: str = ""
    attach_full_script_context: bool = False


@dataclass(slots=True)
class UiAdvancedSettingsViewModel:
    action_delay_ms: int = 900
    launch_timeout_ms: int = 45000
    navigation_timeout_ms: int = 30000
    downloads_timeout_seconds: float = 120.0


@dataclass(slots=True)
class UiStateViewModel:
    active_project_id: str | None = None
    active_run_id: str | None = None
    status_text: str = "готово"
    progress_total: int = 0
    progress_completed: int = 0
    warnings: list[str] = field(default_factory=list)
    notifications: list[UiNotification] = field(default_factory=list)
    projects: list[UiProjectSummary] = field(default_factory=list)
    run_history: list[UiRunHistoryItem] = field(default_factory=list)
    presets: list[UiPresetViewModel] = field(default_factory=list)
    paragraph_items: list[UiParagraphWorkbenchItem] = field(default_factory=list)
    session: UiSessionPanelViewModel = field(default_factory=UiSessionPanelViewModel)
    quick_launch: UiQuickLaunchSettingsViewModel = field(
        default_factory=UiQuickLaunchSettingsViewModel
    )
    advanced: UiAdvancedSettingsViewModel = field(
        default_factory=UiAdvancedSettingsViewModel
    )
    run_preview: UiRunPreviewViewModel | None = None
    run_progress: UiRunProgressViewModel | None = None
