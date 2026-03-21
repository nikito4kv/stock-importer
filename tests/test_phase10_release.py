from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from release_tools.portable import build_portable_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNTIME_DIRECTORIES = (
    "app",
    "browser",
    "config",
    "domain",
    "legacy_core",
    "pipeline",
    "providers",
    "services",
    "storage",
    "ui",
)
EXPECTED_ROOT_FILES = {
    "requirements.txt",
    "launch_gui.bat",
    "launch_smoke.bat",
    "setup_portable.bat",
    "setup_portable.ps1",
    "PORTABLE-README.txt",
    "portable_manifest.json",
}
EXPECTED_DOCUMENT_FILES = {"docs/phase-10/onboarding.md"}
EXCLUDED_BUNDLE_PATHS = {
    ".env.example",
    "implementation_plan.md",
    "docs/phase-10/release-checklist.md",
}


class Phase10ReleaseTests(unittest.TestCase):
    def test_portable_build_contains_expected_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_portable_bundle(
                PROJECT_ROOT, Path(temp_dir), version="test-build"
            )

            for directory_name in EXPECTED_RUNTIME_DIRECTORIES:
                self.assertTrue((result.bundle_dir / directory_name).exists())

            self.assertTrue((result.bundle_dir / "ui" / "qt_app.py").exists())

            for relative_path in EXPECTED_ROOT_FILES | EXPECTED_DOCUMENT_FILES:
                self.assertTrue((result.bundle_dir / relative_path).exists())

            self.assertTrue((result.bundle_dir / "workspace").exists())
            self.assertFalse((result.bundle_dir / ".venv").exists())
            self.assertFalse((result.bundle_dir / "__pycache__").exists())

            for relative_path in EXCLUDED_BUNDLE_PATHS:
                self.assertFalse((result.bundle_dir / relative_path).exists())

            bundled_docs = {
                path.relative_to(result.bundle_dir).as_posix()
                for path in (result.bundle_dir / "docs").rglob("*")
                if path.is_file()
            }
            self.assertEqual(bundled_docs, EXPECTED_DOCUMENT_FILES)

    def test_portable_manifest_and_archive_match_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_portable_bundle(
                PROJECT_ROOT, Path(temp_dir), version="archive-check"
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["version"], "archive-check")
            self.assertEqual(manifest["entrypoints"]["gui"], "launch_gui.bat")
            self.assertEqual(
                manifest["included_runtime_directories"],
                list(EXPECTED_RUNTIME_DIRECTORIES),
            )
            self.assertEqual(
                set(manifest["included_root_files"]),
                EXPECTED_ROOT_FILES,
            )
            self.assertEqual(
                set(manifest["included_document_files"]),
                EXPECTED_DOCUMENT_FILES,
            )
            self.assertEqual(manifest["workspace_dir"], "workspace")
            archive_path = result.archive_path
            self.assertIsNotNone(archive_path)
            assert archive_path is not None
            self.assertTrue(archive_path.exists())

            with ZipFile(archive_path) as archive:
                names = set(archive.namelist())

            prefix = f"{result.bundle_dir.name}/"
            bundled_files = {
                f"{prefix}{path.relative_to(result.bundle_dir).as_posix()}"
                for path in result.bundle_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(names, bundled_files)

            self.assertIn(f"{prefix}launch_gui.bat", names)
            self.assertIn(f"{prefix}portable_manifest.json", names)
            self.assertIn(f"{prefix}docs/phase-10/onboarding.md", names)

            for relative_path in EXCLUDED_BUNDLE_PATHS:
                self.assertNotIn(f"{prefix}{relative_path}", names)


if __name__ == "__main__":
    unittest.main()
