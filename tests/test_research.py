import unittest
from datetime import datetime, timedelta, timezone

from app.research import walk_forward_report


def candles(n=180):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = []
    price = 100.0
    for i in range(n):
        price *= 1.001 if (i // 18) % 2 == 0 else 0.999
        result.append({
            "timestamp": (start + timedelta(minutes=5 * i)).isoformat(),
            "open": price * 0.999, "high": price * 1.002,
            "low": price * 0.998, "close": price, "volume": 1000 + i,
        })
    return result


class WalkForwardTests(unittest.TestCase):
    def test_report_has_three_chronological_windows_and_costs(self):
        report = walk_forward_report(candles(), fee_bps=10, slippage_bps=8)
        self.assertTrue({"train", "validation", "test"}.issubset(report["windows"]))
        self.assertIn("test_buy_and_hold_benchmark", report["windows"])
        self.assertIn("test_outperformed_buy_and_hold_after_costs", report)
        self.assertGreater(report["windows"]["train"]["candles"], report["windows"]["test"]["candles"])
        self.assertEqual(report["cost_model"]["fee_bps_per_fill"], 10)
        self.assertIn("test_is_profitable_after_costs", report)

    def test_rejects_short_history(self):
        with self.assertRaises(ValueError):
            walk_forward_report(candles(90))

    def test_rejects_out_of_order_candles(self):
        data = candles()
        data[0], data[1] = data[1], data[0]
        with self.assertRaises(ValueError):
            walk_forward_report(data)


if __name__ == "__main__":
    unittest.main()
