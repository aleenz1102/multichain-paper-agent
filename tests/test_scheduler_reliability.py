import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main
from app.auto_scheduler import PaperAutoScheduler


class SchedulerReliabilityTests(unittest.TestCase):
    def test_scheduler_runs_immediately_when_enabled(self):
        enabled = {"value": False}
        fired = threading.Event()
        calls = []

        def cycle():
            calls.append("cycle")
            fired.set()
            return {"status": "ok"}

        scheduler = PaperAutoScheduler(
            enabled=lambda: enabled["value"],
            paused=lambda: False,
            run_cycle=cycle,
            audit=lambda *_: None,
            interval_seconds=60,
        )
        scheduler.start()
        try:
            enabled["value"] = True
            self.assertTrue(fired.wait(3), "scheduler did not run promptly after enable")
        finally:
            scheduler.stop()
        self.assertEqual(calls, ["cycle"])
        self.assertEqual(scheduler.cycles_completed, 1)
        self.assertEqual(scheduler.last_cycle_status, "completed")
        self.assertIsNotNone(scheduler.last_cycle_duration_seconds)

    def test_scheduler_counts_failed_cycles(self):
        scheduler = PaperAutoScheduler(
            enabled=lambda: True,
            paused=lambda: False,
            run_cycle=lambda: (_ for _ in ()).throw(RuntimeError("provider down")),
            audit=lambda *_: None,
            interval_seconds=60,
        )
        result = scheduler.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(scheduler.cycles_failed, 1)
        self.assertEqual(scheduler.last_cycle_status, "failed")

    def test_scheduler_rejects_sub_minute_trading_cadence(self):
        with self.assertRaises(ValueError):
            PaperAutoScheduler(
                enabled=lambda: True, paused=lambda: False,
                run_cycle=lambda: None, audit=lambda *_: None,
                interval_seconds=1,
            )


class AutoTradeDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.original_db = main.DB_PATH
        self.tmp = tempfile.TemporaryDirectory()
        main.DB_PATH = Path(self.tmp.name) / "test-paper.db"
        main.init_db()
        main.set_setting("auto_monitor_enabled", True)
        main.set_setting("market_data_mode", "live_public")

    def tearDown(self):
        main.DB_PATH = self.original_db
        self.tmp.cleanup()

    def test_completed_candle_is_never_processed_twice(self):
        candle_time = "2026-10-13T00:00:00+00:00"

        class FakeMonitor:
            def evaluate(self, symbols):
                return {
                    "status": "completed",
                    "evaluated": 1,
                    "results": [{
                        "symbol": "SOL",
                        "signal": "HOLD",
                        "candle_timestamp": candle_time,
                    }],
                }

        request = main.AutoMonitorEvaluation(
            symbols=["SOL"], auto_select_trending=False
        )
        with patch.object(main, "_build_signal_monitor", return_value=FakeMonitor()):
            first = main.auto_trade_run(request)
            second = main.auto_trade_run(request)

        self.assertEqual(first["paper_actions"], [])
        self.assertEqual(
            second["paper_actions"][0]["status"], "skipped_duplicate_candle"
        )
        self.assertEqual(
            main.settings()["last_auto_candle_by_symbol"]["SOL"], candle_time
        )

    def test_home_response_declares_utf8(self):
        response = main.home()
        self.assertIn("charset=utf-8", response.headers["content-type"].lower())
        self.assertIn(b"MULTICHAIN PAPER AGENT", response.body)
