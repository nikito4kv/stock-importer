from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Callable

from domain.enums import EventLevel, RunStage


logger = logging.getLogger(__name__)


def event_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class AppEvent:
    name: str
    level: EventLevel
    message: str
    created_at: datetime = field(default_factory=event_now)
    project_id: str | None = None
    run_id: str | None = None
    paragraph_no: int | None = None
    provider_name: str | None = None
    query: str | None = None
    stage: RunStage | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level.value,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
            "project_id": self.project_id,
            "run_id": self.run_id,
            "paragraph_no": self.paragraph_no,
            "provider_name": self.provider_name,
            "query": self.query,
            "stage": self.stage.value if self.stage else None,
            "payload": dict(self.payload),
        }


class EventBus:
    def __init__(self):
        self._listeners: list[Callable[[AppEvent], None]] = []

    def subscribe(self, listener: Callable[[AppEvent], None]) -> None:
        self._listeners.append(listener)

    def publish(self, event: AppEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("Event listener failed for '%s'", event.name)


class EventRecorder:
    def __init__(self):
        self.events: list[AppEvent] = []
        self._events_by_run: dict[str, list[AppEvent]] = {}
        self._latest_by_run: dict[str, AppEvent] = {}
        self._latest_by_run_paragraph: dict[str, dict[int, AppEvent]] = {}

    def __call__(self, event: AppEvent) -> None:
        self.events.append(event)
        if event.run_id is not None:
            self._events_by_run.setdefault(event.run_id, []).append(event)
            self._latest_by_run[event.run_id] = event
            if event.paragraph_no is not None:
                self._latest_by_run_paragraph.setdefault(event.run_id, {})[
                    event.paragraph_no
                ] = event

    def by_run(self, run_id: str | None) -> list[AppEvent]:
        if run_id is None:
            return list(self.events)
        return list(self._events_by_run.get(run_id, []))

    def latest_for_run(self, run_id: str | None) -> AppEvent | None:
        if run_id is None:
            return self.events[-1] if self.events else None
        return self._latest_by_run.get(run_id)

    def latest_by_paragraph_for_run(self, run_id: str | None) -> dict[int, AppEvent]:
        if run_id is None:
            latest: dict[int, AppEvent] = {}
            for event in self.events:
                if event.paragraph_no is not None:
                    latest[event.paragraph_no] = event
            return latest
        return dict(self._latest_by_run_paragraph.get(run_id, {}))
