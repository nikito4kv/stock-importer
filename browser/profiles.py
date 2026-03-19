from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
from uuid import uuid4

from domain.enums import SessionHealth
from domain.models import BrowserProfile
from storage.repositories import BrowserProfileRepository


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class BrowserProfilePaths:
    root: Path
    user_data_dir: Path
    downloads_dir: Path
    diagnostics_dir: Path
    automation_lock_path: Path


def build_browser_profile_paths(root: str | Path) -> BrowserProfilePaths:
    root_path = Path(root)
    return BrowserProfilePaths(
        root=root_path,
        user_data_dir=root_path / "user_data",
        downloads_dir=root_path / "downloads",
        diagnostics_dir=root_path / "diagnostics",
        automation_lock_path=root_path / ".automation.lock",
    )


class BrowserProfileRegistry:
    def __init__(self, repository: BrowserProfileRepository):
        self._repository = repository

    def list_profiles(self) -> list[BrowserProfile]:
        return self._repository.list_all()

    def save_profile(self, profile: BrowserProfile) -> BrowserProfile:
        profile.updated_at = _now()
        return self._repository.save(profile)

    def create_profile(self, name: str, storage_root: Path) -> BrowserProfile:
        profile_id = uuid4().hex[:12]
        profile = BrowserProfile(
            profile_id=profile_id,
            display_name=name,
            storage_path=storage_root / profile_id,
            session_health=SessionHealth.UNKNOWN,
        )
        self.ensure_profile_structure(profile)
        return self.save_profile(profile)

    def ensure_profile_structure(self, profile: BrowserProfile) -> BrowserProfilePaths:
        paths = build_browser_profile_paths(profile.storage_path)
        for current in (paths.root, paths.user_data_dir, paths.downloads_dir, paths.diagnostics_dir):
            current.mkdir(parents=True, exist_ok=True)
        return paths

    def paths_for(self, profile_or_id: BrowserProfile | str) -> BrowserProfilePaths:
        profile = profile_or_id if isinstance(profile_or_id, BrowserProfile) else self.get_profile(profile_or_id)
        return self.ensure_profile_structure(profile)

    def rename_profile(self, profile_id: str, display_name: str) -> BrowserProfile:
        profile = self.get_profile(profile_id)
        profile.display_name = display_name
        return self.save_profile(profile)

    def delete_profile(self, profile_id: str) -> None:
        try:
            stored = self.get_profile(profile_id)
        except KeyError:
            stored = None

        profile_path = self._repository.path_for(profile_id)
        if profile_path.exists():
            profile_path.unlink()
        if stored is not None and stored.storage_path.exists():
            shutil.rmtree(stored.storage_path, ignore_errors=True)

    def set_active(self, profile_id: str) -> BrowserProfile:
        profiles = self.list_profiles()
        active: BrowserProfile | None = None
        for profile in profiles:
            profile.is_active = profile.profile_id == profile_id
            profile.updated_at = _now()
            self._repository.save(profile)
            if profile.is_active:
                active = profile
        if active is None:
            raise KeyError(profile_id)
        return active

    def select_profile(self, profile_id: str) -> BrowserProfile:
        return self.set_active(profile_id)

    def get_active(self) -> BrowserProfile | None:
        for profile in self.list_profiles():
            if profile.is_active:
                return profile
        return None

    def get_profile(self, profile_id: str) -> BrowserProfile:
        profile = self._repository.load(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        return profile

    def update_session_health(self, profile_id: str, health: SessionHealth) -> BrowserProfile:
        profile = self.get_profile(profile_id)
        profile.session_health = health
        return self.save_profile(profile)

    def update_storyblocks_account(self, profile_id: str, account: str | None) -> BrowserProfile:
        profile = self.get_profile(profile_id)
        profile.storyblocks_account = account
        return self.save_profile(profile)
