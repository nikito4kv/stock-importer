from __future__ import annotations

from dataclasses import dataclass
import time

from config.settings import BrowserSettings


@dataclass(slots=True)
class SlowModePolicy:
    enabled: bool = True
    action_delay_ms: int = 900
    failure_backoff_step_seconds: float = 0.5
    max_backoff_seconds: float = 4.0

    @classmethod
    def from_settings(cls, settings: BrowserSettings) -> "SlowModePolicy":
        return cls(enabled=settings.slow_mode, action_delay_ms=settings.action_delay_ms)


class BrowserActionPacer:
    def __init__(self, policy: SlowModePolicy, *, sleep_func=None):
        self._policy = policy
        self._sleep = sleep_func or time.sleep
        self._failure_backoff_seconds = 0.0

    @property
    def current_backoff_seconds(self) -> float:
        return self._failure_backoff_seconds

    def before_action(self) -> float:
        delay = self.next_delay_seconds()
        if delay > 0:
            self._sleep(delay)
        return delay

    def next_delay_seconds(self) -> float:
        if not self._policy.enabled:
            return self._failure_backoff_seconds
        return max(0.0, self._policy.action_delay_ms / 1000.0) + self._failure_backoff_seconds

    def record_failure(self) -> float:
        self._failure_backoff_seconds = min(
            self._policy.max_backoff_seconds,
            self._failure_backoff_seconds + self._policy.failure_backoff_step_seconds,
        )
        return self._failure_backoff_seconds

    def record_success(self) -> float:
        self._failure_backoff_seconds = max(0.0, self._failure_backoff_seconds - self._policy.failure_backoff_step_seconds)
        return self._failure_backoff_seconds
