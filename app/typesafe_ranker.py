"""Optional TypeSafe AI ranking for public-data paper-trading candidates.

This module only reorders a bounded candidate shortlist. It never creates a signal,
changes a risk limit, or authorizes an order. A missing key, disabled flag, SDK
failure, or malformed answer falls back to the original deterministic ordering.
"""
from __future__ import annotations

import math
import os
from typing import Any


def status() -> dict[str, Any]:
    key_present = bool(os.getenv("TYPESAFE_API_KEY", "").strip())
    enabled = os.getenv("TYPESAFE_RANKING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not key_present:
        reason = "TYPESAFE_API_KEY is not configured"
    elif not enabled:
        reason = "Set TYPESAFE_RANKING_ENABLED=1 to opt in"
    else:
        try:
            import typesafe_sdk  # noqa: F401
        except ImportError:
            reason = "typesafe-sdk package is not installed"
        else:
            reason = "ready"
    return {
        "provider": "TypeSafe AI",
        "enabled": bool(key_present and enabled and reason == "ready"),
        "key_configured": key_present,
        "status": "ready" if reason == "ready" else "disabled",
        "reason": reason,
        "role": "advisory ranking only; deterministic strategy and risk gates remain authoritative",
        "mode": "PAPER_ONLY",
        "live_trading": False,
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def rank_candidates(market_type: str, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score a maximum of 12 candidate snapshots in one TypeSafe request."""
    original = [dict(item) for item in candidates]
    current = status()
    meta = {
        "provider": "TypeSafe AI",
        "status": current["status"],
        "reason": current["reason"],
        "enabled": current["enabled"],
        "ranked_count": 0,
        "model": None,
        "mode": "PAPER_ONLY",
        "live_trading": False,
    }
    if not current["enabled"] or not original:
        return original, meta

    # Keep requests bounded; the caller already limits the active shortlist.
    original = original[:12]
    try:
        from typesafe_sdk import Score, TypeSafeClient

        state_candidates = []
        for item in original:
            state_candidates.append({
                "symbol": str(item.get("symbol", ""))[:24],
                "market_type": str(item.get("market_type", market_type))[:24],
                "change_pct_24h": _number(item.get("change_pct")),
                "reported_volume": _number(item.get("volume")),
                "data_status": str(item.get("data_status", "unknown"))[:32],
                "history_status": str(item.get("history_status", "unknown"))[:32],
                "candle_count": int(_number(item.get("candle_count")) or 0),
            })
        questions = {}
        for index, item in enumerate(state_candidates):
            questions[f"candidate_{index}"] = Score(
                instructions=(
                    f"Rate candidate {item['symbol']} as a research priority for a "
                    f"{market_type} paper-trading scanner, relative to the other candidates "
                    "in state.candidates. Assess only data quality, liquidity/activity, "
                    "and observed momentum. Do not predict profit, invent facts, or treat "
                    "a price move alone as a buy signal. This is ranking, not order approval."
                ),
                criteria=[
                    "0: unusable or concerning data; do not prioritize",
                    "1: weak activity or weak/unclear momentum; low priority",
                    "2: mixed or ordinary setup; neutral priority",
                    "3: relatively strong activity and coherent observed momentum; higher research priority",
                    "4: strongest well-supported research candidate among this shortlist",
                ],
            )
        with TypeSafeClient(timeout=8.0) as client:
            response = client.system_one(
                state={"market_type": market_type, "candidates": state_candidates},
                questions=questions,
            )
        ranked = []
        for index, item in enumerate(original):
            answer = response.scores.get(f"candidate_{index}")
            score = _number(answer.score) if answer is not None else None
            confidence = _number(answer.confidence) if answer is not None else None
            copy = dict(item)
            if score is None or confidence is None or not (0 <= score <= 4 and 0 <= confidence <= 1):
                raise ValueError("TypeSafe returned a missing or invalid score.")
            copy["typesafe_rank_score"] = round(score, 4)
            copy["typesafe_confidence"] = round(confidence, 4)
            copy["typesafe_model"] = str(response.model)[:80]
            ranked.append((score, confidence, index, copy))
        ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
        meta.update({
            "status": "completed",
            "reason": "Candidates reordered by advisory score; deterministic entry and risk gates unchanged",
            "ranked_count": len(ranked),
            "model": str(response.model)[:80],
        })
        return [row[3] for row in ranked], meta
    except Exception as exc:
        # Never let a semantic service outage stop the deterministic paper engine.
        # Do not expose exception strings, headers, or potentially sensitive request data.
        meta.update({
            "status": "fallback",
            "reason": f"TypeSafe ranking unavailable ({type(exc).__name__}); original order retained",
        })
        return original, meta
