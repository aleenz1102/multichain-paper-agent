import unittest
from unittest.mock import patch
from fastapi import HTTPException
from app import main


class ProductionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.original_db = main.DB_PATH
        self.tmp = __import__("tempfile").TemporaryDirectory()
        main.DB_PATH = __import__("pathlib").Path(self.tmp.name) / "test-paper.db"
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.original_db
        self.tmp.cleanup()

    def test_health_and_responses_are_marked_paper_only(self):
        health = main.health()
        self.assertTrue(health["ok"])
        self.assertFalse(health["live_trading"])
        self.assertEqual(health["mode"], "PAPER_ONLY")

    def test_reset_clears_simulation_but_preserves_settings_and_disables_automation(self):
        main.set_setting("max_trade_pct", 4.0)
        main.set_setting("auto_monitor_enabled", True)
        main.order(main.Order(symbol="SOL", side="BUY", amount=100))
        before_settings = main.settings()
        result = main.reset()
        after = main.settings()
        self.assertTrue(result["ok"])
        self.assertEqual(after["max_trade_pct"], 4.0)
        self.assertEqual(after["virtual_cash"], before_settings["virtual_cash"])
        self.assertFalse(after["auto_monitor_enabled"])
        self.assertEqual(main.portfolio()["position_count"], 0)

    def test_empty_live_market_symbols_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            main.get_live_market(" , ")
        self.assertEqual(error.exception.status_code, 400)

    def test_universe_limits_are_bounded(self):
        with self.assertRaises(HTTPException) as error:
            main.intelligence_universe(limit=101)
        self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            main.intelligence_universe(min_quote_volume_usdt=float("nan"))

    def test_news_limit_is_bounded(self):
        with self.assertRaises(HTTPException):
            main.intelligence_news(limit=1000)

    def test_candidate_limit_is_bounded(self):
        with self.assertRaises(HTTPException):
            main.intelligence_candidates(limit=100)

    def test_non_finite_order_and_settings_values_are_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            main.Order(symbol="SOL", side="BUY", amount=float("nan"))
        with self.assertRaises(ValidationError):
            main.Order(symbol="SOL", side="BUY", amount=float("inf"))
        with self.assertRaises(ValidationError):
            main.SettingsUpdate(max_trade_pct=float("nan"))
        with self.assertRaises(ValidationError):
            main.SettingsUpdate(max_drawdown_pct=float("inf"))

    def test_automated_trading_refuses_synthetic_mode(self):
        main.set_setting("auto_monitor_enabled", True)
        main.set_setting("market_data_mode", "synthetic")
        with self.assertRaises(HTTPException) as error:
            main.auto_trade_run(main.AutoMonitorEvaluation())
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("Public market prices", str(error.exception.detail))

    def test_paused_account_rejects_orders(self):
        main.set_setting("paused", True)
        with self.assertRaises(HTTPException) as error:
            main.order(main.Order(symbol="SOL", side="BUY", amount=10))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("paused", str(error.exception.detail).lower())

    def test_buy_is_blocked_when_portfolio_exposure_limit_would_be_breached(self):
        main.set_setting("max_exposure_pct", 1.0)
        with self.assertRaises(HTTPException) as error:
            main.order(main.Order(symbol="SOL", side="BUY", amount=2000))
        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("exposure limit", str(error.exception.detail).lower())

    def test_order_symbol_shape_is_validated_before_execution(self):
        from pydantic import ValidationError
        for symbol in (" sol", "SOLUSDT!", "S", "THIS_SYMBOL_IS_TOO_LONG_FOR_VALIDATION"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValidationError):
                    main.Order(symbol=symbol, side="BUY", amount=10)


if __name__ == "__main__":
    unittest.main()
