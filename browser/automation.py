from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from config.settings import BrowserSettings
from services.errors import ConfigError, SessionError

from .profiles import BrowserProfilePaths


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _current_thread_id() -> int:
    return threading.get_ident()


def _current_thread_name() -> str:
    return threading.current_thread().name


def _is_browser_internal_url(url: str) -> bool:
    normalized = (url or "").strip().casefold()
    return normalized.startswith(("chrome://", "edge://", "about:"))


def select_preferred_page(pages: list[Any], target_url: str = "") -> Any | None:
    if not pages:
        return None
    target = (target_url or "").casefold()
    storyblocks_pages = [
        page
        for page in pages
        if "storyblocks.com" in str(getattr(page, "url", "")).casefold()
    ]
    if storyblocks_pages:
        for page in storyblocks_pages:
            current_url = str(getattr(page, "url", "")).casefold()
            if target and target in current_url:
                return page
        return storyblocks_pages[0]
    if target:
        for page in pages:
            current_url = str(getattr(page, "url", "")).casefold()
            if current_url and target in current_url:
                return page
    for page in pages:
        if not _is_browser_internal_url(str(getattr(page, "url", ""))):
            return page
    return pages[0]


@dataclass(slots=True)
class BrowserChannelAvailability:
    channel: str
    executable_path: Path | None
    available: bool
    reason: str = ""


@dataclass(slots=True)
class BrowserLaunchPlan:
    profile_id: str
    browser_channel: str
    user_data_dir: Path
    profile_directory_name: str
    downloads_dir: Path
    diagnostics_dir: Path
    launch_timeout_ms: int
    navigation_timeout_ms: int
    slow_mode: bool
    action_delay_ms: int
    storyblocks_base_url: str


@dataclass(slots=True)
class PersistentBrowserHandle:
    context: Any
    page: Any
    close_callback: Callable[[], None]

    def close(self) -> None:
        self.close_callback()


@dataclass(slots=True)
class PersistentBrowserSession:
    plan: BrowserLaunchPlan
    handle: PersistentBrowserHandle
    kind: str = "persistent_context"
    lock_acquired: bool = False
    debug_endpoint_url: str = ""
    opened_at: datetime = field(default_factory=_now)
    owner_thread_id: int = field(default_factory=_current_thread_id)
    owner_thread_name: str = field(default_factory=_current_thread_name)


class PersistentContextFactory(Protocol):
    def launch(self, plan: BrowserLaunchPlan) -> PersistentBrowserHandle: ...

    def connect_over_cdp(
        self, plan: BrowserLaunchPlan, endpoint_url: str
    ) -> PersistentBrowserHandle: ...


class BrowserChannelResolver:
    def __init__(
        self,
        *,
        explicit_candidates: dict[str, list[Path]] | None = None,
        which_func: Callable[[str], str | None] | None = None,
    ):
        self._explicit_candidates = explicit_candidates or {}
        self._which = which_func or shutil.which

    def availability_report(
        self, preferred_channels: list[str]
    ) -> list[BrowserChannelAvailability]:
        report: list[BrowserChannelAvailability] = []
        for channel in preferred_channels:
            executable = self._find_channel_executable(channel)
            report.append(
                BrowserChannelAvailability(
                    channel=channel,
                    executable_path=executable,
                    available=executable is not None,
                    reason=""
                    if executable is not None
                    else f"Browser channel '{channel}' is not installed",
                )
            )
        return report

    def resolve(self, preferred_channels: list[str]) -> BrowserChannelAvailability:
        for entry in self.availability_report(preferred_channels):
            if entry.available:
                return entry
        raise ConfigError(
            code="browser_channel_unavailable",
            message=(
                "No supported browser channel is available. Install Chrome or Edge, or update browser settings."
            ),
            details={"preferred_channels": list(preferred_channels)},
        )

    def _find_channel_executable(self, channel: str) -> Path | None:
        for candidate in self._candidate_paths(channel):
            if candidate.exists() and candidate.is_file():
                return candidate
        command_name = "chrome" if channel == "chrome" else channel
        discovered = self._which(command_name)
        if discovered:
            return Path(discovered)
        return None

    def _candidate_paths(self, channel: str) -> list[Path]:
        if channel in self._explicit_candidates:
            return list(self._explicit_candidates[channel])

        program_files = Path(os.environ.get("PROGRAMFILES", ""))
        program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", ""))
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        paths: list[Path] = []

        if channel == "chrome":
            paths.extend(
                [
                    program_files / "Google/Chrome/Application/chrome.exe",
                    program_files_x86 / "Google/Chrome/Application/chrome.exe",
                    local_app_data / "Google/Chrome/Application/chrome.exe",
                ]
            )
        elif channel == "msedge":
            paths.extend(
                [
                    program_files / "Microsoft/Edge/Application/msedge.exe",
                    program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
                    local_app_data / "Microsoft/Edge/Application/msedge.exe",
                ]
            )
        return [path for path in paths if str(path) not in {".", ""}]


