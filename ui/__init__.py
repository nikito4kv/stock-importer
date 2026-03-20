from __future__ import annotations

from importlib import import_module
from typing import Callable

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
from .launch_profiles import (
    LaunchProfileCustomTiming,
    LaunchProfileId,
    ResolvedLaunchProfile,
)


def _load_qt_launcher() -> Callable[[DesktopGuiController], None]:
    try:
        module = import_module("ui.qt_app")
    except ModuleNotFoundError as exc:
        missing_name = str(getattr(exc, "name", "") or "")
        if missing_name == "PySide6" or missing_name.startswith("PySide6."):
            raise RuntimeError(
                "PySide6 is required to launch the desktop UI. "
                "Install PySide6 and retry."
            ) from exc
        raise
    return module.launch_pyside_app


def launch_desktop_app(controller: DesktopGuiController) -> None:
    _load_qt_launcher()(controller)


__all__ = [
    "DesktopGuiController",
    "LaunchProfileCustomTiming",
    "LaunchProfileId",
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
    "ResolvedLaunchProfile",
    "handle_ui_error",
    "launch_desktop_app",
]
