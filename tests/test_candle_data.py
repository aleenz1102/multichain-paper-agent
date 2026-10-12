import json
import unittest
from unittest.mock import patch

from app.candle_data import (
    BinancePublicCandleProvider,
    CandleDataError,
)


NOW_MS = 1_800_000_000_000
INTERVAL_MS = 300_000


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def make_rows(count=40, now_ms=NOW_MS):
    # Create completed, chronological 5-minute candles ending recently.
    last_open = now_ms - 2 * INTERVAL_MS
    rows = []
    for i in range(count):
        open_ms = last_open - (count - 1 - i) * INTERVAL_MS
        price = 100.0 + i
        rows.append([
            open_ms,
            str(price),
            str(price + 2),
            str(price - 1),
            str(price + 1),
            "12.5",
            open_ms + INTERVAL_MS - 1,
        ])
    return rows


class CandleProviderTests(unittest.TestCase):
    def provider(self, rows):
        def opener(request, timeout):
            self.assertIn("/api/v3/klines?", request.full_url)
            self.assertEqual(timeout, 8)
            return FakeResponse(rows)

        return BinancePublicCandleProvider(opener=opener)

    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_returns_completed_candles_in_order(self, _clock):
        candles = self.provider(make_rows()).get_candles("SOL")
        self.assertEqual(len(candles), 40)
        self.assertLess(candles[0]["timestamp"], candles[-1]["timestamp"])
        self.assertEqual(candles[-1]["close"], 140.0)
        self.assertIn("volume", candles[-1])

    def test_rejects_unsupported_symbol(self):
        with self.assertRaises(CandleDataError):
            self.provider(make_rows()).get_candles("UNKNOWN")

    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_rejects_malformed_ohlc(self, _clock):
        rows = make_rows()
        rows[5][2] = "1"  # High below the candle's open/close.
        with self.assertRaises(CandleDataError):
            self.provider(rows).get_candles("SOL")

    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_rejects_duplicate_or_out_of_order_candles(self, _clock):
        rows = make_rows()
        rows[10] = rows[9]
        with self.assertRaises(CandleDataError):
            self.provider(rows).get_candles("SOL")

    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_rejects_insufficient_completed_history(self, _clock):
        with self.assertRaises(CandleDataError):
            self.provider(make_rows(29)).get_candles("SOL")

    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_rejects_stale_candles(self, _clock):
        rows = make_rows(now_ms=NOW_MS - 3_600_000)
        with self.assertRaises(CandleDataError):
            self.provider(rows).get_candles("SOL")


    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_returns_current_forming_candle_for_display_only(self, _clock):
        open_ms = NOW_MS - INTERVAL_MS // 2
        row = [open_ms, "100", "102", "99", "101", "12.5", NOW_MS + 1000]

        def opener(request, timeout):
            self.assertIn("limit=1", request.full_url)
            return FakeResponse([row])

        candle = BinancePublicCandleProvider(opener=opener).get_forming_candle("SOL")
        self.assertTrue(candle["forming"])
        self.assertEqual(candle["close"], 101.0)
        self.assertIn("timestamp", candle)

    def test_rejects_invalid_provider_configuration(self):
        with self.assertRaises(ValueError):
            BinancePublicCandleProvider(limit=10)
        with self.assertRaises(ValueError):
            BinancePublicCandleProvider(interval="unsupported")


    @patch("app.candle_data.time.time", return_value=NOW_MS / 1000)
    def test_rejects_gaps_in_candle_history(self, _clock):
        rows = make_rows()
        rows[15][0] += INTERVAL_MS
        rows[15][6] += INTERVAL_MS

        with self.assertRaises(CandleDataError):
            self.provider(rows).get_candles("SOL")


if __name__ == "__main__":
    unittest.main()