class BrowserProfileLockProbe:
    KNOWN_BROWSER_LOCKS = (
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "LOCK",
    )

    def is_profile_in_use(self, paths: BrowserProfilePaths) -> bool:
        if paths.automation_lock_path.exists():
            return True
        for name in self.KNOWN_BROWSER_LOCKS:
            if (paths.user_data_dir / name).exists() or (paths.root / name).exists():
                return True
        return False

    def acquire(self, paths: BrowserProfilePaths) -> None:
        payload = {
            "pid": os.getpid(),
            "acquired_at": _now().isoformat(),
        }
        paths.automation_lock_path.write_text(json.dumps(payload), encoding="utf-8")

    def release(self, paths: BrowserProfilePaths) -> None:
        if paths.automation_lock_path.exists():
            paths.automation_lock_path.unlink()


class PlaywrightPersistentContextFactory:
    def launch(self, plan: BrowserLaunchPlan) -> PersistentBrowserHandle:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise ConfigError(
                code="playwright_missing",
                message="Playwright is required for Storyblocks browser automation. Install it with: pip install playwright",
            ) from exc

        runner = sync_playwright().start()
        browser_context = None
        try:
            browser_context = runner.chromium.launch_persistent_context(
                user_data_dir=str(plan.user_data_dir),
                channel=plan.browser_channel,
                headless=False,
                accept_downloads=True,
                downloads_path=str(plan.downloads_dir),
                slow_mo=plan.action_delay_ms if plan.slow_mode else 0,
                timeout=plan.launch_timeout_ms,
                args=[f"--profile-directory={plan.profile_directory_name}"],
            )
            page = select_preferred_page(
                list(browser_context.pages),
                target_url=plan.storyblocks_base_url,
            )
            if page is None:
                page = browser_context.new_page()
            if hasattr(page, "set_default_timeout"):
                page.set_default_timeout(plan.navigation_timeout_ms)
            if hasattr(page, "set_default_navigation_timeout"):
                page.set_default_navigation_timeout(plan.navigation_timeout_ms)
        except Exception as exc:
            if browser_context is not None:
                browser_context.close()
            runner.stop()
            raise SessionError(
                code="persistent_context_failed",
                message=f"Unable to launch persistent browser context for channel '{plan.browser_channel}'",
                details={
                    "profile_id": plan.profile_id,
                    "browser_channel": plan.browser_channel,
                },
            ) from exc

        def close() -> None:
            try:
                browser_context.close()
            finally:
                runner.stop()

        return PersistentBrowserHandle(
            context=browser_context, page=page, close_callback=close
        )

    def connect_over_cdp(
        self, plan: BrowserLaunchPlan, endpoint_url: str
    ) -> PersistentBrowserHandle:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise ConfigError(
                code="playwright_missing",
                message="Playwright is required for Storyblocks browser automation. Install it with: pip install playwright",
            ) from exc

        runner = sync_playwright().start()
        browser = None
        browser_context = None
        try:
            browser = runner.chromium.connect_over_cdp(
                endpoint_url, timeout=plan.launch_timeout_ms
            )
            browser_context = browser.contexts[0] if browser.contexts else None
            if browser_context is None:
                raise SessionError(
                    code="cdp_context_missing",
                    message="Connected to the login browser, but Chromium did not expose a default context.",
                    details={
                        "profile_id": plan.profile_id,
                        "endpoint_url": endpoint_url,
                    },
                )
            page = select_preferred_page(
                list(browser_context.pages),
                target_url=plan.storyblocks_base_url,
            )
            if page is None:
                page = browser_context.new_page()
            if hasattr(page, "set_default_timeout"):
                page.set_default_timeout(plan.navigation_timeout_ms)
            if hasattr(page, "set_default_navigation_timeout"):
                page.set_default_navigation_timeout(plan.navigation_timeout_ms)
        except Exception as exc:
            try:
                runner.stop()
            finally:
                raise SessionError(
                    code="cdp_attach_failed",
                    message="Unable to connect Playwright to the browser window opened for Storyblocks login.",
                    details={
                        "profile_id": plan.profile_id,
                        "endpoint_url": endpoint_url,
                        "cause": str(exc),
                    },
                ) from exc

        def close() -> None:
            disconnect = getattr(browser, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                finally:
                    runner.stop()
                return
            runner.stop()

        return PersistentBrowserHandle(
            context=browser_context, page=page, close_callback=close
        )


def build_launch_plan(
    profile_id: str,
    profile_paths: BrowserProfilePaths,
    browser_channel: str,
    settings: BrowserSettings,
    *,
    profile_directory_name: str = "Default",
) -> BrowserLaunchPlan:
    return BrowserLaunchPlan(
        profile_id=profile_id,
        browser_channel=browser_channel,
        user_data_dir=profile_paths.user_data_dir,
        profile_directory_name=profile_directory_name or "Default",
        downloads_dir=profile_paths.downloads_dir,
        diagnostics_dir=profile_paths.diagnostics_dir,
        launch_timeout_ms=settings.launch_timeout_ms,
        navigation_timeout_ms=settings.navigation_timeout_ms,
        slow_mode=settings.slow_mode,
        action_delay_ms=settings.action_delay_ms,
        storyblocks_base_url=settings.storyblocks_base_url,
    )
