from .contracts import (
    UiAdvancedSettingsViewModel,
    UiAssetPreview,
    UiErrorPayload,
    UiNotification,
    UiParagraphWorkbenchItem,
    UiPresetViewModel,
    UiProjectSummary,
    UiQuickLaunchSettingsViewModel,
    UiRunHistoryItem,
    UiRunPreviewViewModel,
    UiSessionPanelViewModel,
    UiStateViewModel,
)
from .controller import DesktopGuiController, handle_ui_error
from .tk_app import DesktopTkApp, launch_tk_app


def launch_desktop_app(controller: DesktopGuiController) -> None:
    try:
        from .qt_app import launch_pyside_app
    except ModuleNotFoundError as exc:
        if not str(getattr(exc, "name", "")).startswith("PySide6"):
            raise
        launch_tk_app(controller)
        return
    launch_pyside_app(controller)


__all__ = [
    "DesktopGuiController",
    "DesktopTkApp",
    "UiAdvancedSettingsViewModel",
    "UiAssetPreview",
    "UiErrorPayload",
    "UiNotification",
    "UiParagraphWorkbenchItem",
    "UiPresetViewModel",
    "UiProjectSummary",
    "UiQuickLaunchSettingsViewModel",
    "UiRunHistoryItem",
    "UiRunPreviewViewModel",
    "UiSessionPanelViewModel",
    "UiStateViewModel",
    "handle_ui_error",
    "launch_desktop_app",
    "launch_tk_app",
]
