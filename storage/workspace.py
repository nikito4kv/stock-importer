from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class WorkspacePaths:
    root: Path
    config_dir: Path
    projects_dir: Path
    runs_dir: Path
    presets_dir: Path
    cache_dir: Path
    browser_profiles_dir: Path
    logs_dir: Path
    secrets_dir: Path


def build_workspace_paths(root: str | Path) -> WorkspacePaths:
    root_path = Path(root)
    return WorkspacePaths(
        root=root_path,
        config_dir=root_path / "config",
        projects_dir=root_path / "projects",
        runs_dir=root_path / "runs",
        presets_dir=root_path / "presets",
        cache_dir=root_path / "cache",
        browser_profiles_dir=root_path / "browser_profiles",
        logs_dir=root_path / "logs",
        secrets_dir=root_path / "secrets",
    )


class WorkspaceStorage:
    def __init__(self, root: str | Path):
        self.paths = build_workspace_paths(root)

    def initialize(self) -> WorkspacePaths:
        for path in (
            self.paths.root,
            self.paths.config_dir,
            self.paths.projects_dir,
            self.paths.runs_dir,
            self.paths.presets_dir,
            self.paths.cache_dir,
            self.paths.browser_profiles_dir,
            self.paths.logs_dir,
            self.paths.secrets_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self.paths
