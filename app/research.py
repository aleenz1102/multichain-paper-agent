"""Cost-aware walk-forward research and conservative strategy selection."""
from __future__ import annotations

from math import isfinite
from statistics import mean, pstdev

from app.backtest import BacktestConfig, run_backtest
from app.strategy import combined_signal


def _closes(candles):
    values = [float(c["close"]) for c in candles]
    if any(not isfinite(x) or x <= 0 for x in values):
        raise ValueError("Candle close values must be finite and positive.")
    return values


def trend_signal(candles):
    closes = _closes(candles)
    if len(closes) < 30:
        return {"signal": "HOLD", "reason": "Insufficient history"}
    fast, slow = mean(closes[-5:]), mean(closes[-25:])
    score = max(-1.0, min(1.0, (fast / slow - 1.0) / 0.02))
    return {"signal": "BUY" if score >= 0.3 else "SELL" if score <= -0.3 else "HOLD",
            "score": round(score, 4), "reason": "moving-average trend"}


def momentum_signal(candles):
    closes = _closes(candles)
    if len(closes) < 30:
        return {"signal": "HOLD", "reason": "Insufficient history"}
    returns = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 14, len(closes))]
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    momentum = closes[-1] / closes[-7] - 1
    if volatility >= 0.04:
        return {"signal": "HOLD", "reason": "high-volatility filter"}
    return {"signal": "BUY" if momentum >= 0.025 else "SELL" if momentum <= -0.025 else "HOLD",
            "score": round(momentum, 5), "reason": "six-candle momentum"}


def mean_reversion_signal(candles):
    closes = _closes(candles)
    if len(closes) < 30:
        return {"signal": "HOLD", "reason": "Insufficient history"}
    recent = closes[-20:]
    avg, sd = mean(recent), pstdev(recent)
    z = (closes[-1] - avg) / sd if sd else 0.0
    # Avoid fading a strong directional move.
    trend = mean(closes[-5:]) / mean(closes[-20:]) - 1
    if abs(trend) > 0.025:
        return {"signal": "HOLD", "reason": "trend filter"}
    return {"signal": "BUY" if z <= -1.0 else "SELL" if z >= 1.0 else "HOLD",
            "score": round(z, 4), "reason": "mean reversion z-score"}


CANDIDATES = {
    "combined_signal_v1": combined_signal,
    "trend_v1": trend_signal,
    "momentum_v1": momentum_signal,
    "mean_reversion_v1": mean_reversion_signal,
}


def _run_segment(data, signal_fn, cfg, warmup=()):
    prefix = list(warmup)
    combined = prefix + list(data)
    warmup_count = len(prefix)

    def segment_only_signal(seen):
        if len(seen) <= warmup_count:
            return {"signal": "HOLD", "reason": "segment warm-up"}
        return signal_fn(seen)

    return run_backtest(combined, segment_only_signal, BacktestConfig(**cfg))


