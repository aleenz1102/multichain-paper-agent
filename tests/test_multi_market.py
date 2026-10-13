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


class TypeSafePaperRankingTests(unittest.TestCase):
    def test_missing_key_falls_back_without_sdk_call(self):
        from app import typesafe_ranker
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "", "TYPESAFE_RANKING_ENABLED": "1"}):
            items, meta = typesafe_ranker.rank_candidates("crypto", [{"symbol": "BTC"}])
        self.assertEqual(items[0]["symbol"], "BTC")
        self.assertFalse(meta["enabled"])
        self.assertEqual(meta["status"], "disabled")
        self.assertIn("not configured", meta["reason"])

    def test_disabled_flag_keeps_original_order(self):
        from app import typesafe_ranker
        sample = [{"symbol": "BTC"}, {"symbol": "ETH"}]
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-only", "TYPESAFE_RANKING_ENABLED": "0"}):
            items, meta = typesafe_ranker.rank_candidates("crypto", sample)
        self.assertEqual([item["symbol"] for item in items], ["BTC", "ETH"])
        self.assertFalse(meta["enabled"])
        self.assertFalse(meta["live_trading"])

    def test_enabled_ranking_orders_by_score_and_keeps_advisory_only(self):
        from app import typesafe_ranker

        class Answer:
            def __init__(self, score, confidence):
                self.score = score
                self.confidence = confidence

        class Response:
            model = "jev-test"
            scores = {"candidate_0": Answer(1.0, 0.8), "candidate_1": Answer(3.5, 0.9)}

        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def system_one(self, **kwargs):
                self.request = kwargs
                return Response()

        import typesafe_sdk
        sample = [{"symbol": "BTC", "change_pct": 1.2}, {"symbol": "ETH", "change_pct": 3.4}]
        with patch.dict("os.environ", {"TYPESAFE_API_KEY": "test-only", "TYPESAFE_RANKING_ENABLED": "1"}):
            with patch.object(typesafe_sdk, "TypeSafeClient", FakeClient), patch.object(typesafe_sdk, "Score", side_effect=lambda **kw: kw):
                items, meta = typesafe_ranker.rank_candidates("crypto", sample)
        self.assertEqual([item["symbol"] for item in items], ["ETH", "BTC"])
        self.assertEqual(items[0]["typesafe_rank_score"], 3.5)
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["model"], "jev-test")
        self.assertFalse(meta["live_trading"])
