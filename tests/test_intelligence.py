import json
import unittest
from io import BytesIO

from app.intelligence import CryptoNewsProvider, TrendingUniverseProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


class IntelligenceTests(unittest.TestCase):
    def test_universe_ranks_liquid_usdt_markets_and_excludes_stablecoins(self):
        payload = [
            {"symbol":"SOLUSDT","lastPrice":"150","quoteVolume":"90000000","priceChangePercent":"8.0","count":"200000","highPrice":"160","lowPrice":"140"},
            {"symbol":"ETHUSDT","lastPrice":"3000","quoteVolume":"80000000","priceChangePercent":"2.0","count":"150000","highPrice":"3100","lowPrice":"2900"},
            {"symbol":"USDCUSDT","lastPrice":"1","quoteVolume":"900000000","priceChangePercent":"0","count":"1000000","highPrice":"1.01","lowPrice":"0.99"},
            {"symbol":"SOLUPUSDT","lastPrice":"20","quoteVolume":"50000000","priceChangePercent":"15","count":"10000","highPrice":"22","lowPrice":"18"},
        ]
        provider = TrendingUniverseProvider(opener=lambda *a, **k: FakeResponse(json.dumps(payload).encode()))
        result = provider.scan(limit=10, min_quote_volume=2_000_000)
        self.assertEqual([x["symbol"] for x in result["items"]], ["SOL", "ETH"])
        self.assertEqual(result["provider_status"], "fresh")
        self.assertGreater(result["items"][0]["trend_score"], result["items"][1]["trend_score"])

    def test_news_sentiment_is_transparent_and_feed_is_cached(self):
        rss = b"""<?xml version="1.0"?><rss><channel>
        <item><title>Ethereum upgrade approval brings adoption growth</title>
        <description>Network integration launches today.</description>
        <link>https://example.com/news/1</link></item>
        </channel></rss>"""
        calls = []
        def opener(*args, **kwargs):
            calls.append(args[0].full_url)
            return FakeResponse(rss)
        provider = CryptoNewsProvider(opener=opener)
        result = provider.fetch(limit=10)
        self.assertGreaterEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["sentiment"]["label"], "positive")
        provider.fetch(limit=10)
        self.assertEqual(len(calls), 2)  # two feeds once; second request uses TTL cache

    def test_empty_feed_is_not_fabricated(self):
        provider = CryptoNewsProvider(opener=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
        result = provider.fetch()
        self.assertEqual(result["provider_status"], "unavailable")
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