def _buy_hold_benchmark(candles, *, initial_cash, fee_bps, slippage_bps, position_fraction):
    """Comparable long-only test-window baseline, including entry/exit costs."""
    closes = _closes(candles)
    if len(closes) < 2:
        return {"return_pct": 0.0, "max_drawdown_pct": 0.0, "fill_count": 0}
    fee = fee_bps / 10_000.0
    slip = slippage_bps / 10_000.0
    allocation = initial_cash * position_fraction
    entry = closes[0] * (1 + slip)
    entry_fee = allocation * fee
    qty = max(0.0, allocation - entry_fee) / entry
    cash = initial_cash - allocation
    equity = []
    for close in closes:
        equity.append(cash + qty * close)
    exit_value = qty * closes[-1] * (1 - slip)
    exit_fee = exit_value * fee
    final_equity = cash + exit_value - exit_fee
    peak = initial_cash
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return {
        "initial_equity": round(initial_cash, 4),
        "final_equity": round(final_equity, 4),
        "net_pnl": round(final_equity - initial_cash, 4),
        "return_pct": round((final_equity / initial_cash - 1) * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "fill_count": 2,
        "assumptions": "Long-only buy-and-hold across the test window; same allocation fraction, entry/exit fees and slippage as the strategy model.",
    }


def walk_forward_report(candles, *, initial_cash=100_000.0, fee_bps=10.0,
                        slippage_bps=8.0, position_fraction=0.10,
                        train_fraction=0.60, validation_fraction=0.20):
    """Compare fixed candidate strategies on train/validation; score selected one on holdout.

    Strategy selection uses validation only. The final test segment is not used to select
    the strategy. This remains an approximate simulator, not a profitability guarantee.
    """
    data = list(candles)
    if len(data) < 120:
        raise ValueError("At least 120 chronological candles are required for three evaluation windows.")
    if not 0.5 <= train_fraction <= 0.8:
        raise ValueError("train_fraction must be between 0.5 and 0.8.")
    if not 0.1 <= validation_fraction <= 0.3:
        raise ValueError("validation_fraction must be between 0.1 and 0.3.")
    if train_fraction + validation_fraction >= 0.95:
        raise ValueError("Reserve at least 5% of candles for the final test window.")
    timestamps = [str(c.get("timestamp", i)) for i, c in enumerate(data)]
    if timestamps != sorted(timestamps):
        raise ValueError("Candles must be chronological before walk-forward evaluation.")
    _closes(data)
    n = len(data)
    a = max(60, int(n * train_fraction))
    b = max(a + 1, int(n * (train_fraction + validation_fraction)))
    if b >= n:
        raise ValueError("Evaluation split leaves no final test window.")
    cfg = dict(initial_cash=initial_cash, fee_bps=fee_bps, slippage_bps=slippage_bps,
               allow_shorts=True, position_fraction=position_fraction)

    training = data[:a]
    validation = data[a:b]
    test = data[b:]
    # The prior 30 candles are lookback only; scored fills begin inside each new window.
    train_results = {}
    validation_results = {}
    for name, signal_fn in CANDIDATES.items():
        train_results[name] = _run_segment(training, signal_fn, cfg)
        validation_results[name] = _run_segment(validation, signal_fn, cfg, warmup=training[-30:])
    # Use validation net return penalized by drawdown, not final test performance.
    def selection_score(result):
        return float(result["return_pct"]) - 0.5 * float(result["max_drawdown_pct"])
    selected = max(CANDIDATES, key=lambda name: (selection_score(validation_results[name]),
                                                  validation_results[name]["return_pct"]))
    test_result = _run_segment(test, CANDIDATES[selected], cfg, warmup=data[b - 30:b])
    benchmark_result = _buy_hold_benchmark(test, initial_cash=initial_cash, fee_bps=fee_bps,
                                           slippage_bps=slippage_bps, position_fraction=position_fraction)

    def compact(result):
        return {key: result[key] for key in ("initial_equity", "final_equity", "net_pnl", "return_pct",
                 "max_drawdown_pct", "volatility_per_step_pct", "sharpe_per_step", "fill_count", "assumptions")}

    return {
        "method": "chronological train / validation / untouched test diagnostic",
        "selection_method": "highest validation return_pct minus 0.5 * max_drawdown_pct",
        "selected_strategy": selected,
        "candidate_scores": {
            name: {"train": compact(train_results[name]), "validation": compact(validation_results[name]),
                   "validation_selection_score": round(selection_score(validation_results[name]), 4)}
            for name in CANDIDATES
        },
        "windows": {
            "train": {"candles": len(training), **compact(train_results[selected])},
            "validation": {"candles": len(validation), **compact(validation_results[selected])},
            "test": {"candles": len(test), **compact(test_result)},
            "test_buy_and_hold_benchmark": {"candles": len(test), **benchmark_result},
        },
        "cost_model": {"fee_bps_per_fill": fee_bps, "slippage_bps_per_fill": slippage_bps},
        "test_is_profitable_after_costs": test_result["net_pnl"] > 0,
        "test_excess_return_vs_buy_and_hold_pct_points": round(test_result["return_pct"] - benchmark_result["return_pct"], 4),
        "test_outperformed_buy_and_hold_after_costs": test_result["return_pct"] > benchmark_result["return_pct"],
        "warning": "Strategy selection is a small heuristic comparison, not machine learning or a guarantee of future returns. The final test window is not used for selection. Compare the selected strategy against the included cost-aware buy-and-hold baseline; a positive return alone is not evidence of edge. Sharpe is per-step, not annualized. Short collateral, funding, liquidation, and realistic execution still require independent validation.",
    }
