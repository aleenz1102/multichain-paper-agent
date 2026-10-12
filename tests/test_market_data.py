import json
import unittest
from unittest.mock import patch

from app.market_data import CoinGeckoFreeProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class MarketDataTests(unittest.TestCase):
    def test_public_price_response_is_cached_within_refresh_interval(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return FakeResponse({"solana": {"inr": 12000.0}})

        provider = CoinGeckoFreeProvider(opener=opener)
        with patch("app.market_data.time.time", return_value=1000.0):
            first = provider.get_prices(["SOL"])
        with patch("app.market_data.time.time", return_value=1010.0):
            second = provider.get_prices(["SOL"])
        self.assertEqual(len(calls), 1)
        self.assertIn("vs_currencies=inr", calls[0])
        self.assertEqual(first["SOL"].price, 12000.0)
        self.assertEqual(second["SOL"].price, 12000.0)
        self.assertEqual(second["SOL"].status, "fresh")
        self.assertEqual(second["SOL"].fetched_at, first["SOL"].fetched_at)

    def test_invalid_nonfinite_and_zero_prices_are_not_cached(self):
        provider = CoinGeckoFreeProvider(opener=lambda *_args, **_kwargs: FakeResponse({
            "solana": {"inr": float("nan")}
        }))
        with patch("app.market_data.time.time", return_value=1000.0):
            result = provider.get_prices(["SOL"])
        self.assertIsNone(result["SOL"].price)
        self.assertEqual(result["SOL"].status, "unavailable")

    def test_unsupported_symbol_is_explicitly_unavailable(self):
        provider = CoinGeckoFreeProvider(opener=lambda *_args, **_kwargs: self.fail("network should not be called"))
        result = provider.get_prices(["NOT_A_REAL_ASSET"])
        self.assertEqual(result["NOT_A_REAL_ASSET"].status, "unavailable")
        self.assertIn("mapping", result["NOT_A_REAL_ASSET"].error)

    def test_rejects_refresh_interval_above_stale_threshold(self):
        with self.assertRaises(ValueError):
            CoinGeckoFreeProvider(stale_after_seconds=10, refresh_interval_seconds=20)


if __name__ == "__main__":
    unittest.main()
