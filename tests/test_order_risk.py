import unittest
from unittest.mock import patch
from fastapi import HTTPException
from app import main


class OrderRiskTests(unittest.TestCase):
    def setUp(self):
        self.original_db = main.DB_PATH
        self.tmp = __import__("tempfile").TemporaryDirectory()
        main.DB_PATH = __import__("pathlib").Path(self.tmp.name) / "test-paper.db"
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.original_db
        self.tmp.cleanup()

    def test_app_startup_and_shutdown_manage_scheduler(self):
        main.startup()
        self.assertIsNotNone(main._auto_scheduler)
        self.assertTrue(main._auto_scheduler.running)
        main.shutdown()
        self.assertFalse(main._auto_scheduler.running)

    def test_live_market_provider_failure_returns_503(self):
        with patch.object(main.live_provider, "get_prices", side_effect=RuntimeError("provider detail")):
            with self.assertRaises(HTTPException) as error:
                main.get_live_market("SOL")
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("provider detail", str(error.exception.detail))

    def test_missing_live_quote_blocks_order(self):
        main.set_setting("market_data_mode", "live_public")
        with patch.object(main.live_provider, "get_prices", return_value={}):
            with self.assertRaises(HTTPException) as error:
                main.order(main.Order(symbol="SOL", side="BUY", amount=100))
        self.assertEqual(error.exception.status_code, 503)
        self.assertIn("fresh price", error.exception.detail.lower())

    def test_ai_status_never_exposes_api_key(self):
        import os
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-secret"}, clear=False):
            status = main.ai_status()
        self.assertTrue(status["enabled"])
        self.assertFalse(status["key_exposed"])
        self.assertFalse(status["can_execute_orders"])
        self.assertNotIn("not-a-real-secret", str(status))

    def test_ai_analysis_requires_server_environment_key(self):
        import os
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as error:
                main.ai_analyze(main.AIAnalysisInput())
        self.assertEqual(error.exception.status_code, 503)

    def test_defaults_include_conservative_portfolio_caps(self):
        s = main.settings()
        self.assertEqual(s["max_drawdown_pct"], 10.0)
        self.assertEqual(s["max_exposure_pct"], 60.0)
        self.assertFalse(s["auto_monitor_enabled"])

    def test_automation_requires_public_price_mode(self):
        with self.assertRaises(HTTPException) as error:
            main.update_settings(main.SettingsUpdate(auto_monitor_enabled=True))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("Public market prices", error.exception.detail)

    def test_portfolio_tracks_peak_equity_for_drawdown(self):
        main.set_setting("high_water_equity", 120000)
        with patch.object(main, "market", return_value=[]):
            with patch.object(main, "open_positions", return_value=[]):
                value = main.portfolio()
        self.assertGreaterEqual(value["high_water_equity"], 120000)
        self.assertGreaterEqual(value["drawdown_pct"], 0)

    def test_new_risk_limits_are_validated(self):
        self.assertEqual(main.SettingsUpdate(max_drawdown_pct=5, max_exposure_pct=50).max_exposure_pct, 50)
        with self.assertRaises(Exception):
            main.SettingsUpdate(max_exposure_pct=0)
        with self.assertRaises(Exception):
            main.SettingsUpdate(max_drawdown_pct=101)

    def test_drawdown_circuit_breaker_pauses_trading(self):
        main.set_setting("max_drawdown_pct", 1.0)
        with patch.object(main, "portfolio", return_value={
            "virtual_topups": 0, "equity": 98000, "exposure": 0, "positions": [],
            "virtual_cash": 98000, "position_count": 0,
            "valuation_complete": True, "high_water_equity": 100000, "drawdown_pct": 2.0
        }):
            with self.assertRaises(HTTPException) as error:
                main.order(main.Order(symbol="SOL", side="BUY", amount=100))
        self.assertIn("drawdown", error.exception.detail.lower())
        self.assertEqual(main.settings()["risk_lockout_reason"], "max_drawdown")
        self.assertFalse(main.settings()["paused"])

    def test_drawdown_lockout_blocks_buy_but_allows_sell_exit(self):
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        main.set_setting("max_drawdown_pct", 0.001)
        with self.assertRaises(HTTPException) as error:
            main.order(main.Order(symbol="SOL", side="BUY", amount=100))
        self.assertIn("drawdown", error.exception.detail.lower())
        self.assertEqual(main.settings()["risk_lockout_reason"], "max_drawdown")
        portfolio = main.portfolio()
        position = next(p for p in portfolio["positions"] if p["symbol"] == "SOL")
        sold = main.order(main.Order(
            symbol="SOL", side="SELL", amount=position["market_value"], sell_percent=100
        ))
        self.assertTrue(sold["ok"])
        self.assertEqual(main.portfolio()["position_count"], 0)

    def test_exposure_cap_blocks_new_order(self):
        main.set_setting("max_exposure_pct", 5.0)
        with patch.object(main, "portfolio", return_value={
            "virtual_topups": 0, "equity": 100000, "exposure": 4900, "positions": [],
            "virtual_cash": 95100, "position_count": 0
        }):
            with self.assertRaises(HTTPException) as error:
                main.order(main.Order(symbol="SOL", side="BUY", amount=200))
        self.assertIn("exposure", error.exception.detail.lower())

    def test_missing_quote_marks_valuation_incomplete(self):
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        main.set_setting("market_data_mode", "live_public")
        with patch.object(main.live_provider, "get_prices", return_value={}):
            valuation = main.portfolio()
        self.assertFalse(valuation["valuation_complete"])
        self.assertIn("SOL", valuation["unpriced_positions"])
        self.assertIsNone(valuation["positions"][0]["mark_price"])

    def test_sell_exit_not_blocked_by_buy_allocation_cap(self):
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        main.set_setting("max_trade_pct", 1.0)
        with patch.object(main, "market", wraps=main.market):
            valuation = main.portfolio()
            position = next(x for x in valuation["positions"] if x["symbol"] == "SOL")
            response = main.order(main.Order(symbol="SOL", side="SELL", amount=position["market_value"], sell_percent=100))
        self.assertTrue(response["ok"])
        self.assertEqual(main.portfolio()["position_count"], 0)

    def test_sell_cannot_sell_more_than_held(self):
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        with self.assertRaises(HTTPException) as error:
            main.order(main.Order(symbol="SOL", side="SELL", amount=2000))
        self.assertIn("Sell exceeds holdings", error.exception.detail)


    def test_minimum_virtual_cash_and_settings_after_first_trade(self):
        updated = main.update_settings(main.SettingsUpdate(virtual_cash=1))
        self.assertEqual(updated["virtual_cash"], 1)
        # Small virtual balances are allowed; no real balance is involved.
        main.set_setting("virtual_cash", 100000)
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        with self.assertRaises(HTTPException) as error:
            main.update_settings(main.SettingsUpdate(virtual_cash=99999))
        self.assertIn("cannot be changed", error.exception.detail.lower())
        # Sending the unchanged starting balance with other settings is allowed.
        updated = main.update_settings(main.SettingsUpdate(virtual_cash=100000, max_trade_pct=5))
        self.assertEqual(updated["max_trade_pct"], 5)

    def test_daily_loss_lockout_allows_sell_exit(self):
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        main.set_setting("daily_loss_limit_pct", 0.01)
        main.set_setting("risk_lockout_reason", "daily_loss")
        main.set_setting("risk_lockout_date", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat())
        portfolio = main.portfolio()
        position = next(p for p in portfolio["positions"] if p["symbol"] == "SOL")
        result = main.order(main.Order(symbol="SOL", side="SELL", amount=position["market_value"], sell_percent=100))
        self.assertTrue(result["ok"])
        self.assertEqual(main.portfolio()["position_count"], 0)


if __name__ == "__main__":
    unittest.main()
