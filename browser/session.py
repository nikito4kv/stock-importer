from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
from typing import Any, Protocol

from config.settings import BrowserSettings
from domain.enums import SessionHealth
from services.errors import ConfigError, SessionError
from storage.serialization import write_json

from .automation import (
    BrowserChannelResolver,
    BrowserProfileLockProbe,
    PersistentBrowserSession,
    PlaywrightPersistentContextFactory,
    build_launch_plan,
    select_preferred_page,
)
from .native_browser import (
    NativeBrowserLaunchPlan,
    NativeBrowserSession,
    SubprocessNativeBrowserLauncher,
    find_available_tcp_port,
)
from .profiles import BrowserProfileRegistry
from .slowmode import BrowserActionPacer, SlowModePolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _current_thread_id() -> int:
    return threading.get_ident()


def _reason_code_for_health(health: SessionHealth) -> str:
    return {
        SessionHealth.READY: "session_ready",
        SessionHealth.LOGIN_REQUIRED: "login_required",
        SessionHealth.CHALLENGE: "challenge_detected",
        SessionHealth.EXPIRED: "session_expired",
        SessionHealth.BLOCKED: "access_blocked",
        SessionHealth.UNKNOWN: "session_unknown",
    }.get(health, "session_unknown")


@dataclass(slots=True)
class ManualInterventionRequest:
    reason: str
    prompt: str
    requested_at: datetime
    paragraph_no: int | None = None
    query: str | None = None
    rescue_url: str | None = None


@dataclass(slots=True)
class AuthorizationSnapshot:
    health: SessionHealth
    account: str | None = None
    current_url: str | None = None
    message: str = ""


class SessionProbe(Protocol):
    def inspect_document(
        self, html: str, current_url: str = ""
    ) -> AuthorizationSnapshot: ...

    def inspect_page(self, page: Any) -> AuthorizationSnapshot: ...


@dataclass(slots=True)
class BrowserSessionState:
    profile_id: str | None
    health: SessionHealth
    last_checked_at: datetime
    browser_channel: str | None = None
    browser_available: bool = False
    profile_in_use: bool = False
    persistent_context_ready: bool = False
    authenticated: bool = False
    storyblocks_account: str | None = None
    current_url: str | None = None
    rescue_url: str | None = None
    manual_intervention: ManualInterventionRequest | None = None
    last_error: str | None = None
    native_login_running: bool = False
    native_debug_port: int | None = None
    manual_ready_override: bool = False
    manual_ready_override_note: str | None = None
    reason_code: str = "session_unknown"
    diagnostics: dict[str, str] = field(default_factory=dict)


