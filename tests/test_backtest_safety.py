import math
import unittest

from app.backtest import BacktestConfig, run_backtest


def candles(closes):
    return [{"timestamp": str(i), "close": price} for i, price in enumerate(closes)]


def hold(_seen):
    return {"signal": "HOLD"}


class BacktestSafetyTests(unittest.TestCase):
    def test_rejects_invalid_configuration(self):
        invalid = [
            BacktestConfig(initial_cash=0),
            BacktestConfig(initial_cash=float("nan")),
            BacktestConfig(fee_bps=-1),
            BacktestConfig(slippage_bps=-1),
            BacktestConfig(fee_bps=10_000),
            BacktestConfig(slippage_bps=10_000),
            BacktestConfig(position_fraction=0),
            BacktestConfig(position_fraction=1.01),
            BacktestConfig(position_fraction=float("inf")),
        ]
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    run_backtest(candles([100, 101]), hold, config)

    def test_rejects_non_positive_or_non_finite_close(self):
        for close in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(close=close):
                with self.assertRaisesRegex(ValueError, "finite and positive"):
                    run_backtest(candles([100, close]), hold)

    def test_missing_close_is_reported_as_invalid_candle(self):
        with self.assertRaisesRegex(ValueError, "numeric close"):
            run_backtest([{"timestamp": "1", "close": 100}, {"timestamp": "2"}], hold)

    def test_hold_strategy_does_not_create_fills(self):
        result = run_backtest(candles([100, 101, 99, 102]), hold)
        self.assertEqual(result["fill_count"], 0)
        self.assertEqual(result["final_equity"], result["initial_equity"])


if __name__ == "__main__":
    unittest.main()
