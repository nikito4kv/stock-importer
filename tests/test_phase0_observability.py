from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document

from app.runtime import DesktopApplication
from domain.enums import AssetKind, EventLevel, ProviderCapability, RunStatus
from domain.models import AssetCandidate
from pipeline import CallbackCandidateSearchBackend, MediaSelectionConfig
from services.events import AppEvent
from services.logging import JsonLineEventLogger, JsonLinePerfLogger


def _write_docx(directory: str, paragraphs: list[str]) -> Path:
    path = Path(directory) / "script.docx"
    doc = Document()
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    doc.save(str(path))
    return path


class Phase0ObservabilityTests(unittest.TestCase):
    def test_create_project_emits_import_timing_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            application = DesktopApplication.create(temp_dir)
            script_path = _write_docx(
                temp_dir,
                [
                    "HEADER",
                    "1. Spanish soldiers row a damaged boat through the jungle river.",
                ],
            )

            project = application.create_project("Demo", script_path)

            events = [
                event
                for event in application.container.event_recorder.events
                if event.name == "project.import.completed"
            ]
            self.assertTrue(events)
            payload = events[-1].payload
            self.assertEqual(project.project_id, events[-1].project_id)
            self.assertIn("ingestion_ms", payload)
            self.assertIn("intent_bootstrap_ms", payload)
            self.assertIn("project_save_ms", payload)
            self.assertIn("time_to_import_project_ms", payload)
            self.assertIn("perf_context_id", payload)
            self.assertIn("perf_run_id", payload)
            self.assertGreater(int(payload["ingestion_ms"]), 0)
            self.assertGreater(int(payload["intent_bootstrap_ms"]), 0)
            self.assertGreater(int(payload["project_save_ms"]), 0)
            self.assertGreater(int(payload["time_to_import_project_ms"]), 0)

    def test_run_perf_context_and_perf_log_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            application = DesktopApplication.create(temp_dir)
            script_path = _write_docx(
                temp_dir,
                [
                    "HEADER",
                    "1. Warriors row boats toward the shoreline at dawn.",
                ],
            )
            project = application.create_project("Demo", script_path)
            descriptor = application.container.provider_registry.get(
                "storyblocks_video"
            )
            assert descriptor is not None
            application.container.media_pipeline.register_backend(
                CallbackCandidateSearchBackend(
                    provider_id="storyblocks_video",
                    capability=ProviderCapability.VIDEO,
                    descriptor=descriptor,
                    search_fn=lambda paragraph, query, limit: [
                        AssetCandidate(
                            asset_id=f"video-{paragraph.paragraph_no}",
                            provider_name="storyblocks_video",
                            kind=AssetKind.VIDEO,
                            source_url=f"https://example.com/video-{paragraph.paragraph_no}.mp4",
                            license_name="test",
                            metadata={"rank_hint": 10.0, "search_query": query},
                        )
                    ],
                )
            )

            run, _manifest = application.container.media_run_service.create_and_execute(
                project.project_id,
                config=MediaSelectionConfig(
                    storyblocks_images_enabled=False,
                    free_images_enabled=False,
                ),
            )

            self.assertEqual(run.status, RunStatus.COMPLETED)
            reloaded_run = application.container.run_repository.load(run.run_id)
            assert reloaded_run is not None
            perf_context = reloaded_run.metadata.get("performance_context")
            self.assertIsInstance(perf_context, dict)
            assert isinstance(perf_context, dict)
            self.assertEqual(perf_context.get("run_id"), run.run_id)
            self.assertEqual(perf_context.get("project_id"), run.project_id)
            self.assertTrue(str(perf_context.get("context_id", "")).strip())

            run_events = application.container.event_recorder.by_run(run.run_id)
            self.assertTrue(any(event.name == "paragraph.perf" for event in run_events))
            perf_summary = next(
                event for event in run_events if event.name == "run.perf"
            )
            self.assertEqual(perf_summary.level, EventLevel.INFO)
            self.assertIn("perf_timings_ms", perf_summary.payload)
            self.assertIn("perf_counters", perf_summary.payload)

            perf_log_path = Path(temp_dir) / "logs" / "perf.jsonl"
            self.assertTrue(perf_log_path.exists())
            lines = [
                line.strip()
                for line in perf_log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(lines)
            record = json.loads(lines[-1])
            self.assertIn("timestamp", record)
            self.assertIn("run_id", record)
            self.assertTrue(str(record["run_id"]).strip())

    def test_failed_import_emits_timing_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            application = DesktopApplication.create(temp_dir)

            with self.assertRaises(FileNotFoundError):
                application.create_project("Broken", Path(temp_dir) / "missing.docx")

            events = [
                event
                for event in application.container.event_recorder.events
                if event.name == "project.import.failed"
            ]
            self.assertTrue(events)
            payload = events[-1].payload
            self.assertIn("ingestion_ms", payload)
            self.assertIn("intent_bootstrap_ms", payload)
            self.assertIn("project_save_ms", payload)
            self.assertIn("time_to_import_project_ms", payload)
            self.assertIn("failed_stage", payload)

    def test_buffered_event_logger_flushes_on_terminal_run_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "events.jsonl"
            logger = JsonLineEventLogger(
                log_path,
                max_buffer_items=32,
                max_buffer_bytes=256 * 1024,
            )
            logger.write(
                AppEvent(
                    name="paragraph.completed",
                    level=EventLevel.INFO,
                    message="Paragraph complete",
                    run_id="run-1",
                )
            )
            self.assertFalse(log_path.exists())

            logger.write(
                AppEvent(
                    name="run.completed",
                    level=EventLevel.INFO,
                    message="Run complete",
                    run_id="run-1",
                )
            )

            lines = [
                line.strip()
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(lines), 2)
            logger.close()

    def test_buffered_perf_logger_flushes_on_terminal_run_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "perf.jsonl"
            logger = JsonLinePerfLogger(
                log_path,
                max_buffer_items=32,
                max_buffer_bytes=256 * 1024,
            )
            logger.write(
                AppEvent(
                    name="paragraph.persisted",
                    level=EventLevel.INFO,
                    message="Persisted",
                    run_id="run-2",
                    payload={"download_ms": 11},
                )
            )
            self.assertFalse(log_path.exists())

            logger.write(
                AppEvent(
                    name="run.completed",
                    level=EventLevel.INFO,
                    message="Completed",
                    run_id="run-2",
                )
            )

            lines = [
                line.strip()
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["event"], "paragraph.persisted")
            logger.close()

    def test_buffered_perf_logger_rotates_by_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "perf.jsonl"
            logger = JsonLinePerfLogger(
                log_path,
                max_bytes=1024,
                max_buffer_items=1,
                max_buffer_bytes=64,
            )
            for index in range(24):
                logger.write(
                    AppEvent(
                        name="paragraph.perf",
                        level=EventLevel.INFO,
                        message=f"Perf {index}",
                        run_id="run-rotate",
                        payload={
                            "perf_context_id": "ctx-rotate",
                            "download_ms": index + 1,
                            "paragraph_total_ms": (index + 1) * 10,
                        },
                    )
                )
            logger.close()

            rotated_path = Path(temp_dir) / "perf.jsonl.1"
            self.assertTrue(log_path.exists())
            self.assertTrue(rotated_path.exists())

            for path in (rotated_path, log_path):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    self.assertIn("timestamp", record)
                    self.assertEqual(record["run_id"], "run-rotate")


if __name__ == "__main__":
    unittest.main()
