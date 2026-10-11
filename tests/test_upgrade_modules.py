import unittest
from app.risk import RiskLimits, RiskSnapshot, check_entry
from app.strategy import combined_signal, classify_regime
from app.backtest import BacktestConfig, run_backtest

class RiskTests(unittest.TestCase):
    def test_stale_prices_block_entries(self):
        s = RiskSnapshot(100_000, 1_000, 0, False, 0, 0, 0, 100_000, price_is_fresh=False)
        d = check_entry(s, RiskLimits())
        self.assertFalse(d.allowed)
        self.assertIn("stale", d.reason)

    def test_trade_size_cap(self):
        s = RiskSnapshot(100_000, 6_000, 0, False, 0, 0, 0, 100_000)
        self.assertFalse(check_entry(s, RiskLimits(max_trade_pct=5),).allowed)

class StrategyTests(unittest.TestCase):
    def test_insufficient_history_holds(self):
        result = combined_signal([{"close": 100 + i} for i in range(10)])
        self.assertEqual(result["signal"], "HOLD")
        self.assertEqual(result["regime"], "uncertain")

    def test_regime_classifies_enough_data(self):
        candles = [{"close": 100 + i * 0.5} for i in range(30)]
        self.assertIn(classify_regime(candles)["regime"], {"trending", "range_bound", "high_volatility"})

class BacktestTests(unittest.TestCase):
    def test_no_trade_baseline_preserves_equity(self):
        candles = [{"close": p} for p in [100, 101, 99, 102, 100]]
        result = run_backtest(candles, lambda history: {"signal": "HOLD"}, BacktestConfig())
        self.assertEqual(result["final_equity"], 100000)
        self.assertEqual(result["fill_count"], 0)

    def test_long_trade_charges_costs(self):
        candles = [{"close": p} for p in [100, 100, 110, 110]]
        result = run_backtest(candles, lambda history: {"signal": "BUY" if len(history) == 1 else "HOLD"}, BacktestConfig())
        self.assertGreater(result["fill_count"], 0)
        self.assertNotEqual(result["final_equity"], 100000)
        self.assertGreater(result["assumptions"]["fee_bps"], 0)

    def test_short_round_trip_has_no_unbounded_cash_creation(self):
        candles = [{"close": p} for p in [100, 100, 90, 90]]
        result = run_backtest(candles, lambda history: {"signal": "SELL" if len(history) == 1 else "HOLD"}, BacktestConfig())
        self.assertLess(result["final_equity"], 120000)
        self.assertGreater(result["final_equity"], 80000)
        self.assertEqual([x["action"] for x in result["fills"]], ["OPEN_SHORT", "COVER_SHORT"])

if __name__ == "__main__": unittest.main()
