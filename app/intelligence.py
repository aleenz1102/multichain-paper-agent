"""Free, read-only market universe and RSS news intelligence.

No authenticated endpoints, API keys, trading endpoints, or third-party dependencies.
Public market-data errors fail closed; sentiment is a weak advisory feature, not a
predictive guarantee. Binance documents the public market-data endpoints at
https://developers.binance.com/en/docs/products/spot/rest-api .
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

BINANCE_DATA = "https://data-api.binance.vision"
EXCLUDED_BASES = {
    "USDC", "USDT", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "BUSD",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S")
POSITIVE = {
    "approval", "approved", "adoption", "partnership", "integrates", "integration",
    "launch", "launched", "upgrade", "surge", "inflow", "bullish", "growth",
    "record", "breakthrough", "etf", "mainnet", "funding", "rally",
}
NEGATIVE = {
    "hack", "hacked", "exploit", "exploited", "lawsuit", "sued", "ban", "banned",
    "fraud", "scam", "breach", "outage", "liquidation", "liquidations", "crash",
    "bearish", "investigation", "charges", "stolen", "vulnerability", "warning",
    "withdrawals", "insolvency", "bankruptcy",
}
RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
)
BAD_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")


def _get_json(url: str, timeout: float, opener=urlopen):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "multichain-paper-agent/1.0"})
    with opener(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    return value


class TrendingUniverseProvider:
    """Ranks spot USDT markets by liquidity, momentum, and activity; no orders."""

    def __init__(self, timeout_seconds=8, cache_seconds=60, opener=urlopen):
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.opener = opener
        self._lock = Lock()
        self._cached_at = 0.0
        self._cached = None

    def scan(self, limit=30, min_quote_volume=2_000_000):
        limit = max(1, min(int(limit), 100))
        min_quote_volume = max(0.0, float(min_quote_volume))
        now = time.time()
        with self._lock:
            if self._cached is not None and now - self._cached_at < self.cache_seconds:
                rows = self._cached
                provider_status = "cached"
            else:
                try:
                    payload = _get_json(BINANCE_DATA + "/api/v3/ticker/24hr", self.timeout_seconds, self.opener)
                    if not isinstance(payload, list):
                        raise ValueError("Unexpected Binance ticker response.")
                    rows = []
                    for item in payload:
                        if not isinstance(item, dict):
                            continue
                        pair = str(item.get("symbol", ""))
                        if not pair.endswith("USDT"):
                            continue
                        base = pair[:-4]
                        if not BAD_SYMBOL.fullmatch(base) or base in EXCLUDED_BASES:
                            continue
                        if any(base.endswith(suffix) for suffix in LEVERAGED_SUFFIXES):
                            continue
                        try:
                            last = float(item["lastPrice"])
                            quote_volume = float(item["quoteVolume"])
                            change = float(item["priceChangePercent"])
                            trades = int(item["count"])
                            high = float(item["highPrice"])
                            low = float(item["lowPrice"])
                        except (KeyError, TypeError, ValueError, OverflowError):
                            continue
                        if not all(math.isfinite(x) for x in (last, quote_volume, change, high, low)):
                            continue
                        if last <= 0 or trades < 100 or high < low:
                            continue
                        rows.append({
                            "symbol": base, "pair": pair, "price_usdt": last,
                            "quote_volume_24h_usdt": quote_volume,
                            "change_24h_pct": change, "trades_24h": trades,
                            "range_24h_pct": ((high - low) / last * 100) if last else 0.0,
                        })
                    if not rows:
                        raise ValueError("No markets passed the minimum liquidity/activity filters.")
                    max_volume = max(math.log1p(x["quote_volume_24h_usdt"]) for x in rows) or 1.0
                    for row in rows:
                        liquidity = math.log1p(row["quote_volume_24h_usdt"]) / max_volume
                        momentum = max(-1.0, min(1.0, row["change_24h_pct"] / 12.0))
                        activity = min(1.0, math.log1p(row["trades_24h"]) / math.log1p(max(x["trades_24h"] for x in rows)))
                        # A mild volatility penalty discourages chasing extreme intraday ranges.
                        risk_penalty = min(0.35, max(0.0, row["range_24h_pct"] - 12.0) / 100.0)
                        row["trend_score"] = round(0.45 * liquidity + 0.35 * momentum + 0.20 * activity - risk_penalty, 5)
                    rows.sort(key=lambda x: (x["trend_score"], x["quote_volume_24h_usdt"]), reverse=True)
                    self._cached = rows
                    self._cached_at = now
                    provider_status = "fresh"
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
                    # Do not serve old cached rankings as if they were fresh.
                    if self._cached is None:
                        raise RuntimeError(f"Public universe scan unavailable: {type(exc).__name__}") from exc
                    rows = self._cached
                    provider_status = "stale_cache"
            return {
                "provider": "Binance public spot ticker",
                "provider_status": provider_status,
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "quote": "USDT",
                "minimum_quote_volume_usdt": min_quote_volume,
                "count": min(limit, len([row for row in rows if row["quote_volume_24h_usdt"] >= min_quote_volume])),
                "items": [dict(x) for x in [row for row in rows if row["quote_volume_24h_usdt"] >= min_quote_volume][:limit]],
                "warning": "Trend score is a ranking heuristic, not a probability of profit. Public ticker data is not an execution quote.",
            }


class CryptoNewsProvider:
    """Small TTL-cached RSS reader with explainable lexicon sentiment."""

    def __init__(self, timeout_seconds=8, cache_seconds=300, opener=urlopen):
        self.timeout_seconds = timeout_seconds
        self.cache_seconds = cache_seconds
        self.opener = opener
        self._lock = Lock()
        self._cached_at = 0.0
        self._cached = []

    @staticmethod
    def _clean(value):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()

    @staticmethod
    def _published(value):
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _sentiment(text):
        tokens = re.findall(r"[a-z]{3,}", text.lower())
        pos = sum(t in POSITIVE for t in tokens)
        neg = sum(t in NEGATIVE for t in tokens)
        total = pos + neg
        score = (pos - neg) / total if total else 0.0
        return {"score": round(score, 3), "label": "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral",
                "matched_terms": {"positive": pos, "negative": neg}, "confidence": round(min(1.0, total / 4.0), 2)}

    def fetch(self, limit=40):
        limit = max(1, min(int(limit), 100))
        now = time.time()
        with self._lock:
            if self._cached and now - self._cached_at < self.cache_seconds:
                items = self._cached
                status = "cached"
            else:
                collected, errors = [], 0
                for feed in RSS_FEEDS:
                    try:
                        request = Request(feed, headers={"Accept": "application/rss+xml, application/xml, text/xml", "User-Agent": "multichain-paper-agent/1.0"})
                        with self.opener(request, timeout=self.timeout_seconds) as response:
                            raw = response.read()
                        root = ET.fromstring(raw)
                        nodes = root.findall(".//item")
                        if not nodes:
                            nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")
                        for node in nodes:
                            def child_text(*names):
                                for name in names:
                                    found = node.find(name)
                                    if found is None:
                                        found = node.find("{http://www.w3.org/2005/Atom}" + name)
                                    if found is not None and (found.text or "").strip():
                                        return found.text.strip()
                                return ""
                            title = self._clean(child_text("title"))
                            description = self._clean(child_text("description", "summary", "content"))
                            link = child_text("link")
                            if link and link.startswith("http"):
                                url = link
                            else:
                                url = ""
                                link_node = node.find("{http://www.w3.org/2005/Atom}link")
                                if link_node is not None:
                                    url = link_node.attrib.get("href", "")
                            if not title or not url.startswith("https://"):
                                continue
                            published = self._published(child_text("pubDate", "published", "updated"))
                            if published:
                                try:
                                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(published)).total_seconds()
                                    if age < -300 or age > 72 * 3600:
                                        continue
                                except ValueError:
                                    pass
                            text = title + " " + description
                            collected.append({"title": title[:300], "summary": description[:700], "url": url,
                                              "source": re.sub(r"^https?://", "", feed).split("/")[0],
                                              "published_at": published, "sentiment": self._sentiment(text)})
                    except (HTTPError, URLError, TimeoutError, OSError, ET.ParseError, ValueError):
                        errors += 1
                # Deduplicate by normalized title; do not invent articles if every feed fails.
                seen, unique = set(), []
                for item in collected:
                    key = re.sub(r"[^a-z0-9]", "", item["title"].lower())
                    if key and key not in seen:
                        seen.add(key)
                        unique.append(item)
                unique.sort(key=lambda x: x.get("published_at") or "", reverse=True)
                if unique:
                    self._cached, self._cached_at = unique, now
                    items, status = unique, "partial" if errors else "fresh"
                elif self._cached:
                    items, status = self._cached, "stale_cache"
                else:
                    items, status = [], "unavailable"
            return {"provider": "public RSS feeds", "provider_status": status,
                    "fetched_at": datetime.now(timezone.utc).isoformat(), "count": min(limit, len(items)),
                    "items": [dict(x) for x in items[:limit]],
                    "warning": "Lexicon sentiment is noisy and can misread context, sarcasm, and negation. News is advisory only."}
