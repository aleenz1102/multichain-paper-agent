import unittest

from app.auto_monitor import MonitorConfig, PaperSignalMonitor


class MonitorTests(unittest.TestCase):
    def test_disabled_monitor_does_not_fetch_or_audit(self):
        calls = []
        monitor = PaperSignalMonitor(
            candle_provider=lambda symbol: calls.append(symbol) or [],
            audit=lambda event, detail: calls.append(event),
        )
        result = monitor.evaluate(["ETH"])
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(calls, [])

    def test_insufficient_history_is_audited_as_hold(self):
        audited = []
        monitor = PaperSignalMonitor(
            candle_provider=lambda symbol: [{"close": 100}] * 10,
            audit=lambda event, detail: audited.append((event, detail)),
            config=MonitorConfig(enabled=True),
        )
        result = monitor.evaluate(["ETH"])
        self.assertEqual(result["evaluated"], 1)
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(result["results"][0]["signal"], "HOLD")
        self.assertEqual(audited[0][0], "AUTO_SIGNAL_EVALUATED")

    def test_rejects_too_fast_interval(self):
        with self.assertRaises(ValueError):
            MonitorConfig(interval_seconds=30)


    def test_provider_failure_is_audited_as_hold_without_orders(self):
        audited = []
        calls = []

        def provider(symbol):
            calls.append(symbol)
            if symbol == "SOL":
                raise RuntimeError("simulated provider outage")
            return [{"close": 100}] * 10

        monitor = PaperSignalMonitor(
            candle_provider=provider,
            audit=lambda event, detail: audited.append((event, detail)),
            config=MonitorConfig(enabled=True),
        )

        result = monitor.evaluate(["SOL", "ETH"])

        self.assertEqual(calls, ["SOL", "ETH"])
        self.assertEqual(result["evaluated"], 2)
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(
            [item["signal"] for item in result["results"]],
            ["HOLD", "HOLD"],
        )
        self.assertIn("provider failed", result["results"][0]["reason"].lower())
        self.assertEqual(len(audited), 2)
        self.assertTrue(all(not item[1]["orders_submitted"] for item in audited))

    def test_mixed_timestamps_become_hold(self):
        candles = [{"timestamp": f"2026-01-01T00:{i:02d}:00+00:00", "close": 100 + i} for i in range(30)]
        candles[15].pop("timestamp")
        monitor = PaperSignalMonitor(
            candle_provider=lambda symbol: candles,
            audit=lambda event, detail: None,
            config=MonitorConfig(enabled=True),
        )
        result = monitor.evaluate(["ETH"])
        self.assertEqual(result["results"][0]["signal"], "HOLD")
        self.assertIn("timestamps", result["results"][0]["reason"].lower())

    def test_duplicate_timestamps_become_hold(self):
        candles = [{"timestamp": f"2026-01-01T00:{i:02d}:00+00:00", "close": 100 + i} for i in range(30)]
        candles[20]["timestamp"] = candles[19]["timestamp"]
        monitor = PaperSignalMonitor(
            candle_provider=lambda symbol: candles,
            audit=lambda event, detail: None,
            config=MonitorConfig(enabled=True),
        )
        result = monitor.evaluate(["ETH"])
        self.assertEqual(result["results"][0]["signal"], "HOLD")
        self.assertIn("strictly chronological", result["results"][0]["reason"])

    def test_invalid_candle_values_become_hold(self):
        audited = []
        candles = [{"close": 100}] * 29 + [{"close": float("nan")}]

        monitor = PaperSignalMonitor(
            candle_provider=lambda symbol: candles,
            audit=lambda event, detail: audited.append((event, detail)),
            config=MonitorConfig(enabled=True),
        )

        result = monitor.evaluate(["ETH"])

        self.assertEqual(result["results"][0]["signal"], "HOLD")
        self.assertFalse(result["orders_submitted"])
        self.assertEqual(len(audited), 1)


if __name__ == "__main__":
    unittest.main()
