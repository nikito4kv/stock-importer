from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

INCLUDED_DIRS = (
    "app",
    "browser",
    "config",
    "docs",
    "domain",
    "legacy_core",
    "pipeline",
    "providers",
    "services",
    "storage",
    "ui",
)

INCLUDED_FILES = (
    "requirements.txt",
    ".env.example",
    "implementation_plan.md",
)

SKIP_DIR_NAMES = {
    ".git",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "output",
    "recordings",
    "workspace",
}

SKIP_SUFFIXES = {".pyc", ".pyo", ".tmp"}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


@dataclass(slots=True)
class PortableBuildResult:
    bundle_dir: Path
    archive_path: Path | None
    manifest_path: Path


def build_portable_bundle(
    project_root: str | Path,
    output_dir: str | Path,
    *,
    version: str | None = None,
    create_zip: bool = True,
) -> PortableBuildResult:
    root = Path(project_root).resolve()
    out_root = Path(output_dir).resolve()
    build_version = version or _utc_stamp()
    bundle_name = f"vid-img-downloader-portable-{build_version}"
    bundle_dir = out_root / bundle_name
    archive_path = out_root / f"{bundle_name}.zip"

    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    for directory_name in INCLUDED_DIRS:
        source = root / directory_name
        if not source.exists():
            continue
        shutil.copytree(source, bundle_dir / directory_name, ignore=_ignore_names)

    for file_name in INCLUDED_FILES:
        source = root / file_name
        if source.exists():
            shutil.copy2(source, bundle_dir / file_name)

    (bundle_dir / "workspace").mkdir(parents=True, exist_ok=True)
    _write_launchers(bundle_dir)
    manifest_path = _write_manifest(bundle_dir, build_version)

    if create_zip:
        if archive_path.exists():
            archive_path.unlink()
        _zip_bundle(bundle_dir, archive_path)
    else:
        archive_path = None

    return PortableBuildResult(
        bundle_dir=bundle_dir,
        archive_path=archive_path,
        manifest_path=manifest_path,
    )


def _ignore_names(directory: str, names: list[str]) -> set[str]:
    current_dir = Path(directory)
    ignored: set[str] = set()
    for name in names:
        path = current_dir / name
        if name in SKIP_DIR_NAMES:
            ignored.add(name)
            continue
        if path.is_dir() and name == "__pycache__":
            ignored.add(name)
            continue
        if path.is_file() and path.suffix.lower() in SKIP_SUFFIXES:
            ignored.add(name)
    return ignored


def _write_launchers(bundle_dir: Path) -> None:
    launch_gui = r"""@echo off
setlocal
set ROOT=%~dp0
set PYTHON=%ROOT%.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m app --workspace "%ROOT%workspace" %*
"""
    launch_smoke = r"""@echo off
setlocal
set ROOT=%~dp0
set PYTHON=%ROOT%.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python
"%PYTHON%" -m app --smoke --workspace "%ROOT%workspace" %*
"""
    setup_bat = r"""@echo off
setlocal
set ROOT=%~dp0
if not exist "%ROOT%.venv\Scripts\python.exe" (
  python -m venv "%ROOT%.venv"
)
"%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
"%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
echo Portable environment is ready.
echo Run launch_gui.bat to start the desktop app.
"""
    setup_ps1 = r"""$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $root '.venv')
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $root 'requirements.txt')
Write-Host 'Portable environment is ready.'
Write-Host 'Run launch_gui.bat to start the desktop app.'
"""
    readme = """Vid Img Downloader Portable Bundle

1. Run setup_portable.ps1 or setup_portable.bat.
2. Make sure Chrome or Edge is installed on the machine.
3. Run launch_gui.bat.
4. The app stores runtime data in the local workspace folder next to this file.

Full user docs live in docs/phase-10/.
"""
    (bundle_dir / "launch_gui.bat").write_text(launch_gui, encoding="utf-8", newline="\r\n")
    (bundle_dir / "launch_smoke.bat").write_text(launch_smoke, encoding="utf-8", newline="\r\n")
    (bundle_dir / "setup_portable.bat").write_text(setup_bat, encoding="utf-8", newline="\r\n")
    (bundle_dir / "setup_portable.ps1").write_text(setup_ps1, encoding="utf-8")
    (bundle_dir / "PORTABLE-README.txt").write_text(readme, encoding="utf-8")


def _write_manifest(bundle_dir: Path, version: str) -> Path:
    manifest = {
        "bundle_name": bundle_dir.name,
        "version": version,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "entrypoints": {
            "gui": "launch_gui.bat",
            "smoke": "launch_smoke.bat",
            "setup_bat": "setup_portable.bat",
            "setup_ps1": "setup_portable.ps1",
        },
        "included_directories": list(INCLUDED_DIRS),
        "included_files": list(INCLUDED_FILES),
        "workspace_dir": "workspace",
    }
    manifest_path = bundle_dir / "portable_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _zip_bundle(bundle_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, arcname=f"{bundle_dir.name}/{path.relative_to(bundle_dir)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a portable release bundle")
    parser.add_argument("--project-root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", default=Path("dist") / "portable")
    parser.add_argument("--version", default=None)
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    result = build_portable_bundle(
        args.project_root,
        args.output_dir,
        version=args.version,
        create_zip=not args.no_zip,
    )
    payload = {
        "bundle_dir": str(result.bundle_dir),
        "archive_path": str(result.archive_path) if result.archive_path else "",
        "manifest_path": str(result.manifest_path),
    }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
