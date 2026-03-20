from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable
from uuid import uuid4

from domain.enums import EventLevel, RunStage, RunStatus
from domain.models import AssetSelection, ParagraphUnit, Run, RunCheckpoint
from services.events import AppEvent, EventBus
from storage.repositories import RunRepository

from .backpressure import BoundedExecutor
from .perf import PerformanceContext, persist_run_performance_context


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(started_at: float) -> int:
    return int(round((perf_counter() - started_at) * 1000.0))


ParagraphProcessor = Callable[[ParagraphUnit], AssetSelection]


@dataclass(slots=True)
class RunControls:
    pause_after_current: bool = False
    cancel_requested: bool = False


class RunOrchestrator:
    def __init__(
        self,
        run_repository: RunRepository,
        event_bus: EventBus,
        *,
        max_workers: int = 1,
        queue_size: int = 4,
    ):
        self._run_repository = run_repository
        self._event_bus = event_bus
        self._max_workers = max_workers
        self._queue_size = queue_size
        self._controls: dict[str, RunControls] = {}

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def queue_size(self) -> int:
        return self._queue_size

    def configure(self, *, max_workers: int, queue_size: int) -> None:
        self._max_workers = max(1, int(max_workers))
        self._queue_size = max(1, int(queue_size))

    def create_run(
        self, project_id: str, selected_paragraphs: list[int] | None = None
    ) -> Run:
        run_id = uuid4().hex[:12]
        run = Run(
            run_id=run_id,
            project_id=project_id,
            status=RunStatus.READY,
            stage=RunStage.IDLE,
            selected_paragraphs=list(selected_paragraphs or []),
            checkpoint=RunCheckpoint(
                run_id=run_id,
                stage=RunStage.IDLE,
                selected_paragraphs=list(selected_paragraphs or []),
            ),
        )
        self._controls[run_id] = RunControls()
        return self._save_run(run)

    def pause_after_current(self, run_id: str) -> None:
        self._controls.setdefault(run_id, RunControls()).pause_after_current = True
        run = self._run_repository.load(run_id)
        if run is not None:
            self._emit(
                "run.pause_requested",
                EventLevel.INFO,
                "Pause requested after current paragraph",
                run,
            )

    def cancel(self, run_id: str) -> None:
        self._controls.setdefault(run_id, RunControls()).cancel_requested = True
        run = self._run_repository.load(run_id)
        if run is not None:
            self._emit(
                "run.cancel_requested",
                EventLevel.WARNING,
                "Cancellation requested",
                run,
            )

    def is_cancel_requested(self, run_id: str) -> bool:
        return self._controls.setdefault(run_id, RunControls()).cancel_requested

    def execute(
        self,
        run: Run,
        paragraphs: list[ParagraphUnit],
        processor: ParagraphProcessor,
        *,
        perf_context: PerformanceContext | None = None,
    ) -> Run:
        controls = self._controls.setdefault(run.run_id, RunControls())
        run.status = RunStatus.RUNNING
        run.stage = RunStage.PROVIDER_SEARCH
        run.started_at = run.started_at or _now()
        run.finished_at = None
        run.last_error = None
        self._emit(
            "run.started",
            EventLevel.INFO,
            "Run started",
            run,
            payload=(
                perf_context.summary_payload() if perf_context is not None else None
            ),
        )

        selected = set(run.selected_paragraphs) if run.selected_paragraphs else None
        remaining = [
            paragraph
            for paragraph in paragraphs
            if selected is None or paragraph.paragraph_no in selected
        ]

        if self._max_workers <= 1:
            for paragraph in remaining:
                if controls.cancel_requested:
                    return self._cancel_run(run, perf_context=perf_context)

                try:
                    result = processor(paragraph)
                except InterruptedError:
                    return self._cancel_run(run, perf_context=perf_context)
                except Exception as exc:
                    self._record_failure(
                        run, paragraph.paragraph_no, exc, perf_context=perf_context
                    )
                    if controls.pause_after_current:
                        return self._pause_run(run, perf_context=perf_context)
                    continue

                self._record_success(
                    run, result.paragraph_no, perf_context=perf_context
                )
                if controls.pause_after_current:
                    return self._pause_run(run, perf_context=perf_context)

            return self._finalize_run(run, perf_context=perf_context)

        with BoundedExecutor[ParagraphUnit, AssetSelection](
            max_workers=self._max_workers,
            queue_size=self._queue_size,
        ) as executor:
            remaining_iter = iter(remaining)
            pending: dict[Future[AssetSelection], ParagraphUnit] = {}

            def submit_next() -> bool:
                if controls.cancel_requested:
                    return False
                try:
                    paragraph = next(remaining_iter)
                except StopIteration:
                    return False
                pending[executor.submit(processor, paragraph)] = paragraph
                return True

            while len(pending) < self._queue_size and submit_next():
                pass

            while pending:
                if controls.cancel_requested:
                    return self._cancel_run(run, perf_context=perf_context)

                done, _ = wait(tuple(pending.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    paragraph = pending.pop(future)
                    try:
                        result = future.result()
                    except InterruptedError:
                        return self._cancel_run(run, perf_context=perf_context)
                    except Exception as exc:
                        self._record_failure(
                            run, paragraph.paragraph_no, exc, perf_context=perf_context
                        )
                        if controls.pause_after_current:
                            return self._pause_run(run, perf_context=perf_context)
                        continue

                    self._record_success(
                        run, result.paragraph_no, perf_context=perf_context
                    )
                    if controls.pause_after_current:
                        return self._pause_run(run, perf_context=perf_context)

                while len(pending) < self._queue_size and submit_next():
                    pass

        return self._finalize_run(run, perf_context=perf_context)

    def resume(
        self,
        run_id: str,
        paragraphs: list[ParagraphUnit],
        processor: ParagraphProcessor,
        *,
        perf_context: PerformanceContext | None = None,
    ) -> Run:
        run = self._run_repository.load(run_id)
        if run is None:
            raise KeyError(run_id)
        completed = set(run.completed_paragraphs)
        pending = [
            paragraph
            for paragraph in paragraphs
            if paragraph.paragraph_no not in completed
        ]
        controls = self._controls.setdefault(run_id, RunControls())
        controls.pause_after_current = False
        controls.cancel_requested = False
        self._emit("run.resumed", EventLevel.INFO, "Run resumed from checkpoint", run)
        return self.execute(run, pending, processor, perf_context=perf_context)

    def _cancel_run(
        self, run: Run, *, perf_context: PerformanceContext | None = None
    ) -> Run:
        run.status = RunStatus.CANCELLED
        run.finished_at = _now()
        run.stage = RunStage.PERSIST
        if run.checkpoint is not None:
            run.checkpoint.stage = RunStage.PERSIST
            run.checkpoint.completed_paragraphs = list(run.completed_paragraphs)
            run.checkpoint.failed_paragraphs = list(run.failed_paragraphs)
            run.checkpoint.updated_at = _now()
        self._emit("run.cancelled", EventLevel.WARNING, "Run cancelled", run)
        return self._save_run(run, perf_context=perf_context)

    def _pause_run(
        self, run: Run, *, perf_context: PerformanceContext | None = None
    ) -> Run:
        run.status = RunStatus.PAUSED
        run.stage = RunStage.PERSIST
        if run.checkpoint is not None:
            run.checkpoint.stage = RunStage.PERSIST
            run.checkpoint.completed_paragraphs = list(run.completed_paragraphs)
            run.checkpoint.failed_paragraphs = list(run.failed_paragraphs)
            run.checkpoint.updated_at = _now()
        self._emit("run.paused", EventLevel.INFO, "Run paused", run)
        return self._save_run(run, perf_context=perf_context)

    def _record_failure(
        self,
        run: Run,
        paragraph_no: int,
        exc: Exception,
        *,
        perf_context: PerformanceContext | None = None,
    ) -> None:
        run.stage = RunStage.PERSIST
        run.last_error = str(exc)
        if paragraph_no not in run.failed_paragraphs:
            run.failed_paragraphs.append(paragraph_no)
        if perf_context is not None:
            perf_context.increment("paragraphs_failed_total", 1)
        if run.checkpoint is not None:
            run.checkpoint.stage = RunStage.PERSIST
            run.checkpoint.current_paragraph_no = paragraph_no
            run.checkpoint.completed_paragraphs = list(run.completed_paragraphs)
            run.checkpoint.failed_paragraphs = list(run.failed_paragraphs)
            run.checkpoint.updated_at = _now()
        self._emit(
            "paragraph.failed",
            EventLevel.ERROR,
            f"Paragraph {paragraph_no} failed: {exc}",
            run,
            paragraph_no=paragraph_no,
            payload=(
                perf_context.summary_payload() if perf_context is not None else None
            ),
        )
        self._save_run(run, perf_context=perf_context)

    def _record_success(
        self,
        run: Run,
        paragraph_no: int,
        *,
        perf_context: PerformanceContext | None = None,
    ) -> None:
        run.stage = RunStage.PERSIST
        if paragraph_no not in run.completed_paragraphs:
            run.completed_paragraphs.append(paragraph_no)
        if perf_context is not None:
            perf_context.increment("paragraphs_completed_total", 1)
        run.failed_paragraphs = [
            item for item in run.failed_paragraphs if item != paragraph_no
        ]
        run.last_error = None if not run.failed_paragraphs else run.last_error
        if run.checkpoint is not None:
            run.checkpoint.stage = RunStage.PERSIST
            run.checkpoint.current_paragraph_no = paragraph_no
            run.checkpoint.completed_paragraphs = list(run.completed_paragraphs)
            run.checkpoint.failed_paragraphs = list(run.failed_paragraphs)
            run.checkpoint.updated_at = _now()
        self._emit(
            "paragraph.completed",
            EventLevel.INFO,
            f"Paragraph {paragraph_no} processed",
            run,
            paragraph_no=paragraph_no,
            payload=(
                perf_context.summary_payload() if perf_context is not None else None
            ),
        )
        self._save_run(run, perf_context=perf_context)

    def _finalize_run(
        self,
        run: Run,
        *,
        perf_context: PerformanceContext | None = None,
    ) -> Run:
        finalize_started_at = perf_counter()
        run.stage = RunStage.COMPLETE
        run.finished_at = _now()
        if run.checkpoint is not None:
            run.checkpoint.stage = RunStage.COMPLETE
            run.checkpoint.completed_paragraphs = list(run.completed_paragraphs)
            run.checkpoint.failed_paragraphs = list(run.failed_paragraphs)
            run.checkpoint.updated_at = _now()
        if perf_context is not None:
            perf_context.add_timing("finalize_ms", _elapsed_ms(finalize_started_at))
        payload = perf_context.summary_payload() if perf_context is not None else {}
        if run.failed_paragraphs:
            run.status = RunStatus.FAILED
            self._emit(
                "run.failed",
                EventLevel.ERROR,
                "Run completed with paragraph failures",
                run,
                payload={
                    "failed_paragraphs": list(run.failed_paragraphs),
                    **payload,
                },
            )
        else:
            run.status = RunStatus.COMPLETED
            run.last_error = None
            self._emit(
                "run.completed",
                EventLevel.INFO,
                "Run completed",
                run,
                payload=payload,
            )
        if perf_context is not None:
            self._emit(
                "run.perf",
                EventLevel.INFO,
                "Run performance summary",
                run,
                payload=perf_context.summary_payload(),
            )
        return self._save_run(run, perf_context=perf_context)

    def _save_run(
        self, run: Run, *, perf_context: PerformanceContext | None = None
    ) -> Run:
        if perf_context is not None:
            persist_run_performance_context(run, perf_context)
        return self._run_repository.save(run)

    def rerun_selected(
        self,
        run: Run,
        paragraph_numbers: list[int],
        paragraphs: list[ParagraphUnit],
        processor: ParagraphProcessor,
    ) -> Run:
        rerun = self.create_run(run.project_id, selected_paragraphs=paragraph_numbers)
        return self.execute(rerun, paragraphs, processor)

    def _emit(
        self,
        name: str,
        level: EventLevel,
        message: str,
        run: Run,
        *,
        paragraph_no: int | None = None,
        provider_name: str | None = None,
        query: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._event_bus.publish(
            AppEvent(
                name=name,
                level=level,
                message=message,
                run_id=run.run_id,
                project_id=run.project_id,
                paragraph_no=paragraph_no,
                provider_name=provider_name,
                query=query,
                stage=run.stage,
                payload=dict(payload or {}),
            )
        )
