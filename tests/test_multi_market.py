import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main
from app.market_adapters import MarketDataError, PublicMultiMarketProvider


class FakeResponse:
    def __init__(self, payload):
        import json
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


class MultiMarketPaperTests(unittest.TestCase):
    def setUp(self):
        self.old_db = main.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        main.DB_PATH = Path(self.temp.name) / "multi-market-test.db"
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.old_db
        self.temp.cleanup()

    def fresh_quote(self, market_type, symbol):
        return {
            "market_type": market_type, "symbol": symbol,
            "display_symbol": symbol, "name": symbol, "price": 2.0,
            "currency": "USD", "data_status": "LIVE PUBLIC PRICE",
            "provider": "test fixture",
        }

    def test_separate_account_defaults_to_thirty_dollars_and_auto_off(self):
        cfg = main.multi_market_settings()
        self.assertEqual(cfg["virtual_cash_usd"], 30.0)
        self.assertEqual(cfg["max_trade_pct"], 3.0)
        self.assertEqual(cfg["max_positions"], 3)
        self.assertEqual(cfg["max_drawdown_pct"], 8.0)
        self.assertEqual(cfg["max_exposure_pct"], 30.0)
        self.assertFalse(cfg["auto_enabled"])
        self.assertFalse(cfg["paused"])

    def test_paper_buy_uses_separate_ledger_and_keeps_legacy_trades_untouched(self):
        with patch.object(main, "multi_market_quote", side_effect=self.fresh_quote):
            result = main.multi_market_order(main.MultiMarketOrderInput(
                market_type="stocks", symbol="STKAAPL", side="BUY", amount_usd=0.9,
            ))
            self.assertTrue(result["ok"])
            self.assertFalse(result["portfolio"]["live_trading"])
            self.assertAlmostEqual(result["notional_usd"], 0.9, places=6)
            self.assertEqual(result["side"], "BUY")
        self.assertEqual(len(main.rows("SELECT * FROM multi_market_trades")), 1)
        self.assertEqual(len(main.rows("SELECT * FROM trades")), 0)
        self.assertLess(main.multi_market_portfolio()["virtual_cash_usd"], 30.0)

    def test_missing_or_stale_quote_blocks_order(self):
        with patch.object(main, "multi_market_quote", side_effect=HTTPException(503, "stale quote")):
            with self.assertRaises(HTTPException) as error:
                main.multi_market_order(main.MultiMarketOrderInput(
                    market_type="stocks", symbol="STKAAPL", side="BUY", amount_usd=3.0,
                ))
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(main.rows("SELECT * FROM multi_market_trades"), [])

    def test_market_switch_does_not_enable_prediction_auto_trading(self):
        with self.assertRaises(HTTPException) as error:
            main.multi_market_update_settings(main.MultiMarketSettingsUpdate(
                selected_market="prediction", auto_enabled=True,
            ))
        self.assertEqual(error.exception.status_code, 400)
        self.assertFalse(main.multi_market_settings()["auto_enabled"])

    def test_automation_is_disabled_by_default(self):
        with self.assertRaises(HTTPException) as error:
            main.multi_market_auto_run(main.MultiMarketAutoRunInput(market_type="stocks"))
        self.assertEqual(error.exception.status_code, 400)

    def test_weak_buy_signal_is_blocked_by_auto_entry_gate(self):
        main.set_multi_market_setting("auto_enabled", True)
        candles = [{"timestamp": "2026-01-01T00:00:00+00:00", "close": 2.0}] * 40
        signal = {"signal": "BUY", "score": 0.40, "regime": "trending", "reason": "test"}
        with (
            patch.object(main, "multi_market_instruments", return_value={
                "provider_status": "fresh",
                "items": [{"symbol": "STKAAPL", "market_type": "stocks"}],
            }),
            patch.object(main, "multi_market_candles", return_value=candles),
            patch.object(main, "combined_signal", return_value=signal),
            patch.object(main, "build_trade_plan", return_value={"available": True}),
            patch.object(main, "multi_market_order") as place_order,
        ):
            result = main.multi_market_auto_cycle("stocks", 1)
        self.assertEqual(result["paper_fills"], 0)
        self.assertEqual(result["actions"][0]["status"], "no_action")
        self.assertIn("score >= 0.45", result["actions"][0]["reason"])
        place_order.assert_not_called()

    def test_auto_stop_loss_closes_bot_managed_position_and_clears_risk(self):
        main.set_multi_market_setting("auto_enabled", True)
        main.set_multi_market_setting("auto_position_risk", {
            "stocks:STKAAPL": {
                "entry_price": 2.0, "stop_loss": 1.9, "take_profit": 2.2,
                "opened_candle_timestamp": "2026-01-01T00:00:00+00:00",
                "entry_trade_id": 1,
            },
        })
        candles = [{
            "timestamp": "2026-01-01T00:15:00+00:00",
            "open": 2.0, "high": 2.05, "low": 1.85, "close": 1.92, "volume": 10,
        }]
        with (
            patch.object(main, "multi_market_instruments", return_value={
                "provider_status": "fresh",
                "items": [{"symbol": "STKAAPL", "market_type": "stocks"}],
            }),
            patch.object(main, "multi_market_candles", return_value=candles),
            patch.object(main, "multi_market_quote", side_effect=self.fresh_quote),
        ):
            with patch.object(main, "multi_market_order", return_value={"trade_id": 2}) as place_order:
                result = main.multi_market_auto_cycle("stocks", 1)
        self.assertEqual(result["actions"][0]["status"], "risk_exit_filled")
        self.assertEqual(result["actions"][0]["reason"], "stop_loss")
        order = place_order.call_args.args[0]
        self.assertEqual(order.side, "SELL")
        self.assertEqual(order.sell_percent, 100)
        self.assertEqual(main.multi_market_settings()["auto_position_risk"], {})

    def test_auto_stop_does_not_trigger_on_entry_candle(self):
        main.set_multi_market_setting("auto_enabled", True)
        main.set_multi_market_setting("auto_position_risk", {
            "stocks:STKAAPL": {
                "entry_price": 2.0, "stop_loss": 1.9, "take_profit": 2.2,
                "opened_candle_timestamp": "2026-01-01T00:15:00+00:00",
                "entry_trade_id": 1,
            },
        })
        candles = [{
            "timestamp": "2026-01-01T00:15:00+00:00",
            "open": 2.0, "high": 2.25, "low": 1.85, "close": 2.0, "volume": 10,
        }]
        with (
            patch.object(main, "multi_market_instruments", return_value={
                "provider_status": "fresh",
                "items": [{"symbol": "STKAAPL", "market_type": "stocks"}],
            }),
            patch.object(main, "multi_market_candles", return_value=candles),
            patch.object(main, "multi_market_quote", side_effect=self.fresh_quote),
            patch.object(main, "combined_signal", return_value={"signal": "HOLD", "score": 0.0, "regime": "range_bound"}),
            patch.object(main, "multi_market_order") as place_order,
        ):
            result = main.multi_market_auto_cycle("stocks", 1)
        self.assertNotEqual(result["actions"][0]["status"], "risk_exit_filled")
        place_order.assert_not_called()

    def test_automation_sweep_includes_supported_markets_but_not_prediction(self):
        main.set_multi_market_setting("auto_enabled", True)
        with patch.object(main, "multi_market_auto_cycle", side_effect=lambda market, max_symbols=6: {
            "status": "completed", "market_type": market, "paper_fills": 0, "actions": [],
        }) as cycle:
            result = main.multi_market_auto_cycle_all(max_symbols=1)
        self.assertGreaterEqual(result["markets_scanned"], 1)
        self.assertNotIn("prediction", [c["market_type"] for c in result["market_cycles"]])
        self.assertTrue(all(c["status"] == "completed" for c in result["market_cycles"]))
        self.assertEqual(cycle.call_count, result["markets_scanned"])

    def test_market_catalog_is_paper_only(self):
        catalog = main.multi_market_catalog()
        ids = {x["id"] for x in catalog["markets"]}
        self.assertTrue({"crypto", "stocks", "forex", "commodities", "indices", "prediction"}.issubset(ids))
        self.assertFalse(catalog["live_trading"])


