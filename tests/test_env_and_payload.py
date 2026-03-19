from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from legacy_core.env import load_dotenv
from legacy_core.keyword_payload import extract_paragraph_tasks


class EnvAndPayloadTests(unittest.TestCase):
    def test_load_dotenv_does_not_create_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            returned = load_dotenv(env_path)

            self.assertEqual(returned, env_path)
            self.assertFalse(env_path.exists())

    def test_load_dotenv_loads_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text('TEST_PHASE1_KEY="value"\n', encoding="utf-8")

            os.environ.pop("TEST_PHASE1_KEY", None)
            load_dotenv(env_path)

            self.assertEqual(os.environ.get("TEST_PHASE1_KEY"), "value")

    def test_extract_paragraph_tasks_from_items(self) -> None:
        payload = {
            "items": [
                {
                    "paragraph_no": 3,
                    "original_index": 7,
                    "text": "Scene",
                    "keywords": [" river ", "river", "boat"],
                }
            ]
        }

        tasks = extract_paragraph_tasks(payload)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].paragraph_no, 3)
        self.assertEqual(tasks[0].original_index, 7)
        self.assertEqual(tasks[0].keywords, ["river", "boat"])

    def test_extract_paragraph_tasks_from_keywords_map(self) -> None:
        payload = {"keywords_by_paragraph": {"2": ["mist"], "1": ["jungle"]}}

        tasks = extract_paragraph_tasks(payload)

        self.assertEqual([task.paragraph_no for task in tasks], [1, 2])
        self.assertEqual([task.keywords for task in tasks], [["jungle"], ["mist"]])

    def test_extract_paragraph_tasks_from_intent_contract(self) -> None:
        payload = {
            "items": [
                {
                    "paragraph_no": 4,
                    "original_index": 9,
                    "text": "Scene",
                    "intent": {
                        "primary_video_queries": ["river boat crew"],
                        "image_queries": ["river boat crew on jungle river"],
                    },
                    "query_bundle": {
                        "video_queries": ["river boat crew"],
                        "image_queries": ["river boat crew jungle river photo"],
                        "provider_queries": {
                            "storyblocks_video": ["river boat crew"],
                            "storyblocks_image": ["river boat crew on jungle river"],
                            "free_image": ["river boat crew jungle river photo"],
                        },
                    },
                }
            ]
        }

        video_tasks = extract_paragraph_tasks(payload, query_kind="video")
        image_tasks = extract_paragraph_tasks(payload, query_kind="image")

        self.assertEqual(video_tasks[0].keywords, ["river boat crew"])
        self.assertEqual(image_tasks[0].keywords, ["river boat crew jungle river photo"])
