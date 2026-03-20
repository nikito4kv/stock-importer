from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Condition
from time import perf_counter
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class QueueBackpressureStats:
    wait_ms: int
    queue_depth: int


class BoundedExecutor(Generic[T, R]):
    def __init__(self, max_workers: int, queue_size: int):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._queue_size = int(queue_size)
        self._inflight = 0
        self._condition = Condition()
        self._shutdown_called = False

    def submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        future, _stats = self.submit_with_stats(fn, item)
        return future

    def submit_with_stats(
        self,
        fn: Callable[[T], R],
        item: T,
        *,
        block: bool = True,
        timeout: float | None = None,
    ) -> tuple[Future[R], QueueBackpressureStats]:
        acquired, wait_ms, queue_depth = self._acquire_slot(
            block=bool(block),
            timeout=timeout,
        )
        if not acquired:
            raise TimeoutError("Bounded executor queue is full")
        return self._submit_reserved(fn, item), QueueBackpressureStats(
            wait_ms=wait_ms,
            queue_depth=queue_depth,
        )

    def try_submit(
        self,
        fn: Callable[[T], R],
        item: T,
    ) -> tuple[Future[R], QueueBackpressureStats] | None:
        acquired, wait_ms, queue_depth = self._acquire_slot(
            block=False,
            timeout=0.0,
        )
        if not acquired:
            return None
        return self._submit_reserved(fn, item), QueueBackpressureStats(
            wait_ms=wait_ms,
            queue_depth=queue_depth,
        )

    def inflight_count(self) -> int:
        with self._condition:
            return self._inflight

    def queue_size(self) -> int:
        return self._queue_size

    def map_unordered(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        futures = [self.submit(fn, item) for item in items]
        return [future.result() for future in as_completed(futures)]

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _submit_reserved(self, fn: Callable[[T], R], item: T) -> Future[R]:
        def wrapped() -> R:
            try:
                return fn(item)
            finally:
                self._release_slot()

        return self._executor.submit(wrapped)

    def _acquire_slot(
        self,
        *,
        block: bool,
        timeout: float | None,
    ) -> tuple[bool, int, int]:
        wait_started_at = perf_counter()
        with self._condition:
            if not block:
                if self._inflight >= self._queue_size:
                    return False, 0, self._inflight
            else:
                remaining = None if timeout is None else max(0.0, float(timeout))
                while self._inflight >= self._queue_size:
                    if remaining is not None and remaining <= 0.0:
                        return False, self._elapsed_ms(wait_started_at), self._inflight
                    before_wait = perf_counter()
                    self._condition.wait(remaining)
                    if remaining is not None:
                        remaining -= perf_counter() - before_wait
            self._inflight += 1
            return True, self._elapsed_ms(wait_started_at), self._inflight

    def _release_slot(self) -> None:
        with self._condition:
            self._inflight = max(0, self._inflight - 1)
            self._condition.notify()

    def _elapsed_ms(self, started_at: float) -> int:
        return int(round((perf_counter() - started_at) * 1000.0))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