class PublicMarketAdapterTests(unittest.TestCase):
    def test_yahoo_adapter_returns_validated_completed_candles(self):
        import time
        now = int(time.time())
        times = [now - 2700, now - 1800, now - 900]
        payload = {"chart": {"error": None, "result": [{
            "meta": {"currency": "USD", "regularMarketPrice": 150.0,
                     "regularMarketTime": now, "chartPreviousClose": 145.0},
            "timestamp": times,
            "indicators": {"quote": [{
                "open": [140.0, 145.0, 146.0],
                "high": [151.0, 152.0, 153.0],
                "low": [139.0, 144.0, 145.0],
                "close": [149.0, 150.0, 151.0],
                "volume": [10, 11, 12],
            }]},
        }]}}
        provider = PublicMultiMarketProvider(opener=lambda *a, **k: FakeResponse(payload))
        result = provider._yahoo_one("stocks", "STKAAPL", "AAPL", "Apple")
        self.assertEqual(result["symbol"], "STKAAPL")
        self.assertEqual(result["price"], 150.0)
        self.assertEqual(result["data_status"], "LIVE PUBLIC PRICE")
        self.assertEqual(len(result["_candles"]), 3)
        self.assertIn("timestamp", result["_candles"][-1])

    def test_prediction_markets_are_explicitly_not_auto_supported(self):
        provider = PublicMultiMarketProvider()
        self.assertFalse(provider.catalog()[5]["auto_supported"])


