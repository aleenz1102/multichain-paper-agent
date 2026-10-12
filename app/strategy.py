"""Deterministic, explainable combined strategy and regime classifier.

Input candles are chronological dictionaries containing close and optionally high/low/volume.
This is a research scaffold, not a profitability claim.
"""
from math import isfinite
from statistics import mean, pstdev


def _closes(candles):
    out = [float(c["close"]) for c in candles]
    if any(not isfinite(x) or x <= 0 for x in out):
        raise ValueError("Candle closes must be finite positive prices.")
    return out


def classify_regime(candles, trend_window=20, vol_window=14):
    closes = _closes(candles)
    if len(closes) < max(trend_window, vol_window + 1):
        return {"regime": "uncertain", "reason": "Insufficient candle history."}
    returns = [closes[i] / closes[i-1] - 1 for i in range(len(closes)-vol_window, len(closes))]
    vol = pstdev(returns) if len(returns) > 1 else 0.0
    fast = mean(closes[-5:])
    slow = mean(closes[-trend_window:])
    trend_strength = abs(fast / slow - 1) if slow else 0
    if vol >= 0.04:
        regime = "high_volatility"
    elif trend_strength >= 0.012:
        regime = "trending"
    else:
        regime = "range_bound"
    return {"regime": regime, "return_volatility": vol, "trend_strength": trend_strength}


def combined_signal(candles, min_history=30):
    """Return BUY/SELL/HOLD with transparent component scores and reason."""
    closes = _closes(candles)
    if len(closes) < min_history:
        return {"signal": "HOLD", "score": 0.0, "regime": "uncertain", "reason": "Insufficient candle history."}
    regime_info = classify_regime(candles)
    fast = mean(closes[-5:]); slow = mean(closes[-20:])
    trend = max(-1.0, min(1.0, (fast / slow - 1) / 0.025)) if slow else 0.0
    momentum = max(-1.0, min(1.0, (closes[-1] / closes[-6] - 1) / 0.04))
    recent = closes[-14:]
    avg = mean(recent)
    sd = pstdev(recent)
    z = (closes[-1] - avg) / sd if sd else 0.0
    # Mean reversion is deliberately down-weighted in a trending regime.
    reversion = max(-1.0, min(1.0, -z / 2.0))
    reversion_weight = 0.1 if regime_info["regime"] == "trending" else 0.3
    score = 0.4 * trend + 0.3 * momentum + reversion_weight * reversion
    if regime_info["regime"] == "high_volatility":
        return {"signal": "HOLD", "score": round(score, 4), **regime_info, "components": {"trend": round(trend,4), "momentum": round(momentum,4), "mean_reversion": round(reversion,4)}, "reason": "High-volatility regime blocks new entries."}
    signal = "BUY" if score >= 0.35 else "SELL" if score <= -0.35 else "HOLD"
    return {"signal": signal, "score": round(score, 4), **regime_info, "components": {"trend": round(trend,4), "momentum": round(momentum,4), "mean_reversion": round(reversion,4)}, "reason": f"Combined score {score:.3f} in {regime_info['regime']} regime."}



def build_trade_plan(candles, signal):
    """Create a transparent long-only reference plan; levels are research estimates, not orders."""
    closes = _closes(candles)
    if not closes or signal.get("signal") != "BUY" or signal.get("regime") == "high_volatility":
        return {
            "available": False,
            "reason": "No long entry plan: the current signal is not a qualified BUY.",
            "entry_reference": None, "stop_loss": None, "take_profit_1": None,
            "take_profit_2": None, "risk_reward_1": None, "risk_reward_2": None,
        }
    entry = closes[-1]
    true_ranges = []
    for i in range(max(1, len(candles) - 14), len(candles)):
        c = candles[i]
        try:
            high, low, prev_close = float(c["high"]), float(c["low"]), closes[i - 1]
        except (KeyError, TypeError, ValueError):
            continue
        if not all(isfinite(v) for v in (high, low, prev_close)) or high < low:
            continue
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < 5:
        return {
            "available": False, "reason": "Insufficient valid high/low candles to estimate risk levels.",
            "entry_reference": entry, "stop_loss": None, "take_profit_1": None,
            "take_profit_2": None, "risk_reward_1": None, "risk_reward_2": None,
        }
    atr = sum(true_ranges) / len(true_ranges)
    risk_distance = max(1.5 * atr, entry * 0.005)
    stop = entry - risk_distance
    if stop <= 0:
        return {
            "available": False, "reason": "Risk estimate invalid; entry plan withheld.",
            "entry_reference": entry, "stop_loss": None, "take_profit_1": None,
            "take_profit_2": None, "risk_reward_1": None, "risk_reward_2": None,
        }
    return {
        "available": True,
        "method": "Reference levels: entry at latest completed close; stop at 1.5× recent ATR (minimum 0.5%); targets at 1.5R and 2R.",
        "entry_reference": round(entry, 10),
        "atr_14": round(atr, 10),
        "stop_loss": round(stop, 10),
        "take_profit_1": round(entry + 1.5 * risk_distance, 10),
        "take_profit_2": round(entry + 2.0 * risk_distance, 10),
        "risk_reward_1": 1.5,
        "risk_reward_2": 2.0,
        "risk_distance_pct": round(risk_distance / entry * 100, 3),
    }
