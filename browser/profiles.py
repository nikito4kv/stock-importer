from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from domain.enums import SessionHealth
from domain.models import BrowserProfile
from storage.repositories import BrowserProfileRepository


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sortable_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return value.timestamp()


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
    SINGLETON_PROFILE_ID = "storyblocks"
    SINGLETON_DISPLAY_NAME = "Storyblocks"

    def __init__(self, repository: BrowserProfileRepository):
        self._repository = repository

    def save_profile(self, profile: BrowserProfile) -> BrowserProfile:
        profile.updated_at = _now()
        return self._repository.save(profile)

    def get_singleton(self) -> BrowserProfile | None:
        profiles = self._repository.list_all()
        if not profiles:
            return None
        profile = self._select_singleton_profile(profiles)
        self.ensure_profile_structure(profile)
        return profile

    def get_or_create_singleton(
        self,
        *,
        display_name: str | None = None,
    ) -> BrowserProfile:
        existing = self.get_singleton()
        if existing is not None:
            return existing
        profile = BrowserProfile(
            profile_id=self.SINGLETON_PROFILE_ID,
            display_name=(display_name or self.SINGLETON_DISPLAY_NAME).strip()
            or self.SINGLETON_DISPLAY_NAME,
            storage_path=self._storage_root() / self.SINGLETON_PROFILE_ID,
            session_health=SessionHealth.UNKNOWN,
        )
        self.ensure_profile_structure(profile)
        return self.save_profile(profile)

    def ensure_profile_structure(self, profile: BrowserProfile) -> BrowserProfilePaths:
        paths = build_browser_profile_paths(profile.storage_path)
        for current in (
            paths.root,
            paths.user_data_dir,
            paths.downloads_dir,
            paths.diagnostics_dir,
        ):
            current.mkdir(parents=True, exist_ok=True)
        return paths

    def paths_for(self, profile_or_id: BrowserProfile | str) -> BrowserProfilePaths:
        profile = (
            profile_or_id
            if isinstance(profile_or_id, BrowserProfile)
            else self.get_profile(profile_or_id)
        )
        return self.ensure_profile_structure(profile)

    def get_profile(self, profile_id: str) -> BrowserProfile:
        profile = self._repository.load(profile_id)
        if profile is None:
            raise KeyError(profile_id)
        return profile

    def update_session_health(
        self, profile_id: str, health: SessionHealth
    ) -> BrowserProfile:
        profile = self.get_profile(profile_id)
        profile.session_health = health
        return self.save_profile(profile)

    def update_storyblocks_account(
        self, profile_id: str, account: str | None
    ) -> BrowserProfile:
        profile = self.get_profile(profile_id)
        profile.storyblocks_account = account
        return self.save_profile(profile)

    def _storage_root(self) -> Path:
        return self._repository.paths.browser_profiles_dir

    def _select_singleton_profile(
        self, profiles: list[BrowserProfile]
    ) -> BrowserProfile:
        preferred = next(
            (
                profile
                for profile in profiles
                if profile.profile_id == self.SINGLETON_PROFILE_ID
            ),
            None,
        )
        if preferred is not None:
            return preferred
        return max(profiles, key=self._singleton_sort_key)

    def _singleton_sort_key(self, profile: BrowserProfile) -> tuple[float, float, str]:
        return (
            _sortable_timestamp(profile.updated_at),
            _sortable_timestamp(profile.created_at),
            profile.profile_id,
        )