if __name__ == "__main__":
    unittest.main()



class NativePaperOpportunityRankingTests(unittest.TestCase):
    def test_ranks_fresh_positive_momentum_and_volume_without_external_sdk(self):
        from app.opportunity_ranker import rank_candidates
        sample = [
            {"symbol": "LOW", "change_pct": -2.0, "volume": 100, "data_status": "STALE",
             "history_status": "stale", "candle_count": 5},
            {"symbol": "HIGH", "change_pct": 3.0, "volume": 10000, "data_status": "LIVE PUBLIC PRICE",
             "history_status": "fresh", "candle_count": 120},
        ]
        ranked, meta = rank_candidates("crypto", sample)
        self.assertEqual(ranked[0]["symbol"], "HIGH")
        self.assertGreater(ranked[0]["opportunity_rank_score"], ranked[1]["opportunity_rank_score"])
        self.assertIn("fresh public quote", ranked[0]["opportunity_rank_reasons"])
        self.assertEqual(meta["provider"], "native-python")
        self.assertFalse(meta["live_trading"])
        self.assertEqual(meta["cost"], "no AI API fees")

    def test_deterministic_tie_break_and_bounded_shortlist(self):
        from app.opportunity_ranker import rank_candidates
        sample = [{"symbol": "A"}, {"symbol": "B"}] + [{"symbol": str(i)} for i in range(20)]
        ranked, meta = rank_candidates("crypto", sample)
        self.assertEqual([x["symbol"] for x in ranked], [str(i) for i in range(12)])
        self.assertEqual(ranked[0]["symbol"], "0")
        self.assertEqual(meta["ranked_count"], 12)

    def test_protected_open_position_is_not_dropped_from_shortlist(self):
        from app.opportunity_ranker import rank_candidates
        sample = [{"symbol": str(i), "change_pct": 0} for i in range(15)]
        ranked, meta = rank_candidates("crypto", sample, protected_symbols=["14"])
        self.assertIn("14", [item["symbol"] for item in ranked])
        self.assertEqual(len(ranked), 12)
        self.assertFalse(meta["live_trading"])

    def test_missing_fields_and_non_finite_values_are_safe(self):
        from app.opportunity_ranker import rank_candidates
        ranked, meta = rank_candidates("stocks", [
            {"symbol": "NAN", "change_pct": float("nan"), "volume": float("inf"), "candle_count": None}
        ])
        self.assertEqual(len(ranked), 1)
        self.assertTrue(0 <= ranked[0]["opportunity_rank_score"] <= 100)
        self.assertEqual(meta["mode"], "PAPER_ONLY")
        self.assertFalse(meta["live_trading"])
