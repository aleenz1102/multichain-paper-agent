"""Free deterministic candidate ranking for paper-trading scans.

Uses only fields already returned by public market-data adapters. It reorders a
bounded shortlist; it does not create trade signals, estimate profit probabilities,
or alter execution/risk controls.
"""
from __future__ import annotations

import math
from typing import Any


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def rank_candidates(market_type: str, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank up to 12 snapshots; use deterministic tie-breaking by original order."""
    items = [dict(item) for item in candidates[:12]]
    volumes = [_finite_number(item.get("volume")) for item in items]
    valid_volumes = [max(0.0, value) for value in volumes if value is not None]
    max_volume = max(valid_volumes, default=0.0)
    ranked = []

    for index, item in enumerate(items):
        change = _finite_number(item.get("change_pct"))
        volume = _finite_number(item.get("volume"))
        candles = _finite_number(item.get("candle_count"))
        history = str(item.get("history_status", "unknown")).lower()
        data_status = str(item.get("data_status", "unknown")).upper()

        # Momentum favors constructive positive movement but caps extreme moves so
        # a single pump does not dominate the whole ranking.
        momentum_score = _clamp(50.0 + (change or 0.0) * 8.0) if change is not None else 35.0
        # Normalize volume within this scan; logarithms prevent large-cap assets from
        # swamping every other signal solely because their nominal volume is enormous.
        if volume is not None and volume > 0 and max_volume > 0:
            volume_score = _clamp(100.0 * math.log1p(volume) / math.log1p(max_volume))
        else:
            volume_score = 0.0
        fresh = data_status in {"LIVE PUBLIC PRICE", "LIVE", "FRESH"} and history not in {"stale", "expired"}
        quality_score = 100.0 if fresh else (45.0 if history in {"fresh", "ok", "available"} else 0.0)
        history_score = _clamp((candles or 0.0) / 100.0 * 100.0) if candles is not None else 0.0

        score = round(
            momentum_score * 0.40
            + volume_score * 0.25
            + quality_score * 0.20
            + history_score * 0.15,
            2,
        )
        reasons = []
        if change is None:
            reasons.append("24h change unavailable; neutral-low momentum score")
        elif change > 0:
            reasons.append(f"positive reported 24h change ({change:.2f}%)")
        elif change < 0:
            reasons.append(f"negative reported 24h change ({change:.2f}%)")
        else:
            reasons.append("reported 24h change is flat")
        if volume_score >= 70:
            reasons.append("high relative volume within this shortlist")
        elif volume_score < 25:
            reasons.append("low or unavailable relative volume")
        reasons.append("fresh public quote" if fresh else "quote freshness not confirmed")
        if candles is not None and candles >= 30:
            reasons.append("sufficient reported candle history")
        else:
            reasons.append("candle history count is limited or unknown")

        item["opportunity_rank_score"] = score
        item["opportunity_rank_reasons"] = reasons
        ranked.append((score, index, item))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    result = [row[2] for row in ranked]
    meta = {
        "provider": "native-python",
        "status": "completed",
        "reason": "Deterministic snapshot ranking; strategy and risk gates are unchanged",
        "market_type": market_type,
        "ranked_count": len(result),
        "weights": {"momentum": 0.40, "relative_volume": 0.25, "data_quality": 0.20, "history_depth": 0.15},
        "cost": "no AI API fees",
        "mode": "PAPER_ONLY",
        "live_trading": False,
    }
    return result, meta
