import unittest
from app.auto_scheduler import PaperAutoScheduler


class PaperAutoSchedulerTests(unittest.TestCase):
    def _make(self, enabled=True, paused=False, cycle=None, interval=60):
        events = []
        scheduler = PaperAutoScheduler(
            enabled=lambda: enabled,
            paused=lambda: paused,
            run_cycle=cycle or (lambda: {"mode": "PAPER_ONLY"}),
            audit=lambda event, detail: events.append((event, detail)),
            interval_seconds=interval,
        )
        return scheduler, events

    def test_disabled_scheduler_does_not_run_cycle(self):
        calls = []
        scheduler, _ = self._make(enabled=False, cycle=lambda: calls.append("run"))
        self.assertEqual(scheduler.run_once()["status"], "disabled")
        self.assertEqual(calls, [])

    def test_paused_scheduler_does_not_run_cycle(self):
        calls = []
        scheduler, _ = self._make(paused=True, cycle=lambda: calls.append("run"))
        self.assertEqual(scheduler.run_once()["status"], "paused")
        self.assertEqual(calls, [])

    def test_enabled_cycle_is_audited_as_paper_only(self):
        scheduler, events = self._make()
        result = scheduler.run_once()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["mode"], "PAPER_ONLY")
        self.assertEqual(scheduler.last_cycle_status, "completed")
        self.assertIsNotNone(scheduler.last_cycle_at)
        self.assertIsNone(scheduler.last_cycle_error)
        self.assertTrue(any(name == "AUTO_SCHEDULER_CYCLE" and not data["live_trading"] for name, data in events))

    def test_failures_are_contained(self):
        def fail():
            raise RuntimeError("sensitive internal detail")
        scheduler, events = self._make(cycle=fail)
        self.assertEqual(scheduler.run_once(), {"status": "failed", "reason": "RuntimeError"})
        self.assertEqual(scheduler.last_cycle_status, "failed")
        self.assertEqual(scheduler.last_cycle_error, "RuntimeError")
        self.assertIsNotNone(scheduler.last_cycle_at)
        self.assertTrue(any(name == "AUTO_SCHEDULER_CYCLE_FAILED" for name, _ in events))

    def test_rejects_short_interval(self):
        with self.assertRaises(ValueError):
            self._make(interval=30)

    def test_scheduler_stops_cleanly(self):
        scheduler, events = self._make(enabled=False)
        scheduler.start()
        self.assertTrue(scheduler.running)
        scheduler.stop(timeout=2)
        self.assertFalse(scheduler.running)
        names = [name for name, _ in events]
        self.assertIn("AUTO_SCHEDULER_STARTED", names)
        self.assertIn("AUTO_SCHEDULER_STOPPED", names)


if __name__ == "__main__":
    unittest.main()
