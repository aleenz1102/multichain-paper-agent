"""Free public market-data adapter with caching and explicit freshness metadata.

Provider limits change. This adapter never silently falls back to synthetic values.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time
from threading import Lock
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
    def __init__(self, timeout_seconds=8, stale_after_seconds=180,
                 refresh_interval_seconds=30, opener=urlopen):
        if timeout_seconds <= 0 or stale_after_seconds <= 0 or refresh_interval_seconds <= 0:
            raise ValueError("Timeout and cache-freshness thresholds must be positive.")
        if refresh_interval_seconds > stale_after_seconds:
            raise ValueError("Refresh interval cannot exceed stale threshold.")
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self.opener = opener
        self.cache: dict[str, tuple[float, float]] = {}
        self._lock = Lock()

    @staticmethod
    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    def get_prices(self, symbols: list[str]) -> dict[str, PriceResult]:
        ids = {}
        unsupported = []
        for raw in symbols:
            symbol = str(raw).strip().upper()
            coin_id = COINGECKO_IDS.get(symbol)
            if coin_id:
                ids[symbol] = coin_id
            elif symbol:
                unsupported.append(symbol)

        now = time.time()
        out = {
            symbol: PriceResult(symbol, None, "coingecko", self._iso(now),
                                "unavailable", "No provider mapping configured.")
            for symbol in unsupported
        }
        if not ids:
            return out

        # Avoid hitting the free endpoint on every dashboard poll. Serialize refreshes so
        # simultaneous UI/API calls do not all consume the provider's quota.
        with self._lock:
            stale_symbols = [
                symbol for symbol in ids
                if symbol not in self.cache or now - self.cache[symbol][1] >= self.refresh_interval_seconds
            ]
            error = None
            if stale_symbols:
                params = urlencode({
                    "ids": ",".join(sorted({ids[symbol] for symbol in stale_symbols})),
                    "vs_currencies": "inr",
                })
                req = Request(
                    "https://api.coingecko.com/api/v3/simple/price?" + params,
                    headers={"Accept": "application/json", "User-Agent": "multichain-paper-agent/1.0"},
                )
                try:
                    with self.opener(req, timeout=self.timeout_seconds) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("Unexpected market-data response.")
                    fetched_at = time.time()
                    for symbol in stale_symbols:
                        coin_id = ids[symbol]
                        entry = payload.get(coin_id)
                        price = entry.get("inr") if isinstance(entry, dict) else None
                        if isinstance(price, (int, float)) and math.isfinite(float(price)) and price > 0:
                            self.cache[symbol] = (float(price), fetched_at)
                        else:
                            error = f"Missing or invalid provider price for {symbol}."
                except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                    error = f"Market provider unavailable: {type(exc).__name__}."

            checked_at = time.time()
            for symbol in ids:
                cached = self.cache.get(symbol)
                if not cached:
                    out[symbol] = PriceResult(symbol, None, "coingecko", self._iso(checked_at),
                                              "unavailable", error or "No price returned.")
                    continue
                price, fetched_ts = cached
                age = max(0.0, checked_at - fetched_ts)
                status = "fresh" if age <= self.stale_after_seconds else "stale"
                # Cache hits are still fresh within the explicit TTL. If a refresh attempt
                # failed, allow a previously cached quote only until the stale threshold.
                message = None if status == "fresh" else (error or "Cached price exceeded freshness threshold.")
                out[symbol] = PriceResult(symbol, price, "coingecko", self._iso(fetched_ts), status, message)
        return out
