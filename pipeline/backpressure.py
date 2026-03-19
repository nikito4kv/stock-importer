from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from threading import Semaphore
from typing import Callable, Generic, Iterable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


class BoundedExecutor(Generic[T, R]):
    def __init__(self, max_workers: int, queue_size: int):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._capacity = Semaphore(queue_size)

    def submit(self, fn: Callable[[T], R], item: T) -> Future[R]:
        self._capacity.acquire()

        def wrapped() -> R:
            try:
                return fn(item)
            finally:
                self._capacity.release()

        return self._executor.submit(wrapped)

    def map_unordered(self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        futures = [self.submit(fn, item) for item in items]
        return [future.result() for future in as_completed(futures)]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
