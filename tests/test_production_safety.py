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


if __name__ == "__main__":
    unittest.main()
