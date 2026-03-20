from __future__ import annotations

import unittest
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

POLLING_MODULE_PATH = Path(__file__).resolve().parents[1] / "ui" / "polling.py"
POLLING_SPEC = spec_from_file_location("ui_polling_module", POLLING_MODULE_PATH)
assert POLLING_SPEC is not None and POLLING_SPEC.loader is not None
POLLING_MODULE = module_from_spec(POLLING_SPEC)
sys.modules[POLLING_SPEC.name] = POLLING_MODULE
POLLING_SPEC.loader.exec_module(POLLING_MODULE)
plan_poll_refresh = POLLING_MODULE.plan_poll_refresh


class UiPollingTests(unittest.TestCase):
    def test_plan_uses_active_interval_for_running_status(self) -> None:
        plan = plan_poll_refresh(
            run_id="run-1",
            run_status="running",
            previous_terminal_signature=("run-1", "completed"),
            active_interval_ms=500,
            idle_interval_ms=1600,
        )
        self.assertFalse(plan.should_heavy_refresh)
        self.assertEqual(plan.next_interval_ms, 500)
        self.assertIsNone(plan.terminal_signature)

    def test_plan_requests_single_heavy_refresh_when_terminal_state_first_seen(
        self,
    ) -> None:
        first = plan_poll_refresh(
            run_id="run-1",
            run_status="completed",
            previous_terminal_signature=None,
            active_interval_ms=500,
            idle_interval_ms=1600,
        )
        second = plan_poll_refresh(
            run_id="run-1",
            run_status="completed",
            previous_terminal_signature=first.terminal_signature,
            active_interval_ms=500,
            idle_interval_ms=1600,
        )
        self.assertTrue(first.should_heavy_refresh)
        self.assertEqual(first.next_interval_ms, 1600)
        self.assertFalse(second.should_heavy_refresh)
        self.assertEqual(second.next_interval_ms, 1600)

    def test_plan_requests_refresh_again_for_new_terminal_signature(self) -> None:
        first = plan_poll_refresh(
            run_id="run-1",
            run_status="completed",
            previous_terminal_signature=None,
            active_interval_ms=500,
            idle_interval_ms=1600,
        )
        next_run = plan_poll_refresh(
            run_id="run-2",
            run_status="completed",
            previous_terminal_signature=first.terminal_signature,
            active_interval_ms=500,
            idle_interval_ms=1600,
        )
        self.assertTrue(next_run.should_heavy_refresh)
        self.assertEqual(next_run.terminal_signature, ("run-2", "completed"))


if __name__ == "__main__":
    unittest.main()
