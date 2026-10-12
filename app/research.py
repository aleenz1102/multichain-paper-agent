"""Out-of-sample evaluation helpers for the paper-trading research loop."""
from __future__ import annotations

from app.backtest import BacktestConfig, run_backtest
from app.strategy import combined_signal


def walk_forward_report(candles, *, initial_cash=100_000.0, fee_bps=10.0,
                        slippage_bps=8.0, position_fraction=0.10,
                        train_fraction=0.60, validation_fraction=0.20):
    """Report separate chronological train/validation/test windows.

    This is a diagnostic, not an optimizer: the current strategy is fixed, so no
    parameter selection occurs on the held-out test segment.
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
    closes = [float(c["close"]) for c in data]
    if any(not (p > 0 and p < float("inf")) for p in closes):
        raise ValueError("Candle close values must be finite and positive.")
    n = len(data)
    a = max(60, int(n * train_fraction))
    b = max(a + 1, int(n * (train_fraction + validation_fraction)))
    if b >= n:
        raise ValueError("Evaluation split leaves no final test window.")
    cfg = dict(initial_cash=initial_cash, fee_bps=fee_bps, slippage_bps=slippage_bps,
               allow_shorts=True, position_fraction=position_fraction)
    results = {}
    for name, segment in (("train", data[:a]), ("validation", data[a:b]), ("test", data[b:])):
        # Keep the model's minimum lookback available while only scoring trades inside each segment.
        warmup = data[max(0, (0 if name == "train" else (a if name == "validation" else b)) - 30):
                      (0 if name == "train" else (a if name == "validation" else b))]
        combined = list(warmup) + list(segment)
        result = run_backtest(combined, combined_signal, BacktestConfig(**cfg))
        results[name] = {"candles": len(segment), **result}
    return {
        "method": "chronological train / validation / untouched test diagnostic",
        "strategy": "combined_signal_v1_fixed",
        "cost_model": {"fee_bps_per_fill": fee_bps, "slippage_bps_per_fill": slippage_bps},
        "windows": results,
        "test_is_profitable_after_costs": results["test"]["net_pnl"] > 0,
        "warning": "This is not a guarantee of future returns. Current backtest mechanics are approximate; independently audit short collateral, liquidation, funding, and exact segment fill accounting before treating results as production-grade.",
    }
