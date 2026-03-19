from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import image_fetcher
import video_fetcher
from legacy_core.files import build_run_dir, write_hashed_temp_file
from legacy_core.licenses import normalize_license_info
from legacy_core.query_utils import build_query_variants, parse_sources


class CoreUtilityTests(unittest.TestCase):
    def test_normalize_license_info(self) -> None:
        self.assertEqual(
            normalize_license_info("pexels", "ignored", None),
            ("pexels-license", True, False),
        )
        self.assertEqual(
            normalize_license_info("openverse", "CC0", None),
            ("CC0", True, False),
        )
        self.assertEqual(
            normalize_license_info("wikimedia", "CC BY 4.0", None),
            ("CC BY 4.0", True, True),
        )

    def test_query_variants_and_source_parsing(self) -> None:
        variants = build_query_variants(
            "river boat",
            "A slow river boat moving through morning mist.",
            short_suffixes=("photo", "realistic"),
        )

        self.assertIn("river boat photo", variants)
        self.assertEqual(
            parse_sources("bing,openverse,bing", default_sources=["wikimedia"]),
            ["bing", "openverse"],
        )
        self.assertEqual(parse_sources("", default_sources=["wikimedia"]), ["wikimedia"])

    def test_file_helpers_create_run_dir_and_hashed_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_id, run_dir = build_run_dir(root, "Phase 1 Run")
            digest, file_path = write_hashed_temp_file(run_dir, b"abc", ".bin")

            self.assertTrue(run_id.startswith("phase_1_run_"))
            self.assertTrue(run_dir.exists())
            self.assertEqual(file_path.read_bytes(), b"abc")
            self.assertTrue(file_path.name.startswith(digest))

    def test_video_and_image_fetchers_use_shared_keyword_payload_extraction(self) -> None:
        payload = {
            "items": [
                {
                    "paragraph_no": 2,
                    "original_index": 4,
                    "text": "Scene",
                    "keywords": ["river", "boat"],
                }
            ]
        }

        video_tasks = video_fetcher._extract_paragraph_tasks(payload)
        image_tasks = image_fetcher._extract_paragraph_tasks(payload)

        self.assertEqual(video_tasks[0].paragraph_no, 2)
        self.assertEqual(image_tasks[0].paragraph_no, 2)
        self.assertEqual(video_tasks[0].keywords, ["river", "boat"])
        self.assertEqual(image_tasks[0].keywords, ["river", "boat"])
