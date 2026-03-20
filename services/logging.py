from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .events import AppEvent


class _BufferedJsonLineWriter:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int,
        max_buffer_items: int,
        max_buffer_bytes: int,
    ):
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._max_buffer_items = max(1, int(max_buffer_items))
        self._max_buffer_bytes = max(1024, int(max_buffer_bytes))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._buffer_bytes = 0

    def append_line(self, line: str, *, force_flush: bool = False) -> None:
        encoded_size = len(line.encode("utf-8")) + 1
        with self._lock:
            self._buffer.append(line)
            self._buffer_bytes += encoded_size
            should_flush = (
                force_flush
                or len(self._buffer) >= self._max_buffer_items
                or self._buffer_bytes >= self._max_buffer_bytes
            )
            if should_flush:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        self.flush()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        payload = "".join(f"{line}\n" for line in self._buffer)
        payload_bytes = len(payload.encode("utf-8"))
        self._rotate_if_needed_locked(incoming_bytes=payload_bytes)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
        except FileNotFoundError:
            # Temporary test workspaces may disappear before atexit flush.
            self._buffer.clear()
            self._buffer_bytes = 0
            return
        self._buffer.clear()
        self._buffer_bytes = 0

    def _rotate_if_needed_locked(self, *, incoming_bytes: int = 0) -> None:
        try:
            if not self._path.exists():
                return
            if self._path.stat().st_size + max(0, incoming_bytes) < self._max_bytes:
                return
        except FileNotFoundError:
            return
        rotated = self._path.with_suffix(f"{self._path.suffix}.1")
        try:
            if rotated.exists():
                rotated.unlink()
            self._path.rename(rotated)
        except FileNotFoundError:
            return


class JsonLineEventLogger:
    _TERMINAL_EVENTS = {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.paused",
    }

    def __init__(
        self,
        log_path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        max_buffer_items: int = 64,
        max_buffer_bytes: int = 256 * 1024,
    ):
        self._writer = _BufferedJsonLineWriter(
            log_path,
            max_bytes=max_bytes,
            max_buffer_items=max_buffer_items,
            max_buffer_bytes=max_buffer_bytes,
        )

    def write(self, event: AppEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        force_flush = (
            event.name in self._TERMINAL_EVENTS or event.name.endswith(".perf")
        )
        self._writer.append_line(line, force_flush=force_flush)

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()


class JsonLinePerfLogger:
    _TERMINAL_EVENTS = {
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.paused",
    }

    def __init__(
        self,
        log_path: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        max_buffer_items: int = 48,
        max_buffer_bytes: int = 192 * 1024,
    ):
        self._writer = _BufferedJsonLineWriter(
            log_path,
            max_bytes=max_bytes,
            max_buffer_items=max_buffer_items,
            max_buffer_bytes=max_buffer_bytes,
        )

    def write(self, event: AppEvent) -> None:
        metrics = self._extract_metrics(event.payload)
        should_write = bool(metrics) or event.name.endswith(".perf")
        force_flush = (
            event.name in self._TERMINAL_EVENTS or event.name.endswith(".perf")
        )
        if not should_write:
            if force_flush:
                self.flush()
            return
        run_id = self._run_id_for_event(event)
        record: dict[str, Any] = {
            "timestamp": event.created_at.isoformat(),
            "run_id": run_id,
            "project_id": event.project_id or "",
            "event": event.name,
            "stage": event.stage.value if event.stage is not None else "",
            "perf_context_id": str(event.payload.get("perf_context_id", "")),
            "metrics": metrics,
        }
        line = json.dumps(record, ensure_ascii=False)
        self._writer.append_line(line, force_flush=force_flush)

    def flush(self) -> None:
        self._writer.flush()

    def close(self) -> None:
        self._writer.close()

    def _run_id_for_event(self, event: AppEvent) -> str:
        if event.run_id:
            return event.run_id
        payload_run_id = str(event.payload.get("perf_run_id", "")).strip()
        if payload_run_id:
            return payload_run_id
        perf_context_id = str(event.payload.get("perf_context_id", "")).strip()
        if perf_context_id:
            return perf_context_id
        return str(event.project_id or "")

    def _extract_metrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"perf_counters", "perf_timings_ms"} and isinstance(value, dict):
                metrics[key] = dict(value)
                continue
            if not self._looks_like_metric_key(key):
                continue
            metrics[key] = value
        return metrics

    def _looks_like_metric_key(self, key: str) -> bool:
        return bool(re.search(r"(?:_ms|_p50|_p95|_total|_count|^candidates_)", key))
