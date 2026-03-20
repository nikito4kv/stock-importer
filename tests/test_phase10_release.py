from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from release_tools.portable import build_portable_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Phase10ReleaseTests(unittest.TestCase):
    def test_portable_build_contains_expected_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_portable_bundle(PROJECT_ROOT, Path(temp_dir), version="test-build")

            self.assertTrue((result.bundle_dir / "app").exists())
            self.assertTrue((result.bundle_dir / "ui").exists())
            self.assertTrue((result.bundle_dir / "ui" / "qt_app.py").exists())
            self.assertTrue((result.bundle_dir / "legacy_core").exists())
            self.assertTrue((result.bundle_dir / "docs" / "phase-10" / "onboarding.md").exists())
            self.assertTrue((result.bundle_dir / "launch_gui.bat").exists())
            self.assertTrue((result.bundle_dir / "launch_smoke.bat").exists())
            self.assertTrue((result.bundle_dir / "setup_portable.ps1").exists())
            self.assertTrue((result.bundle_dir / "portable_manifest.json").exists())
            self.assertTrue((result.bundle_dir / "workspace").exists())
            self.assertFalse((result.bundle_dir / ".venv").exists())
            self.assertFalse((result.bundle_dir / "__pycache__").exists())

    def test_portable_manifest_and_archive_match_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_portable_bundle(PROJECT_ROOT, Path(temp_dir), version="archive-check")
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["version"], "archive-check")
            self.assertEqual(manifest["entrypoints"]["gui"], "launch_gui.bat")
            self.assertIn("docs", manifest["included_directories"])
            self.assertTrue(result.archive_path.exists())

            with ZipFile(result.archive_path) as archive:
                names = set(archive.namelist())

            prefix = f"{result.bundle_dir.name}/"
            self.assertIn(f"{prefix}launch_gui.bat", names)
            self.assertIn(f"{prefix}portable_manifest.json", names)
            self.assertIn(f"{prefix}docs/phase-10/release-checklist.md", names)


if __name__ == "__main__":
    unittest.main()
