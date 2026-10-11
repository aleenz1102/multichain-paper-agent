"""Free-first CoinGecko public market-data adapter with explicit freshness metadata.

Provider usage/rate limits can change. This adapter has no credentials and never silently
falls back to synthetic values. Callers must block new entries when status is not fresh.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

COINGECKO_IDS = {
    "SOL": "solana", "ETH": "ethereum", "ARB": "arbitrum", "BNB": "binancecoin",
    "JUP": "jupiter-exchange-solana", "AERO": "aerodrome-finance", "STRK": "starknet",
    "PENGU": "pudgy-penguins", "TIA": "celestia", "HYPE": "hyperliquid",
    "TRUMP": "official-trump", "USDC": "usd-coin",
}

@dataclass
class PriceResult:
    symbol: str
    price: float | None
    provider: str
    fetched_at: str
    status: str
    error: str | None = None

class CoinGeckoFreeProvider:
    def __init__(self, timeout_seconds=8, stale_after_seconds=180, opener=urlopen):
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.opener = opener
        self.cache: dict[str, tuple[float, float]] = {}

    def get_prices(self, symbols: list[str]) -> dict[str, PriceResult]:
        ids = {}
        unsupported = []
        for symbol in symbols:
            coin_id = COINGECKO_IDS.get(symbol.upper())
            if coin_id: ids[symbol.upper()] = coin_id
            else: unsupported.append(symbol.upper())
        out = {}
        now = time.time()
        for symbol in unsupported:
            out[symbol] = PriceResult(symbol, None, "coingecko", datetime.now(timezone.utc).isoformat(), "unavailable", "No provider mapping configured.")
        if not ids:
            return out
        params = urlencode({"ids": ",".join(sorted(set(ids.values()))), "vs_currencies": "usd"})
        req = Request("https://api.coingecko.com/api/v3/simple/price?" + params, headers={"Accept":"application/json", "User-Agent":"multichain-paper-agent/1.0"})
        error = None
        try:
            with self.opener(req, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for symbol, coin_id in ids.items():
                price = payload.get(coin_id, {}).get("usd")
                if isinstance(price, (int, float)) and price > 0:
                    self.cache[symbol] = (float(price), now)
                else:
                    error = f"Missing or invalid provider price for {symbol}."
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            error = f"Market provider unavailable: {type(exc).__name__}."
        fetched = datetime.now(timezone.utc).isoformat()
        for symbol in ids:
            cached = self.cache.get(symbol)
            if cached:
                price, ts = cached
                age = now - ts
                status = "fresh" if error is None and age <= self.stale_after_seconds else "stale"
                out[symbol] = PriceResult(symbol, price, "coingecko", fetched, status, None if status == "fresh" else error or "Cached price exceeded freshness threshold.")
            else:
                out[symbol] = PriceResult(symbol, None, "coingecko", fetched, "unavailable", error or "No price returned.")
        return out
