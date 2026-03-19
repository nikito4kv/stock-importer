from __future__ import annotations

from collections.abc import Callable
import random
import time
from typing import TypeVar


T = TypeVar("T")


def retry_call(
    operation: Callable[[], T],
    *,
    retries: int,
    base_delay_seconds: float,
    jitter_seconds: float = 0.0,
    error_prefix: str,
) -> T:
    last_error: Exception | None = None
    attempts = max(1, int(retries) + 1)

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                delay = max(0.0, base_delay_seconds * attempt)
                if jitter_seconds > 0:
                    delay += random.uniform(0.0, jitter_seconds)
                time.sleep(delay)

    raise RuntimeError(f"{error_prefix}: {last_error}")
