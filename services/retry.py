from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import AppError


@dataclass(frozen=True, slots=True)
class RetryProfile:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    fatal: bool
    error_code: str
    details: dict[str, Any] = field(default_factory=dict)


def build_retry_profile(
    retry_budget: int,
    *,
    base_delay_seconds: float,
    max_delay_seconds: float,
    jitter_seconds: float = 0.0,
) -> RetryProfile:
    return RetryProfile(
        max_attempts=max(1, int(retry_budget) + 1),
        base_delay_seconds=max(0.0, float(base_delay_seconds)),
        max_delay_seconds=max(0.0, float(max_delay_seconds)),
        jitter_seconds=max(0.0, float(jitter_seconds)),
    )


def is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return exc.__class__.__name__.casefold().endswith("timeouterror")


def classify_retryable_exception(
    exc: Exception,
    *,
    timeout_error_code: str,
    default_error_code: str,
) -> RetryDecision:
    if is_timeout_exception(exc):
        return RetryDecision(
            retryable=True,
            fatal=False,
            error_code=timeout_error_code,
        )

    if isinstance(exc, AppError):
        return RetryDecision(
            retryable=bool(exc.retryable) and not bool(exc.fatal),
            fatal=bool(exc.fatal),
            error_code=str(exc.code or default_error_code),
            details=dict(exc.details),
        )

    return RetryDecision(
        retryable=False,
        fatal=False,
        error_code=default_error_code,
    )


def compute_retry_delay_seconds(
    profile: RetryProfile,
    attempt_count: int,
) -> float:
    attempt = max(1, int(attempt_count))
    delay = float(profile.base_delay_seconds) * (2 ** max(0, attempt - 1))
    delay = min(float(profile.max_delay_seconds), delay)
    if profile.jitter_seconds > 0:
        delay = min(
            float(profile.max_delay_seconds),
            delay + random.uniform(0.0, float(profile.jitter_seconds)),
        )
    return round(max(0.0, delay), 6)


def sleep_for_retry_attempt(
    profile: RetryProfile,
    attempt_count: int,
) -> float:
    delay = compute_retry_delay_seconds(profile, attempt_count)
    if delay > 0:
        time.sleep(delay)
    return delay


__all__ = [
    "RetryDecision",
    "RetryProfile",
    "build_retry_profile",
    "classify_retryable_exception",
    "compute_retry_delay_seconds",
    "is_timeout_exception",
    "sleep_for_retry_attempt",
]
