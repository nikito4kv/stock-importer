from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile

from domain.models import BrowserProfile
from domain.enums import SessionHealth
from services.errors import PersistenceError, SessionError
from storage.serialization import write_json

from .automation import BrowserProfileLockProbe
from .profiles import BrowserProfileRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ImportableBrowserSession:
    browser_name: str
    browser_label: str
    user_data_root: Path
    profile_dir: Path
    profile_dir_name: str
    profile_label: str
    locked: bool = False

    @property
    def display_label(self) -> str:
        suffix = " (close browser before import)" if self.locked else ""
        return f"{self.browser_label}: {self.profile_label} [{self.profile_dir_name}]{suffix}"


class ChromiumProfileImportService:
    SUPPORTED_BROWSERS = ("chrome", "msedge")
    COPY_ROOT_FILES = ("Local State", "Last Version", "First Run")
    PROFILE_DIR_PREFIXES = ("Profile ",)
    IGNORED_DIRECTORY_NAMES = {
        "Cache",
        "Code Cache",
        "Crashpad",
        "Crash Reports",
        "DawnCache",
        "GPUCache",
        "GrShaderCache",
        "Media Cache",
        "ShaderCache",
    }
    IGNORED_FILE_SUFFIXES = (".tmp", ".temp", ".crdownload")

    def __init__(
        self,
        profile_registry: BrowserProfileRegistry,
        *,
        lock_probe: BrowserProfileLockProbe | None = None,
        env: dict[str, str] | None = None,
        explicit_user_data_roots: dict[str, list[Path]] | None = None,
    ):
        self._profile_registry = profile_registry
        self._lock_probe = lock_probe or BrowserProfileLockProbe()
        self._env = dict(env or os.environ)
        self._explicit_user_data_roots = explicit_user_data_roots or {}

    def discover_profiles(self, browser_name: str | None = None) -> list[ImportableBrowserSession]:
        browser_names = [browser_name] if browser_name else list(self.SUPPORTED_BROWSERS)
        sessions: list[ImportableBrowserSession] = []
        for current_browser in browser_names:
            for root in self._candidate_user_data_roots(current_browser):
                if not root.exists() or not root.is_dir():
                    continue
                locked = self._is_external_root_locked(root)
                for profile_dir in self._profile_dirs(root):
                    sessions.append(
                        ImportableBrowserSession(
                            browser_name=current_browser,
                            browser_label=self._browser_label(current_browser),
                            user_data_root=root,
                            profile_dir=profile_dir,
                            profile_dir_name=profile_dir.name,
                            profile_label=self._profile_label(profile_dir),
                            locked=locked,
                        )
                    )
        return sorted(sessions, key=lambda item: (item.browser_label, item.profile_label.casefold(), item.profile_dir_name.casefold()))

    def resolve_source(self, source_path: str | Path, *, browser_name: str | None = None) -> ImportableBrowserSession:
        path = Path(source_path).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise SessionError(
                code="external_profile_missing",
                message="The selected browser profile folder does not exist.",
                details={"source_path": str(path)},
            )
        if self._looks_like_profile_dir(path):
            profile_dir = path
            user_data_root = path.parent
        else:
            profile_dirs = self._profile_dirs(path)
            if len(profile_dirs) == 1:
                profile_dir = profile_dirs[0]
                user_data_root = path
            elif (path / "Default").exists():
                profile_dir = path / "Default"
                user_data_root = path
            else:
                raise SessionError(
                    code="external_profile_ambiguous",
                    message="Select a concrete Chrome or Edge profile directory such as 'Default' or 'Profile 1'.",
                    details={"source_path": str(path)},
                )
        detected_browser = browser_name or self._infer_browser_name(user_data_root)
        return ImportableBrowserSession(
            browser_name=detected_browser,
            browser_label=self._browser_label(detected_browser),
            user_data_root=user_data_root,
            profile_dir=profile_dir,
            profile_dir_name=profile_dir.name,
            profile_label=self._profile_label(profile_dir),
            locked=self._is_external_root_locked(user_data_root),
        )

    def import_profile(self, source: ImportableBrowserSession, target_profile_id: str | None = None) -> BrowserProfile:
        target_profile = self._resolve_target_profile(target_profile_id)
        target_paths = self._profile_registry.paths_for(target_profile)
        if self._lock_probe.is_profile_in_use(target_paths):
            raise SessionError(
                code="managed_browser_profile_in_use",
                message="Close the app-managed Storyblocks browser before importing a new session.",
                details={"profile_id": target_profile.profile_id},
            )
        if source.locked:
            raise SessionError(
                code="external_browser_profile_in_use",
                message=f"Close {source.browser_label} before importing its session.",
                details={"source_path": str(source.user_data_root)},
            )
        if not (source.user_data_root / "Local State").exists():
            raise SessionError(
                code="external_profile_invalid",
                message="The selected browser profile is missing Chromium state files. Select a Chrome or Edge profile folder.",
                details={"source_path": str(source.profile_dir)},
            )

        staged_root = Path(tempfile.mkdtemp(prefix="profile-import-", dir=str(target_paths.root)))
        staged_user_data = staged_root / "user_data"
        staged_user_data.mkdir(parents=True, exist_ok=True)
        try:
            self._copy_source_profile(source, staged_user_data)
            self._patch_local_state(staged_user_data / "Local State", source.profile_dir_name)
            self._swap_user_data_dir(target_paths.user_data_dir, staged_user_data)
            target_profile.launch_profile_dir_name = source.profile_dir_name or "Default"
            target_profile.import_source_browser = source.browser_name
            target_profile.import_source_root = source.user_data_root
            target_profile.import_source_profile_dir = source.profile_dir
            target_profile.import_source_profile_dir_name = source.profile_dir_name
            target_profile.import_source_profile_name = source.profile_label
            target_profile.imported_at = _now()
            target_profile.last_import_error = ""
            target_profile.storyblocks_account = None
            target_profile.session_health = SessionHealth.UNKNOWN
            imported = self._profile_registry.save_profile(target_profile)
            self._write_import_diagnostics(imported)
            return imported
        except Exception as exc:
            target_profile.last_import_error = str(exc)
            self._profile_registry.save_profile(target_profile)
            if isinstance(exc, SessionError):
                raise
            raise PersistenceError(
                code="browser_profile_import_failed",
                message="Failed to import the external browser profile into the app-managed Storyblocks profile.",
                details={"source_path": str(source.profile_dir), "target_profile_id": target_profile.profile_id},
            ) from exc
        finally:
            if staged_root.exists():
                shutil.rmtree(staged_root, ignore_errors=True)

    def reimport_profile(self, target_profile_id: str | None = None) -> BrowserProfile:
        target_profile = self._resolve_target_profile(target_profile_id)
        if target_profile.import_source_profile_dir is None:
            raise SessionError(
                code="no_import_source",
                message="This managed profile has no imported browser source yet.",
                details={"profile_id": target_profile.profile_id},
            )
        source = self.resolve_source(
            target_profile.import_source_profile_dir,
            browser_name=target_profile.import_source_browser or None,
        )
        return self.import_profile(source, target_profile.profile_id)

    def _resolve_target_profile(self, target_profile_id: str | None) -> BrowserProfile:
        profile = self._profile_registry.get_profile(target_profile_id) if target_profile_id else self._profile_registry.get_active()
        if profile is None:
            raise SessionError(
                code="managed_profile_missing",
                message="Create or select an app-managed Storyblocks profile before importing a session.",
            )
        return profile

    def _copy_source_profile(self, source: ImportableBrowserSession, target_user_data_root: Path) -> None:
        for name in self.COPY_ROOT_FILES:
            source_path = source.user_data_root / name
            if not source_path.exists():
                continue
            destination = target_user_data_root / name
            if source_path.is_file():
                shutil.copy2(source_path, destination)
            elif source_path.is_dir():
                shutil.copytree(source_path, destination, ignore=self._ignore_copy)
        shutil.copytree(source.profile_dir, target_user_data_root / source.profile_dir_name, ignore=self._ignore_copy)

    def _patch_local_state(self, local_state_path: Path, profile_dir_name: str) -> None:
        if not local_state_path.exists():
            return
        try:
            payload = json.loads(local_state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        profile_block = payload.setdefault("profile", {})
        profile_block["last_used"] = profile_dir_name
        profile_block["last_active_profiles"] = [profile_dir_name]
        payload["profile"] = profile_block
        write_json(local_state_path, payload)

    def _swap_user_data_dir(self, destination: Path, staged_user_data: Path) -> None:
        backup = destination.parent / "user_data.previous"
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        if destination.exists():
            destination.rename(backup)
        try:
            shutil.move(str(staged_user_data), str(destination))
        except Exception:
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if backup.exists():
                backup.rename(destination)
            raise
        else:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)

    def _write_import_diagnostics(self, profile: BrowserProfile) -> None:
        paths = self._profile_registry.paths_for(profile)
        write_json(
            paths.diagnostics_dir / "imported_session.json",
            {
                "profile_id": profile.profile_id,
                "display_name": profile.display_name,
                "import_source_browser": profile.import_source_browser,
                "import_source_root": str(profile.import_source_root) if profile.import_source_root is not None else "",
                "import_source_profile_dir": str(profile.import_source_profile_dir) if profile.import_source_profile_dir is not None else "",
                "import_source_profile_dir_name": profile.import_source_profile_dir_name,
                "import_source_profile_name": profile.import_source_profile_name,
                "launch_profile_dir_name": profile.launch_profile_dir_name,
                "imported_at": profile.imported_at.isoformat() if profile.imported_at is not None else "",
            },
        )

    def _candidate_user_data_roots(self, browser_name: str) -> list[Path]:
        if browser_name in self._explicit_user_data_roots:
            return [Path(path) for path in self._explicit_user_data_roots[browser_name]]
        local_app_data = Path(self._env.get("LOCALAPPDATA", ""))
        if browser_name == "chrome":
            return [local_app_data / "Google/Chrome/User Data"]
        if browser_name == "msedge":
            return [local_app_data / "Microsoft/Edge/User Data"]
        return []

    def _profile_dirs(self, user_data_root: Path) -> list[Path]:
        profile_dirs: list[Path] = []
        default_dir = user_data_root / "Default"
        if self._looks_like_profile_dir(default_dir):
            profile_dirs.append(default_dir)
        for child in sorted(user_data_root.iterdir() if user_data_root.exists() else []):
            if not child.is_dir():
                continue
            if not any(child.name.startswith(prefix) for prefix in self.PROFILE_DIR_PREFIXES):
                continue
            if self._looks_like_profile_dir(child):
                profile_dirs.append(child)
        return profile_dirs

    def _looks_like_profile_dir(self, path: Path) -> bool:
        return path.exists() and path.is_dir() and any(
            candidate.exists()
            for candidate in (
                path / "Preferences",
                path / "Cookies",
                path / "Network" / "Cookies",
                path / "Local Storage",
            )
        )

    def _is_external_root_locked(self, user_data_root: Path) -> bool:
        for name in self._lock_probe.KNOWN_BROWSER_LOCKS:
            if (user_data_root / name).exists():
                return True
        return False

    def _profile_label(self, profile_dir: Path) -> str:
        preferences_path = profile_dir / "Preferences"
        try:
            payload = json.loads(preferences_path.read_text(encoding="utf-8"))
        except Exception:
            return profile_dir.name
        profile_block = payload.get("profile", {})
        label = str(profile_block.get("name", "")).strip()
        return label or profile_dir.name

    def _browser_label(self, browser_name: str) -> str:
        if browser_name == "chrome":
            return "Google Chrome"
        if browser_name == "msedge":
            return "Microsoft Edge"
        return "Chromium"

    def _infer_browser_name(self, user_data_root: Path) -> str:
        current = str(user_data_root).casefold()
        if "microsoft" in current and "edge" in current:
            return "msedge"
        if "google" in current and "chrome" in current:
            return "chrome"
        return "chrome"

    def _ignore_copy(self, directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name in self.IGNORED_DIRECTORY_NAMES:
                ignored.add(name)
                continue
            if any(name.endswith(suffix) for suffix in self.IGNORED_FILE_SUFFIXES):
                ignored.add(name)
        return ignored
