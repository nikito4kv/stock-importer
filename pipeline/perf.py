from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from domain.models import Run

PERF_METADATA_KEY = "performance_context"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_counter_key(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").strip()).strip("_")
    return normalized.casefold() or "unknown"


@dataclass(slots=True)
class PerformanceContext:
    context_id: str
    run_id: str
    project_id: str
    created_at: datetime = field(default_factory=_utc_now)
    counters: dict[str, int] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)
    _origin_monotonic: float = field(default_factory=perf_counter)

    def add_timing(self, key: str, elapsed_ms: int) -> None:
        metric_key = _normalize_counter_key(key)
        self.timings_ms[metric_key] = self.timings_ms.get(metric_key, 0) + max(
            0, int(elapsed_ms)
        )

    def increment(self, key: str, value: int = 1) -> None:
        metric_key = _normalize_counter_key(key)
        self.counters[metric_key] = self.counters.get(metric_key, 0) + int(value)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "created_at": self.created_at.isoformat(),
            "counters": dict(self.counters),
            "timings_ms": dict(self.timings_ms),
        }

    def summary_payload(self) -> dict[str, Any]:
        return {
            "perf_context_id": self.context_id,
            "perf_run_id": self.run_id,
            "perf_project_id": self.project_id,
            "perf_counters": dict(self.counters),
            "perf_timings_ms": dict(self.timings_ms),
        }


def create_performance_context(run_id: str, project_id: str) -> PerformanceContext:
    return PerformanceContext(
        context_id=uuid4().hex[:12],
        run_id=run_id,
        project_id=project_id,
    )


def context_from_metadata(
    payload: dict[str, Any] | None,
    *,
    run_id: str,
    project_id: str,
) -> PerformanceContext:
    payload = dict(payload or {})
    raw_created_at = str(payload.get("created_at") or "").strip()
    try:
        created_at = datetime.fromisoformat(raw_created_at)
    except ValueError:
        created_at = _utc_now()
    context_id = str(payload.get("context_id") or "").strip() or uuid4().hex[:12]
    counters = {
        _normalize_counter_key(key): int(value)
        for key, value in dict(payload.get("counters") or {}).items()
    }
    timings_ms = {
        _normalize_counter_key(key): int(value)
        for key, value in dict(payload.get("timings_ms") or {}).items()
    }
    return PerformanceContext(
        context_id=context_id,
        run_id=run_id,
        project_id=project_id,
        created_at=created_at,
        counters=counters,
        timings_ms=timings_ms,
    )


def ensure_run_performance_context(run: Run) -> PerformanceContext:
    payload = run.metadata.get(PERF_METADATA_KEY)
    if isinstance(payload, dict):
        context = context_from_metadata(
            payload,
            run_id=run.run_id,
            project_id=run.project_id,
        )
    else:
        context = create_performance_context(run.run_id, run.project_id)
    run.metadata[PERF_METADATA_KEY] = context.to_metadata()
    return context


def persist_run_performance_context(run: Run, context: PerformanceContext) -> None:
    run.metadata[PERF_METADATA_KEY] = context.to_metadata()


__all__ = [
    "PERF_METADATA_KEY",
    "PerformanceContext",
    "create_performance_context",
    "ensure_run_performance_context",
    "persist_run_performance_context",
]
