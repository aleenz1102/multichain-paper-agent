import unittest

from app.auto_scheduler import PaperAutoScheduler


class PaperAutoSchedulerTests(unittest.TestCase):
    def make_scheduler(self, enabled=True, paused=False, run_cycle=None):
        events = []
        scheduler = PaperAutoScheduler(
            enabled=lambda: enabled,
            paused=lambda: paused,
            run_cycle=run_cycle or (lambda: {"mode": "PAPER_ONLY"}),
            audit=lambda event, detail: events.append((event, detail)),
            interval_seconds=60,
        )
        return scheduler, events

    def test_disabled_scheduler_does_not_run_cycle(self):
        calls = []
        scheduler, _ = self.make_scheduler(enabled=False, run_cycle=lambda: calls.append("run"))
        self.assertEqual(scheduler.run_once()["status"], "disabled")
        self.assertEqual(calls, [])

    def test_paused_scheduler_does_not_run_cycle(self):
        calls = []
        scheduler, _ = self.make_scheduler(paused=True, run_cycle=lambda: calls.append("run"))
        self.assertEqual(scheduler.run_once()["status"], "paused")
        self.assertEqual(calls, [])

    def test_enabled_cycle_is_audited_as_paper_only(self):
        scheduler, events = self.make_scheduler()
        result = scheduler.run_once()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["mode"], "PAPER_ONLY")
        self.assertTrue(any(e == "AUTO_SCHEDULER_CYCLE" and d["live_trading"] is False for e, d in events))

    def test_cycle_failure_is_contained_and_audited_without_exception_leak(self):
        def fail():
            raise RuntimeError("private detail should not leak")
        scheduler, events = self.make_scheduler(run_cycle=fail)
        result = scheduler.run_once()
        self.assertEqual(result, {"status": "failed", "reason": "RuntimeError"})
        self.assertTrue(any(e == "AUTO_SCHEDULER_CYCLE_FAILED" for e, _ in events))

    def test_rejects_interval_under_one_minute(self):
        with self.assertRaises(ValueError):
            PaperAutoScheduler(enabled=lambda: False, paused=lambda: False,
                               run_cycle=lambda: None, audit=lambda *_: None,
                               interval_seconds=30)


if __name__ == "__main__":
    unittest.main()
