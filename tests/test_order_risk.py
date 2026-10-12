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

    def test_defaults_include_conservative_portfolio_caps(self):
        s = main.settings()
        self.assertEqual(s["max_drawdown_pct"], 10.0)
        self.assertEqual(s["max_exposure_pct"], 60.0)
        self.assertFalse(s["auto_monitor_enabled"])

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
            "virtual_cash": 98000, "position_count": 0
        }):
            with self.assertRaises(HTTPException) as error:
                main.order(main.Order(symbol="SOL", side="BUY", amount=100))
        self.assertIn("drawdown", error.exception.detail.lower())
        self.assertTrue(main.settings()["paused"])

    def test_exposure_cap_blocks_new_order(self):
        main.set_setting("max_exposure_pct", 5.0)
        with patch.object(main, "portfolio", return_value={
            "virtual_topups": 0, "equity": 100000, "exposure": 4900, "positions": [],
            "virtual_cash": 95100, "position_count": 0
        }):
            with self.assertRaises(HTTPException) as error:
                main.order(main.Order(symbol="SOL", side="BUY", amount=200))
        self.assertIn("exposure", error.exception.detail.lower())

    def test_sell_cannot_sell_more_than_held(self):
        main.order(main.Order(symbol="SOL", side="BUY", amount=1000))
        with self.assertRaises(HTTPException) as error:
            main.order(main.Order(symbol="SOL", side="SELL", amount=2000))
        self.assertIn("Sell exceeds holdings", error.exception.detail)


if __name__ == "__main__":
    unittest.main()