class BrowserSessionManager:
    def __init__(
        self,
        profile_registry: BrowserProfileRegistry,
        settings: BrowserSettings | None = None,
        *,
        channel_resolver: BrowserChannelResolver | None = None,
        context_factory: Any | None = None,
        native_browser_launcher: Any | None = None,
        session_probe: SessionProbe | None = None,
        lock_probe: BrowserProfileLockProbe | None = None,
    ):
        self._profile_registry = profile_registry
        self._settings = settings or BrowserSettings()
        self._channel_resolver = channel_resolver or BrowserChannelResolver()
        self._context_factory = context_factory or PlaywrightPersistentContextFactory()
        self._native_browser_launcher = (
            native_browser_launcher or SubprocessNativeBrowserLauncher()
        )
        self._session_probe = session_probe
        self._lock_probe = lock_probe or BrowserProfileLockProbe()
        self._action_pacer = BrowserActionPacer(
            SlowModePolicy.from_settings(self._settings)
        )
        self._active_sessions: dict[str, PersistentBrowserSession] = {}
        self._native_browser_sessions: dict[str, NativeBrowserSession] = {}
        self._states: dict[str, BrowserSessionState] = {}
        self._lock = threading.RLock()

    def current_state(self, profile_id: str | None = None) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id, allow_missing=True)
        if profile is None:
            return BrowserSessionState(None, SessionHealth.UNKNOWN, _now())

        native_session = self._refresh_native_browser_session(profile.profile_id)

        with self._lock:
            state = self._states.get(profile.profile_id)
            if state is not None:
                state.native_login_running = native_session is not None
                state.native_debug_port = (
                    native_session.plan.remote_debugging_port
                    if native_session is not None
                    else None
                )
                return state
        return BrowserSessionState(
            profile_id=profile.profile_id,
            health=profile.session_health,
            last_checked_at=_now(),
            authenticated=profile.session_health == SessionHealth.READY,
            storyblocks_account=profile.storyblocks_account,
            native_login_running=native_session is not None,
            native_debug_port=native_session.plan.remote_debugging_port
            if native_session is not None
            else None,
            reason_code=_reason_code_for_health(profile.session_health),
            diagnostics={
                "health": profile.session_health.value,
                "storyblocks_account": profile.storyblocks_account or "",
            },
        )

    def set_health(self, profile_id: str, health: SessionHealth) -> BrowserSessionState:
        profile = self._profile_registry.update_session_health(profile_id, health)
        state = self.current_state(profile.profile_id)
        state.health = profile.session_health
        state.authenticated = profile.session_health == SessionHealth.READY
        state.reason_code = _reason_code_for_health(profile.session_health)
        state.diagnostics = {
            "health": profile.session_health.value,
            "storyblocks_account": state.storyblocks_account or "",
        }
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def set_manual_ready_override(
        self,
        profile_id: str | None = None,
        *,
        note: str = "Marked ready manually by the operator.",
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        state = self.current_state(profile.profile_id)
        state.health = SessionHealth.READY
        state.authenticated = True
        state.manual_intervention = None
        state.manual_ready_override = True
        state.manual_ready_override_note = note
        state.last_error = None
        state.last_checked_at = _now()
        self._profile_registry.update_session_health(
            profile.profile_id, SessionHealth.READY
        )
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def clear_manual_ready_override(
        self, profile_id: str | None = None
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        state = self.current_state(profile.profile_id)
        state.manual_ready_override = False
        state.manual_ready_override_note = None
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def has_manual_ready_override(self, profile_id: str | None = None) -> bool:
        return self.current_state(profile_id).manual_ready_override

    def check_browser_channel(
        self, profile_id: str | None = None
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        state = self.current_state(profile.profile_id)
        availability = self._channel_resolver.resolve(self._settings.preferred_channels)
        state.browser_channel = availability.channel
        state.browser_available = availability.available
        state.reason_code = (
            "browser_channel_ready"
            if availability.available
            else "browser_channel_unavailable"
        )
        state.diagnostics = {
            "browser_channel": availability.channel or "",
            "browser_available": str(bool(availability.available)).lower(),
            "channel_reason": availability.reason or "",
        }
        state.last_error = availability.reason or None
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def profile_in_use(self, profile_id: str | None = None) -> bool:
        profile = self._resolve_profile(profile_id)
        with self._lock:
            if profile.profile_id in self._active_sessions:
                return False
        return self._lock_probe.is_profile_in_use(
            self._profile_registry.paths_for(profile)
        )

    def open_browser(self, profile_id: str | None = None) -> PersistentBrowserSession:
        profile = self._resolve_profile(profile_id)
        with self._lock:
            existing = self._active_sessions.get(profile.profile_id)
        if existing is not None:
            if existing.owner_thread_id != _current_thread_id():
                raise SessionError(
                    code="browser_session_thread_mismatch",
                    message="The cached Storyblocks browser session belongs to another thread and cannot be reused safely.",
                    details={
                        "profile_id": profile.profile_id,
                        "owner_thread_id": existing.owner_thread_id,
                        "owner_thread_name": existing.owner_thread_name,
                        "current_thread_id": _current_thread_id(),
                    },
                )
            state = self.current_state(profile.profile_id)
            state.persistent_context_ready = True
            state.browser_available = True
            with self._lock:
                self._states[profile.profile_id] = state
            return existing

        if self._refresh_native_browser_session(profile.profile_id) is not None:
            return self._attach_to_native_browser(profile.profile_id)

        profile_paths = self._profile_registry.paths_for(profile)
        availability = self._channel_resolver.resolve(self._settings.preferred_channels)
        if self._lock_probe.is_profile_in_use(profile_paths):
            state = self.current_state(profile.profile_id)
            state.profile_in_use = True
            state.browser_channel = availability.channel
            state.browser_available = availability.available
            state.last_checked_at = _now()
            state.last_error = (
                "Browser profile is already in use by another browser process"
            )
            with self._lock:
                self._states[profile.profile_id] = state
            raise SessionError(
                code="browser_profile_in_use",
                message="The selected browser profile is already in use by another process.",
                details={"profile_id": profile.profile_id},
            )

        plan = build_launch_plan(
            profile.profile_id,
            profile_paths,
            availability.channel,
            self._settings,
            profile_directory_name=profile.launch_profile_dir_name or "Default",
        )
        try:
            handle = self._context_factory.launch(plan)
        except ConfigError:
            raise
        except SessionError as exc:
            raise SessionError(
                code=exc.code,
                message=exc.message,
                details={
                    **dict(exc.details),
                    "profile_id": profile.profile_id,
                    "browser_channel": availability.channel,
                },
            ) from exc
        except Exception as exc:
            raise SessionError(
                code="browser_launch_failed",
                message=f"Failed to open persistent browser for profile '{profile.display_name}'.",
                details={
                    "profile_id": profile.profile_id,
                    "browser_channel": availability.channel,
                    "cause": str(exc),
                },
            ) from exc

        self._lock_probe.acquire(profile_paths)
        session = PersistentBrowserSession(
            plan=plan, handle=handle, kind="persistent_context", lock_acquired=True
        )
        with self._lock:
            self._active_sessions[profile.profile_id] = session

        state = self.current_state(profile.profile_id)
        state.browser_channel = availability.channel
        state.browser_available = True
        state.profile_in_use = False
        state.persistent_context_ready = True
        state.last_error = None
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return session

    def open_native_login_browser(
        self,
        profile_id: str | None = None,
        *,
        url: str | None = None,
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        self.close_browser(profile.profile_id)
        existing = self._refresh_native_browser_session(profile.profile_id)
        if existing is not None:
            state = self.current_state(profile.profile_id)
            state.browser_channel = existing.plan.browser_channel
            state.browser_available = True
            state.profile_in_use = False
            state.persistent_context_ready = False
            state.last_error = None
            state.reason_code = "native_login_in_progress"
            state.diagnostics = {
                "browser_channel": existing.plan.browser_channel,
                "target_url": existing.plan.target_url,
                "native_login_running": "true",
            }
            state.last_checked_at = _now()
            state.manual_intervention = ManualInterventionRequest(
                reason="native_login",
                prompt="Finish login in the opened browser window, then click Check Session.",
                requested_at=_now(),
                rescue_url=existing.plan.target_url,
            )
            state.native_login_running = True
            state.native_debug_port = existing.plan.remote_debugging_port
            self._write_native_login_diagnostics(
                profile.profile_id, status="native_browser_reused"
            )
            with self._lock:
                self._states[profile.profile_id] = state
            return state

        profile_paths = self._profile_registry.paths_for(profile)
        if self._lock_probe.is_profile_in_use(profile_paths):
            state = self.current_state(profile.profile_id)
            state.profile_in_use = True
            state.last_checked_at = _now()
            state.last_error = "The managed Storyblocks profile is already open in another browser window"
            with self._lock:
                self._states[profile.profile_id] = state
            raise SessionError(
                code="browser_profile_in_use",
                message="Close the browser window that already uses this managed Storyblocks profile, then try again.",
                details={"profile_id": profile.profile_id},
            )

        availability = self._channel_resolver.resolve(self._settings.preferred_channels)
        if availability.executable_path is None:
            raise ConfigError(
                code="browser_channel_unavailable",
                message="No supported browser channel is available. Install Chrome or Edge, or update browser settings.",
                details={"preferred_channels": list(self._settings.preferred_channels)},
            )
        target_url = url or f"{self._settings.storyblocks_base_url.rstrip('/')}/login"
        remote_debugging_port = find_available_tcp_port()
        native_session = self._native_browser_launcher.launch(
            NativeBrowserLaunchPlan(
                profile_id=profile.profile_id,
                browser_channel=availability.channel,
                executable_path=availability.executable_path,
                user_data_dir=profile_paths.user_data_dir,
                profile_directory_name=profile.launch_profile_dir_name or "Default",
                remote_debugging_port=remote_debugging_port,
                target_url=target_url,
            )
        )
        with self._lock:
            self._native_browser_sessions[profile.profile_id] = native_session
        self._profile_registry.update_session_health(
            profile.profile_id, SessionHealth.LOGIN_REQUIRED
        )
        state = self.current_state(profile.profile_id)
        state.health = SessionHealth.LOGIN_REQUIRED
        state.authenticated = False
        state.browser_channel = availability.channel
        state.browser_available = True
        state.profile_in_use = False
        state.persistent_context_ready = False
        state.current_url = target_url
        state.manual_intervention = ManualInterventionRequest(
            reason="native_login",
            prompt=f"Sign in to Storyblocks in the opened {availability.channel} window, then click Check Session.",
            requested_at=_now(),
            rescue_url=target_url,
        )
        state.last_error = None
        state.reason_code = "native_login_in_progress"
        state.diagnostics = {
            "browser_channel": availability.channel or "",
            "target_url": target_url,
            "native_login_running": "true",
        }
        state.last_checked_at = _now()
        state.native_login_running = True
        state.native_debug_port = remote_debugging_port
        self._write_native_login_diagnostics(
            profile.profile_id, status="native_browser_started"
        )
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def close_native_browser(self, profile_id: str | None = None) -> None:
        profile = self._resolve_profile(profile_id)
        with self._lock:
            attached = self._active_sessions.get(profile.profile_id)
        if attached is not None and attached.kind == "native_debug_attach":
            self.close_browser(profile.profile_id)
        with self._lock:
            native_session = self._native_browser_sessions.pop(profile.profile_id, None)
        if native_session is not None and native_session.is_running():
            native_session.terminate()
            time.sleep(0.2)
        state = self.current_state(profile.profile_id)
        state.profile_in_use = False
        state.native_login_running = False
        state.native_debug_port = None
        state.last_checked_at = _now()
        self._write_native_login_diagnostics(
            profile.profile_id, status="native_browser_closed"
        )
        with self._lock:
            self._states[profile.profile_id] = state

    def native_browser_running(self, profile_id: str | None = None) -> bool:
        profile = self._resolve_profile(profile_id)
        return self._refresh_native_browser_session(profile.profile_id) is not None

    def close_browser(self, profile_id: str | None = None) -> None:
        profile = self._resolve_profile(profile_id)
        with self._lock:
            session = self._active_sessions.get(profile.profile_id)
        if session is not None and session.owner_thread_id != _current_thread_id():
            raise SessionError(
                code="browser_session_thread_mismatch",
                message="The Storyblocks browser session belongs to another thread and cannot be closed safely from here.",
                details={
                    "profile_id": profile.profile_id,
                    "owner_thread_id": session.owner_thread_id,
                    "owner_thread_name": session.owner_thread_name,
                    "current_thread_id": _current_thread_id(),
                },
            )
        with self._lock:
            session = self._active_sessions.pop(profile.profile_id, None)
        if session is not None:
            try:
                session.handle.close()
            finally:
                if session.lock_acquired:
                    self._lock_probe.release(self._profile_registry.paths_for(profile))
        state = self.current_state(profile.profile_id)
        state.persistent_context_ready = False
        state.profile_in_use = False
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state

    def close_browsers_owned_by_current_thread(self) -> None:
        current_thread_id = _current_thread_id()
        with self._lock:
            owned_profile_ids = [
                profile_id
                for profile_id, session in self._active_sessions.items()
                if session.owner_thread_id == current_thread_id
            ]
        for profile_id in owned_profile_ids:
            self.close_browser(profile_id)

    def check_authorization(
        self,
        profile_id: str | None = None,
        *,
        html: str | None = None,
        current_url: str | None = None,
        persist_handle: bool = True,
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        state = self.current_state(profile.profile_id)

        if self._session_probe is None:
            state.last_checked_at = _now()
            with self._lock:
                self._states[profile.profile_id] = state
            return state

        if html is not None:
            snapshot = self._session_probe.inspect_document(html, current_url or "")
        else:
            session = None
            if self._refresh_native_browser_session(profile.profile_id) is not None:
                try:
                    session = self._attach_to_native_browser(profile.profile_id)
                except SessionError as exc:
                    state.health = SessionHealth.LOGIN_REQUIRED
                    state.authenticated = False
                    state.profile_in_use = False
                    state.native_login_running = True
                    state.manual_intervention = ManualInterventionRequest(
                        reason="native_login",
                        prompt="The login browser is still starting or not ready for automation attachment yet. Wait a moment and click Check Session again.",
                        requested_at=_now(),
                        rescue_url=state.current_url,
                    )
                    state.last_error = exc.message
                    state.reason_code = exc.code
                    state.diagnostics = {
                        "health": SessionHealth.LOGIN_REQUIRED.value,
                        "current_url": state.current_url or "",
                        "error": exc.message,
                    }
                    state.last_checked_at = _now()
                    self._write_native_login_diagnostics(
                        profile.profile_id, status="attach_failed", error=exc.message
                    )
                    with self._lock:
                        self._states[profile.profile_id] = state
                    return state
            else:
                session = self.open_browser(profile.profile_id)
            try:
                self._ensure_storyblocks_page(session)
                snapshot = self._session_probe.inspect_page(session.handle.page)
            finally:
                if not persist_handle:
                    self.close_browsers_owned_by_current_thread()

        transient_warning = snapshot.health == SessionHealth.UNKNOWN and any(
            token in snapshot.message.casefold()
            for token in ("still navigating", "non-storyblocks browser page")
        )
        if transient_warning:
            state.current_url = snapshot.current_url
            state.last_error = snapshot.message or None
            state.reason_code = "transient_navigation"
            state.diagnostics = {
                "health": snapshot.health.value,
                "current_url": snapshot.current_url or "",
                "message": snapshot.message,
            }
            state.last_checked_at = _now()
            with self._lock:
                self._states[profile.profile_id] = state
            return state

        self._profile_registry.update_session_health(
            profile.profile_id, snapshot.health
        )
        if snapshot.account is not None:
            self._profile_registry.update_storyblocks_account(
                profile.profile_id, snapshot.account
            )

        state.health = snapshot.health
        state.authenticated = snapshot.health == SessionHealth.READY
        state.storyblocks_account = snapshot.account
        state.current_url = snapshot.current_url
        state.profile_in_use = False
        state.manual_intervention = None
        if snapshot.health == SessionHealth.READY:
            state.manual_ready_override = False
            state.manual_ready_override_note = None
        else:
            state.manual_ready_override = False
            state.manual_ready_override_note = None
        state.last_error = (
            None
            if snapshot.health == SessionHealth.READY
            else (snapshot.message or None)
        )
        state.reason_code = _reason_code_for_health(snapshot.health)
        state.diagnostics = {
            "health": snapshot.health.value,
            "current_url": snapshot.current_url or "",
            "message": snapshot.message,
            "storyblocks_account": snapshot.account or "",
        }
        state.last_checked_at = _now()
        self._write_native_login_diagnostics(
            profile.profile_id,
            status="authorization_checked",
            error=state.last_error or "",
        )
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def require_manual_login(
        self,
        profile_id: str | None = None,
        *,
        paragraph_no: int | None = None,
        query: str | None = None,
        rescue_url: str | None = None,
    ) -> BrowserSessionState:
        return self._set_manual_intervention(
            profile_id,
            health=SessionHealth.LOGIN_REQUIRED,
            reason="login_required",
            prompt="Storyblocks login is required. Continue in the already opened persistent browser profile.",
            paragraph_no=paragraph_no,
            query=query,
            rescue_url=rescue_url,
        )

    def register_challenge(
        self,
        profile_id: str | None = None,
        *,
        paragraph_no: int | None = None,
        query: str | None = None,
        rescue_url: str | None = None,
    ) -> BrowserSessionState:
        return self._set_manual_intervention(
            profile_id,
            health=SessionHealth.CHALLENGE,
            reason="challenge_detected",
            prompt="Storyblocks challenge detected. Wait for the user to finish the manual verification in the persistent browser.",
            paragraph_no=paragraph_no,
            query=query,
            rescue_url=rescue_url,
        )

    def mark_blocked(
        self, profile_id: str | None = None, message: str = ""
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        self._profile_registry.update_session_health(
            profile.profile_id, SessionHealth.BLOCKED
        )
        state = self.current_state(profile.profile_id)
        state.health = SessionHealth.BLOCKED
        state.authenticated = False
        state.manual_ready_override = False
        state.manual_ready_override_note = None
        state.last_error = message or "Storyblocks blocked or denied access"
        state.reason_code = "access_blocked"
        state.diagnostics = {
            "health": SessionHealth.BLOCKED.value,
            "message": state.last_error or "",
            "current_url": state.current_url or "",
        }
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def confirm_manual_intervention(
        self,
        profile_id: str | None = None,
        *,
        resolved_health: SessionHealth = SessionHealth.READY,
        account: str | None = None,
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        self._profile_registry.update_session_health(
            profile.profile_id, resolved_health
        )
        if account is not None:
            self._profile_registry.update_storyblocks_account(
                profile.profile_id, account
            )

        state = self.current_state(profile.profile_id)
        state.health = resolved_health
        state.authenticated = resolved_health == SessionHealth.READY
        state.storyblocks_account = account or state.storyblocks_account
        state.manual_intervention = None
        state.manual_ready_override = False
        state.manual_ready_override_note = None
        state.last_error = None
        state.reason_code = _reason_code_for_health(resolved_health)
        state.diagnostics = {
            "health": resolved_health.value,
            "storyblocks_account": state.storyblocks_account or "",
            "current_url": state.current_url or "",
        }
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def reset_session_state(self, profile_id: str | None = None) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        self.close_browser(profile.profile_id)
        self.close_native_browser(profile.profile_id)
        self._profile_registry.update_storyblocks_account(profile.profile_id, None)
        self._profile_registry.update_session_health(
            profile.profile_id, SessionHealth.LOGIN_REQUIRED
        )
        state = self.current_state(profile.profile_id)
        state.health = SessionHealth.LOGIN_REQUIRED
        state.authenticated = False
        state.storyblocks_account = None
        state.current_url = None
        state.rescue_url = None
        state.manual_intervention = None
        state.manual_ready_override = False
        state.manual_ready_override_note = None
        state.last_error = "Session reset requested by operator. Log in again and re-check authorization."
        state.reason_code = "session_reset"
        state.diagnostics = {
            "health": SessionHealth.LOGIN_REQUIRED.value,
            "reset": "true",
        }
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def wait_for_user(
        self,
        profile_id: str | None = None,
        *,
        poll_interval_seconds: float = 1.0,
        max_checks: int = 10,
        inspector: Any | None = None,
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        session = self.open_browser(profile.profile_id)
        checker = inspector or (
            lambda: self._session_probe.inspect_page(session.handle.page)
            if self._session_probe
            else AuthorizationSnapshot(self.current_state(profile.profile_id).health)
        )

        for _ in range(max(1, max_checks)):
            snapshot = checker()
            if not isinstance(snapshot, AuthorizationSnapshot):
                raise SessionError(
                    code="invalid_manual_check",
                    message="Manual intervention inspector must return AuthorizationSnapshot.",
                )
            state = self.confirm_manual_intervention(
                profile.profile_id,
                resolved_health=snapshot.health,
                account=snapshot.account,
            )
            state.current_url = snapshot.current_url
            with self._lock:
                self._states[profile.profile_id] = state
            if snapshot.health == SessionHealth.READY:
                return state
            time.sleep(max(0.0, poll_interval_seconds))
        return self.current_state(profile.profile_id)

    def record_instability(self) -> float:
        return self._action_pacer.record_failure()

    def record_stable_action(self) -> float:
        return self._action_pacer.record_success()

    def restore_session(self, profile_id: str | None = None) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        return self.check_authorization(profile.profile_id, persist_handle=False)

    def open_rescue_url(
        self, url: str, profile_id: str | None = None
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        session = self.open_browser(profile.profile_id)
        page = session.handle.page
        if not hasattr(page, "goto"):
            raise SessionError(
                code="browser_page_unavailable",
                message="Persistent browser page does not support navigation.",
            )
        try:
            self._action_pacer.before_action()
            page.goto(url)
            self._action_pacer.record_success()
        except Exception:
            self._action_pacer.record_failure()
            raise
        state = self.current_state(profile.profile_id)
        state.rescue_url = url
        state.current_url = url
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def rescue_storyblocks_query(
        self,
        query: str,
        *,
        search_adapter: Any,
        profile_id: str | None = None,
        use_homepage: bool = False,
    ) -> BrowserSessionState:
        if search_adapter is None:
            raise SessionError(
                code="missing_search_adapter",
                message="Search adapter is required for rescue-flow navigation.",
            )
        url = (
            search_adapter.build_homepage_rescue_url(query)
            if use_homepage
            else search_adapter.build_direct_search_url(query)
        )
        return self.open_rescue_url(url, profile_id=profile_id)

    def _attach_to_native_browser(self, profile_id: str) -> PersistentBrowserSession:
        profile = self._resolve_profile(profile_id)
        native_session = self._refresh_native_browser_session(profile.profile_id)
        if native_session is None:
            raise SessionError(
                code="native_login_browser_missing",
                message="No login browser is currently open for Storyblocks session attachment.",
                details={"profile_id": profile.profile_id},
            )
        with self._lock:
            existing = self._active_sessions.get(profile.profile_id)
        if existing is not None and existing.kind == "native_debug_attach":
            if existing.owner_thread_id != _current_thread_id():
                raise SessionError(
                    code="browser_session_thread_mismatch",
                    message="The cached Storyblocks browser attachment belongs to another thread and cannot be reused safely.",
                    details={
                        "profile_id": profile.profile_id,
                        "owner_thread_id": existing.owner_thread_id,
                        "owner_thread_name": existing.owner_thread_name,
                        "current_thread_id": _current_thread_id(),
                    },
                )
            return existing

        profile_paths = self._profile_registry.paths_for(profile)
        plan = build_launch_plan(
            profile.profile_id,
            profile_paths,
            native_session.plan.browser_channel,
            self._settings,
            profile_directory_name=profile.launch_profile_dir_name
            or native_session.plan.profile_directory_name,
        )
        try:
            handle = self._context_factory.connect_over_cdp(
                plan, native_session.endpoint_url
            )
        except ConfigError:
            raise
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError(
                code="native_browser_attach_failed",
                message="Unable to connect to the browser window opened for Storyblocks login.",
                details={
                    "profile_id": profile.profile_id,
                    "endpoint_url": native_session.endpoint_url,
                    "cause": str(exc),
                },
            ) from exc

        preferred_page = self._select_storyblocks_page(
            handle,
            target_url=native_session.plan.target_url,
        )
        if preferred_page is not None:
            handle.page = preferred_page

        session = PersistentBrowserSession(
            plan=plan,
            handle=handle,
            kind="native_debug_attach",
            lock_acquired=False,
            debug_endpoint_url=native_session.endpoint_url,
        )
        with self._lock:
            self._active_sessions[profile.profile_id] = session

        state = self.current_state(profile.profile_id)
        state.browser_channel = native_session.plan.browser_channel
        state.browser_available = True
        state.profile_in_use = False
        state.native_login_running = True
        state.native_debug_port = native_session.plan.remote_debugging_port
        state.persistent_context_ready = True
        state.last_error = None
        state.last_checked_at = _now()
        self._write_native_login_diagnostics(profile.profile_id, status="attached")
        with self._lock:
            self._states[profile.profile_id] = state
        return session

    def _select_storyblocks_page(self, handle, *, target_url: str = ""):
        context = getattr(handle, "context", None)
        pages = list(getattr(context, "pages", []) or [])
        current_page = getattr(handle, "page", None)
        if current_page is not None and current_page not in pages:
            pages.append(current_page)
        return select_preferred_page(pages, target_url=target_url)

    def _ensure_storyblocks_page(self, session: PersistentBrowserSession) -> None:
        preferred_page = self._select_storyblocks_page(
            session.handle,
            target_url=session.plan.storyblocks_base_url,
        )
        if preferred_page is not None:
            session.handle.page = preferred_page
        current_url = str(getattr(session.handle.page, "url", ""))
        if "storyblocks.com" in current_url.casefold():
            return
        page = session.handle.page
        if not hasattr(page, "goto"):
            return
        fallback_url = f"{session.plan.storyblocks_base_url.rstrip('/')}/login"
        try:
            page.goto(fallback_url)
        except Exception:
            return

    def _set_manual_intervention(
        self,
        profile_id: str | None,
        *,
        health: SessionHealth,
        reason: str,
        prompt: str,
        paragraph_no: int | None,
        query: str | None,
        rescue_url: str | None,
    ) -> BrowserSessionState:
        profile = self._resolve_profile(profile_id)
        self._profile_registry.update_session_health(profile.profile_id, health)
        state = self.current_state(profile.profile_id)
        state.health = health
        state.authenticated = False
        state.manual_intervention = ManualInterventionRequest(
            reason=reason,
            prompt=prompt,
            requested_at=_now(),
            paragraph_no=paragraph_no,
            query=query,
            rescue_url=rescue_url,
        )
        state.manual_ready_override = False
        state.manual_ready_override_note = None
        state.rescue_url = rescue_url
        state.reason_code = reason
        state.diagnostics = {
            "health": health.value,
            "query": query or "",
            "rescue_url": rescue_url or "",
        }
        state.last_checked_at = _now()
        with self._lock:
            self._states[profile.profile_id] = state
        return state

    def _resolve_profile(self, profile_id: str | None, *, allow_missing: bool = False):
        if profile_id is not None:
            return self._profile_registry.get_profile(profile_id)
        profile = self._profile_registry.get_active()
        if profile is None and allow_missing:
            return None
        if profile is None:
            raise SessionError(
                code="active_profile_required",
                message="Select an active browser profile before opening Storyblocks automation.",
            )
        return profile

    def _refresh_native_browser_session(
        self, profile_id: str
    ) -> NativeBrowserSession | None:
        with self._lock:
            native_session = self._native_browser_sessions.get(profile_id)
        if native_session is None:
            return None
        if native_session.is_running():
            return native_session
        with self._lock:
            self._native_browser_sessions.pop(profile_id, None)
            attached_session = self._active_sessions.get(profile_id)
        if (
            attached_session is not None
            and attached_session.kind == "native_debug_attach"
        ):
            if attached_session.owner_thread_id == _current_thread_id():
                try:
                    attached_session.handle.close()
                finally:
                    with self._lock:
                        self._active_sessions.pop(profile_id, None)
            else:
                with self._lock:
                    self._active_sessions.pop(profile_id, None)
        with self._lock:
            state = self._states.get(profile_id)
        if state is not None:
            state.profile_in_use = False
            state.native_login_running = False
            state.native_debug_port = None
            if state.last_error is None:
                state.last_error = "Login browser closed. Click Check Session to validate the Storyblocks session."
            state.last_checked_at = _now()
            self._write_native_login_diagnostics(
                profile_id, status="native_browser_closed"
            )
        return None

    def _write_native_login_diagnostics(
        self, profile_id: str, *, status: str, error: str = ""
    ) -> None:
        profile = self._profile_registry.get_profile(profile_id)
        if profile is None:
            return
        paths = self._profile_registry.paths_for(profile)
        with self._lock:
            native_session = self._native_browser_sessions.get(profile_id)
            state = self._states.get(profile_id)
        payload = {
            "profile_id": profile_id,
            "status": status,
            "checked_at": _now().isoformat(),
            "browser_channel": native_session.plan.browser_channel
            if native_session is not None
            else "",
            "target_url": native_session.plan.target_url
            if native_session is not None
            else "",
            "remote_debugging_port": native_session.plan.remote_debugging_port
            if native_session is not None
            else None,
            "endpoint_url": native_session.endpoint_url
            if native_session is not None
            else "",
            "login_browser_running": native_session.is_running()
            if native_session is not None
            else False,
            "error": error,
            "reason_code": state.reason_code if state is not None else "",
            "health": state.health.value if state is not None else "",
            "current_url": state.current_url if state is not None else "",
            "diagnostics": dict(state.diagnostics) if state is not None else {},
        }
        write_json(paths.diagnostics_dir / "native_login.json", payload)
