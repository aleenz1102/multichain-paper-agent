"""Public, unauthenticated Binance spot candle data for paper trading only.

Only completed candles are returned. This module never submits orders.
"""
from datetime import datetime, timezone
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SYMBOL_PAIRS = {
    "SOL": "SOLUSDT",
    "ETH": "ETHUSDT",
    "ARB": "ARBUSDT",
    "BNB": "BNBUSDT",
}


class CandleDataError(RuntimeError):
    """Market data is unavailable, invalid, or too stale to use."""


class BinancePublicCandleProvider:
    def __init__(
        self,
        interval="5m",
        limit=250,
        timeout_seconds=8,
        stale_after_seconds=600,
        opener=urlopen,
    ):
        if interval not in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"}:
            raise ValueError("Unsupported candle interval.")
        if not 30 <= limit <= 1000:
            raise ValueError("limit must be between 30 and 1000.")
        if timeout_seconds <= 0 or stale_after_seconds <= 0:
            raise ValueError("Timeout and freshness thresholds must be positive.")

        self.interval = interval
        self.limit = limit
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.opener = opener

    def get_candles(self, symbol):
        symbol = symbol.upper()
        pair = SYMBOL_PAIRS.get(symbol)
        if not pair:
            # Dynamic universe scanning only proposes spot USDT base assets. Validate
            # strictly before constructing a public read-only market-data request.
            if not re.fullmatch(r"[A-Z0-9]{2,20}", symbol):
                raise CandleDataError(f"Invalid market symbol {symbol!r}.")
            pair = symbol + "USDT"

        params = urlencode({
            "symbol": pair,
            "interval": self.interval,
            "limit": self.limit,
        })
        request = Request(
            "https://data-api.binance.vision/api/v3/klines?" + params,
            headers={
                "Accept": "application/json",
                "User-Agent": "multichain-paper-agent/1.0",
            },
        )

        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            raise CandleDataError(
                f"Public candle provider unavailable: {type(exc).__name__}."
            ) from exc

        if not isinstance(payload, list):
            raise CandleDataError("Candle provider returned an unexpected response.")

        now_ms = int(time.time() * 1000)
        interval_ms = {
            "1m": 60_000, "3m": 180_000, "5m": 300_000,
            "15m": 900_000, "30m": 1_800_000,
            "1h": 3_600_000, "4h": 14_400_000,
            "1d": 86_400_000,
        }[self.interval]
        candles = []
        previous_open = None

        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                raise CandleDataError("Malformed candle row.")

            try:
                open_ms = int(row[0])
                open_price, high, low, close, volume = map(
                    float, (row[1], row[2], row[3], row[4], row[5])
                )
                close_ms = int(row[6])
            except (TypeError, ValueError, OverflowError) as exc:
                raise CandleDataError("Candle contains invalid fields.") from exc

            values = (open_price, high, low, close, volume)
            if not all(math.isfinite(value) for value in values):
                raise CandleDataError("Candle contains non-finite values.")
            if min(open_price, high, low, close) <= 0 or volume < 0:
                raise CandleDataError("Candle prices must be positive and volume non-negative.")
            if high < max(open_price, close, low) or low > min(open_price, close, high):
                raise CandleDataError("Inconsistent candle OHLC values.")
            if close_ms < open_ms:
                raise CandleDataError("Candle close time precedes open time.")
            if previous_open is not None:
                if open_ms <= previous_open:
                    raise CandleDataError("Candles are not strictly chronological.")
                if open_ms - previous_open != interval_ms:
                    raise CandleDataError("Gap detected in candle history.")
            previous_open = open_ms

            # Never give the strategy a candle that has not finished.
            if close_ms >= now_ms:
                continue

            candles.append({
                "timestamp": datetime.fromtimestamp(
                    open_ms / 1000, tz=timezone.utc
                ).isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            })

        if len(candles) < 30:
            raise CandleDataError(
                f"Only {len(candles)} completed candles available; at least 30 required."
            )

        latest_open_ms = int(
            datetime.fromisoformat(candles[-1]["timestamp"]).timestamp() * 1000
        )
        age_ms = now_ms - (latest_open_ms + interval_ms)

        if age_ms < 0 or age_ms > self.stale_after_seconds * 1000:
            raise CandleDataError("Latest completed candle is stale.")

        return candles
