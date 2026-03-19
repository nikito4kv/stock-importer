from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import subprocess
from typing import Any, Protocol

from services.errors import ConfigError, SessionError


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class NativeBrowserLaunchPlan:
    profile_id: str
    browser_channel: str
    executable_path: Path
    user_data_dir: Path
    profile_directory_name: str
    remote_debugging_port: int
    target_url: str


@dataclass(slots=True)
class NativeBrowserSession:
    plan: NativeBrowserLaunchPlan
    process: Any
    opened_at: datetime = field(default_factory=_now)

    @property
    def endpoint_url(self) -> str:
        return f"http://127.0.0.1:{self.plan.remote_debugging_port}"

    def is_running(self) -> bool:
        poll = getattr(self.process, "poll", None)
        if callable(poll):
            return poll() is None
        return True

    def terminate(self) -> None:
        terminate = getattr(self.process, "terminate", None)
        if callable(terminate):
            terminate()


class NativeBrowserLauncher(Protocol):
    def launch(self, plan: NativeBrowserLaunchPlan) -> NativeBrowserSession: ...


class SubprocessNativeBrowserLauncher:
    def launch(self, plan: NativeBrowserLaunchPlan) -> NativeBrowserSession:
        if not plan.executable_path.exists():
            raise ConfigError(
                code="browser_executable_missing",
                message="Unable to launch the browser for native Storyblocks login because the executable was not found.",
                details={"browser_channel": plan.browser_channel, "path": str(plan.executable_path)},
            )
        command = [
            str(plan.executable_path),
            f"--user-data-dir={plan.user_data_dir}",
            f"--profile-directory={plan.profile_directory_name}",
            f"--remote-debugging-port={plan.remote_debugging_port}",
            "--remote-debugging-address=127.0.0.1",
            "--new-window",
            "--no-first-run",
            plan.target_url,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            raise SessionError(
                code="native_browser_launch_failed",
                message=f"Failed to open {plan.browser_channel} for manual Storyblocks login.",
                details={"browser_channel": plan.browser_channel, "profile_id": plan.profile_id},
            ) from exc
        return NativeBrowserSession(plan=plan, process=process)


def find_available_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])
