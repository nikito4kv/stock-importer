from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from legacy_core.relevance import ImageRelevanceCache, VideoRelevanceCache, parse_relevance_response


class RelevanceCacheTests(unittest.TestCase):
    def test_parse_relevance_response_handles_fenced_json(self) -> None:
        parsed = parse_relevance_response(
            "```json\n{\"match\": true, \"score\": 0.9, \"reason\": \"good\"}\n```"
        )

        self.assertEqual(parsed, (True, 0.9, "good"))

    def test_image_relevance_cache_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.sqlite"
            cache = ImageRelevanceCache(path)
            try:
                cache.set("river", "hash1", "model", True, 0.8, "ok")
                self.assertEqual(cache.get("river", "hash1", "model"), (True, 0.8, "ok"))
            finally:
                cache.close()

    def test_video_relevance_cache_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.sqlite"
            cache = VideoRelevanceCache(path)
            try:
                cache.set("river", "hash2", "model", 8, "uniform-v1", False, 0.2, "bad")
                self.assertEqual(
                    cache.get("river", "hash2", "model", 8, "uniform-v1"),
                    (False, 0.2, "bad"),
                )
            finally:
                cache.close()
