"""Read-only public-data adapters for multi-market paper trading.

Stocks, selected major USD-quoted forex pairs, commodities, indices and
prediction-market contracts use public endpoints only. No credentials, wallets,
signatures or live order endpoints are used. Unsupported/stale data fails closed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

YAHOO_SYMBOLS = {
    "stocks": {
        "STKAAPL": ("AAPL", "Apple"),
        "STKMSFT": ("MSFT", "Microsoft"),
        "STKNVDA": ("NVDA", "NVIDIA"),
        "STKAMZN": ("AMZN", "Amazon"),
        "STKTSLA": ("TSLA", "Tesla"),
        "STKMETA": ("META", "Meta Platforms"),
        "STKGOOGL": ("GOOGL", "Alphabet"),
    },
    "forex": {
        "FXEURUSD": ("EURUSD=X", "EUR/USD"),
        "FXGBPUSD": ("GBPUSD=X", "GBP/USD"),
        "FXAUDUSD": ("AUDUSD=X", "AUD/USD"),
        "FXNZDUSD": ("NZDUSD=X", "NZD/USD"),
    },
    "commodities": {
        "CMGOLD": ("GC=F", "Gold futures"),
        "CMSILVER": ("SI=F", "Silver futures"),
        "CMWTI": ("CL=F", "WTI crude oil futures"),
        "CMBRENT": ("BZ=F", "Brent crude oil futures"),
        "CMNATGAS": ("NG=F", "Natural gas futures"),
    },
    "indices": {
        "IDXSP500": ("^GSPC", "S&P 500"),
        "IDXNASDAQ": ("^IXIC", "NASDAQ Composite"),
        "IDXDOW": ("^DJI", "Dow Jones Industrial Average"),
        "IDXNIFTY": ("^NSEI", "NIFTY 50"),
        "IDXSENSEX": ("^BSESN", "BSE SENSEX"),
    },
}
MARKET_TYPES = {
    "crypto": {"label": "Crypto · USDT spot", "provider": "Binance public spot", "live": True, "auto_supported": True},
    "stocks": {"label": "Stocks", "provider": "Yahoo Finance public chart", "live": True, "auto_supported": True},
    "forex": {"label": "Forex · USD-quoted majors", "provider": "Yahoo Finance public chart", "live": True, "auto_supported": True},
    "commodities": {"label": "Commodities · futures references", "provider": "Yahoo Finance public chart", "live": True, "auto_supported": True},
    "indices": {"label": "Indices · reference instruments", "provider": "Yahoo Finance public chart", "live": True, "auto_supported": True},
    "prediction": {"label": "Prediction markets", "provider": "Polymarket public Gamma API", "live": True, "auto_supported": False},
}
YAHOO_INTERVAL = "15m"
YAHOO_RANGE = "5d"
FRESH_SECONDS = 180
CACHE_SECONDS = 30


class MarketDataError(RuntimeError):
    """A provider failed, returned invalid data, or has no suitable history."""


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _json(url: str, timeout: float = 7):
    request = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "multichain-paper-agent/1.0",
    })
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload


def _number(value):
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return num if math.isfinite(num) else None


def _prediction_array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


class PublicMultiMarketProvider:
    """TTL-cached data adapters. Tickers are namespaced to prevent collisions."""

    def __init__(self, opener=urlopen, timeout_seconds=7, cache_seconds=CACHE_SECONDS):
        self.opener = opener
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self._lock = Lock()
        self._cache = {}
        self._prediction_by_symbol = {}

    def catalog(self):
        return [{"id": key, **value} for key, value in MARKET_TYPES.items()]

    def _yahoo_one(self, market_type: str, symbol: str, ticker: str, name: str):
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(ticker, safe="") + "?" + urlencode({
            "range": YAHOO_RANGE, "interval": YAHOO_INTERVAL, "includePrePost": "false",
        })
        # Use the injectable opener so unit tests can supply deterministic responses.
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "multichain-paper-agent/1.0"})
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise MarketDataError(f"Public {market_type} provider unavailable ({type(exc).__name__}).") from exc
        chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
        if chart.get("error"):
            raise MarketDataError(f"Provider did not return data for {ticker}.")
        results = chart.get("result") or []
        if not results or not isinstance(results[0], dict):
            raise MarketDataError(f"No public chart data returned for {ticker}.")
        result = results[0]
        meta = result.get("meta") or {}
        quote_blocks = (result.get("indicators") or {}).get("quote") or []
        if not quote_blocks:
            raise MarketDataError(f"Chart history is missing for {ticker}.")
        block = quote_blocks[0]
        timestamps = result.get("timestamp") or []
        candles = []
        opens, highs, lows, closes = (block.get(k) or [] for k in ("open", "high", "low", "close"))
        volumes = block.get("volume") or []
        for i, timestamp in enumerate(timestamps):
            if i >= len(closes):
                break
            close = _number(closes[i])
            op = _number(opens[i]) if i < len(opens) else None
            high = _number(highs[i]) if i < len(highs) else None
            low = _number(lows[i]) if i < len(lows) else None
            volume = _number(volumes[i]) if i < len(volumes) else 0.0
            if close is None or close <= 0:
                continue
            op = op if op and op > 0 else close
            high = high if high and high > 0 else max(op, close)
            low = low if low and low > 0 else min(op, close)
            if high < max(op, close, low) or low > min(op, close, high):
                continue
            candles.append({
                "timestamp": _utc(float(timestamp)), "open": op, "high": high,
                "low": low, "close": close, "volume": max(0.0, volume or 0.0),
            })
        if not candles:
            raise MarketDataError(f"No valid price candles returned for {ticker}.")
        now = time.time()
        newest = datetime.fromisoformat(candles[-1]["timestamp"]).timestamp()
        age = max(0.0, now - newest)
        meta_price = _number(meta.get("regularMarketPrice"))
        price = meta_price if meta_price and meta_price > 0 else candles[-1]["close"]
        previous = _number(meta.get("chartPreviousClose")) or _number(meta.get("previousClose"))
        change = ((price / previous) - 1) * 100 if previous and previous > 0 else None
        return {
            "market_type": market_type, "symbol": symbol, "display_symbol": ticker,
            "name": name, "price": price, "currency": meta.get("currency") or "USD",
            "change_pct": change, "volume": max(0.0, _number(meta.get("regularMarketVolume")) or 0.0),
            "provider": "Yahoo Finance public chart", "fetched_at": _utc(now),
            "data_status": "LIVE PUBLIC PRICE" if age <= FRESH_SECONDS else "STALE",
            "history_status": "fresh" if age <= FRESH_SECONDS else "stale",
            "candle_count": len(candles), "interval": YAHOO_INTERVAL,
            "auto_supported": True, "_candles": candles,
        }

    def _refresh_yahoo(self, market_type: str):
        configs = YAHOO_SYMBOLS.get(market_type)
        if not configs:
            raise MarketDataError("Unsupported market category.")
        results = []
        errors = []
        # Parallel public requests reduce page load time, but every request has a timeout.
        with ThreadPoolExecutor(max_workers=min(5, len(configs))) as pool:
            futures = {pool.submit(self._yahoo_one, market_type, symbol, ticker, name): symbol
                       for symbol, (ticker, name) in configs.items()}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except MarketDataError as exc:
                    errors.append(str(exc))
        if not results:
            raise MarketDataError(errors[0] if errors else "No instruments available.")
        results.sort(key=lambda item: list(configs).index(item["symbol"]))
        return results, "fresh" if not errors else "partial"

    def _refresh_prediction(self):
        url = "https://gamma-api.polymarket.com/markets?" + urlencode({
            "active": "true", "closed": "false", "limit": "40",
            "order": "volume24hr", "ascending": "false",
        })
        try:
            payload = _json(url, self.timeout_seconds)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise MarketDataError(f"Prediction-market provider unavailable ({type(exc).__name__}).") from exc
        if not isinstance(payload, list):
            raise MarketDataError("Prediction-market provider returned an unexpected response.")
        now = time.time()
        rows = []
        token_map = {}
        for market in payload:
            if not isinstance(market, dict) or market.get("closed") or market.get("active") is False:
                continue
            outcomes = _prediction_array(market.get("outcomes"))
            prices = _prediction_array(market.get("outcomePrices"))
            tokens = _prediction_array(market.get("clobTokenIds"))
            condition = str(market.get("conditionId") or market.get("id") or "")
            question = str(market.get("question") or market.get("slug") or "Prediction market").strip()
            if len(outcomes) != 2 or len(prices) != 2 or len(tokens) != 2 or not condition:
                continue
            for idx, outcome in enumerate(outcomes):
                price = _number(prices[idx])
                token_id = str(tokens[idx])
                if price is None or not (0 < price < 1) or not token_id:
                    continue
                digest = hashlib.sha256((condition + ":" + str(outcome)).encode("utf-8")).hexdigest()[:12].upper()
                symbol = "PM" + digest
                row = {
                    "market_type": "prediction", "symbol": symbol,
                    "display_symbol": f"{question[:72]} · {outcome}",
                    "name": f"{question} — {outcome}", "price": price,
                    "currency": "USD", "change_pct": None,
                    "volume": max(0.0, _number(market.get("volume24hr")) or 0.0),
                    "provider": "Polymarket public Gamma API",
                    "fetched_at": _utc(now), "data_status": "LIVE PUBLIC PRICE",
                    "history_status": "unavailable", "candle_count": 0,
                    "interval": None, "auto_supported": False,
                    "probability": price, "outcome": str(outcome),
                }
                rows.append(row)
                token_map[symbol] = {"token_id": token_id, "condition_id": condition, "question": question, "outcome": str(outcome)}
        if not rows:
            raise MarketDataError("No active binary prediction markets with valid outcomes were returned.")
        self._prediction_by_symbol = token_map
        rows.sort(key=lambda item: item["volume"], reverse=True)
        return rows, "fresh"

    def instruments(self, market_type: str, limit: int = 50):
        if market_type not in MARKET_TYPES:
            raise MarketDataError("Unknown market category.")
        limit = max(1, min(int(limit), 100))
        if market_type == "crypto":
            raise MarketDataError("Crypto USDT pairs are provided by the Binance scanner endpoint.")
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(market_type)
            if cached and now - cached[0] < self.cache_seconds:
                rows, status = cached[1], "cached"
            else:
                if market_type == "prediction":
                    rows, status = self._refresh_prediction()
                else:
                    rows, status = self._refresh_yahoo(market_type)
                self._cache[market_type] = (now, rows, status)
            exposed = []
            for row in rows[:limit]:
                exposed.append({k: v for k, v in row.items() if k != "_candles"})
            return {
                "market_type": market_type, "label": MARKET_TYPES[market_type]["label"],
                "provider": MARKET_TYPES[market_type]["provider"], "provider_status": status,
                "scanned_at": _utc(time.time()), "count": len(exposed), "items": exposed,
                "warning": ("Prediction prices represent outcome probabilities between zero and one; no prediction-market historical candle strategy is enabled."
                            if market_type == "prediction" else
                            "Public quotes can be delayed or unavailable. Paper fills are simulations, not executable broker prices."),
            }

    def quote(self, market_type: str, symbol: str):
        symbol = str(symbol).strip().upper()
        if market_type == "prediction":
            # Prediction contracts are refreshed separately due their dynamic universe.
            self.instruments("prediction", 100)
            for row in self._cache.get("prediction", (0, [], ""))[1]:
                if row["symbol"] == symbol:
                    return {k: v for k, v in row.items() if k != "_candles"}
            raise MarketDataError("Selected prediction outcome is no longer in the active public universe.")
        now = time.monotonic()
        cached = self._cache.get(market_type)
        rows = cached[1] if cached and now - cached[0] < self.cache_seconds else None
        if rows is None:
            self.instruments(market_type, 100)
            rows = self._cache.get(market_type, (0, [], ""))[1]
        for row in rows:
            if row["symbol"] == symbol:
                item = {k: v for k, v in row.items() if k != "_candles"}
                if item.get("data_status") != "LIVE PUBLIC PRICE" or not isinstance(item.get("price"), (int, float)):
                    raise MarketDataError("Selected market quote is stale or unavailable.")
                return item
        raise MarketDataError("Unknown instrument for the selected market.")

    def candles(self, market_type: str, symbol: str):
        if market_type == "crypto":
            raise MarketDataError("Crypto candles are supplied by the existing Binance candle provider.")
        if market_type == "prediction":
            raise MarketDataError("Prediction-market historical candles are not enabled; auto-trading is disabled for this market.")
        self.quote(market_type, symbol)
        with self._lock:
            rows = self._cache.get(market_type, (0, [], ""))[1]
            for row in rows:
                if row["symbol"] == symbol:
                    candles = row.get("_candles") or []
                    if len(candles) < 30:
                        raise MarketDataError("At least 30 valid completed candles are required.")
                    newest = datetime.fromisoformat(candles[-1]["timestamp"]).timestamp()
                    if time.time() - newest > FRESH_SECONDS:
                        raise MarketDataError("Latest candle is stale; signal withheld.")
                    return [dict(c) for c in candles]
        raise MarketDataError("Candle history unavailable.")
