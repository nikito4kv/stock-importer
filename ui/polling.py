from __future__ import annotations

from dataclasses import dataclass


TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "paused"})


@dataclass(slots=True, frozen=True)
class PollRefreshPlan:
    should_heavy_refresh: bool
    next_interval_ms: int
    terminal_signature: tuple[str | None, str | None] | None


def plan_poll_refresh(
    *,
    run_id: str | None,
    run_status: str | None,
    previous_terminal_signature: tuple[str | None, str | None] | None,
    active_interval_ms: int,
    idle_interval_ms: int,
) -> PollRefreshPlan:
    is_terminal = run_status is None or run_status in TERMINAL_RUN_STATUSES
    if not is_terminal:
        return PollRefreshPlan(
            should_heavy_refresh=False,
            next_interval_ms=active_interval_ms,
            terminal_signature=None,
        )

    signature = (run_id, run_status)
    return PollRefreshPlan(
        should_heavy_refresh=signature != previous_terminal_signature,
        next_interval_ms=idle_interval_ms,
        terminal_signature=signature,
    )
