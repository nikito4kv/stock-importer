from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from release_tools.portable import build_portable_bundle


class PortableReleaseTests(unittest.TestCase):
    def test_portable_bundle_contains_current_pipeline_sources(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_portable_bundle(
                project_root,
                Path(temp_dir),
                version="test-sync",
                create_zip=False,
            )

            for relative_path in (
                Path("app") / "bootstrap.py",
                Path("browser") / "session.py",
                Path("legacy_core") / "image_providers.py",
                Path("pipeline") / "intents.py",
                Path("pipeline") / "media.py",
                Path("providers") / "images" / "clients.py",
                Path("providers") / "images" / "filtering.py",
                Path("providers") / "images" / "querying.py",
                Path("services") / "settings_manager.py",
                Path("services") / "secrets.py",
            ):
                source = (project_root / relative_path).read_text(encoding="utf-8")
                bundled = (result.bundle_dir / relative_path).read_text(
                    encoding="utf-8"
                )
                self.assertEqual(bundled, source)


if __name__ == "__main__":
    unittest.main()
