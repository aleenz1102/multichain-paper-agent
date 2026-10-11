"""Small deterministic OHLCV backtest harness for strategy research.

This deliberately simple close-to-next-close model includes fees and slippage and supports
long-only or long/short direction. It is not a substitute for a full execution simulator.
"""
from dataclasses import dataclass
from math import sqrt
from statistics import mean, pstdev
from typing import Callable, Iterable

@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    fee_bps: float = 10.0
    slippage_bps: float = 8.0
    allow_shorts: bool = True
    position_fraction: float = 0.10


def run_backtest(candles: Iterable[dict], signal_fn: Callable[[list], dict], config: BacktestConfig = BacktestConfig()):
    data = list(candles)
    if config.initial_cash <= 0 or not 0 < config.position_fraction <= 1:
        raise ValueError("Invalid initial cash or position fraction.")
    if len(data) < 2:
        raise ValueError("At least two candles are required.")
    closes = [float(c["close"]) for c in data]
    if any(p <= 0 for p in closes):
        raise ValueError("Close prices must be positive.")
    cash = config.initial_cash
    qty = 0.0  # positive long, negative short
    equity_curve = []
    fills = []
    fee_rate = config.fee_bps / 10_000
    slip_rate = config.slippage_bps / 10_000
    for i in range(1, len(data)):
        # Signal sees only candles through i-1; execution uses candle i close.
        decision = signal_fn(data[:i])
        signal = decision.get("signal", "HOLD")
        px = closes[i]
        target = 1 if signal == "BUY" else (-1 if signal == "SELL" and config.allow_shorts else 0)
        current_side = 1 if qty > 0 else (-1 if qty < 0 else 0)
        if target != current_side:
            if qty:
                close_px = px * (1 - slip_rate if qty > 0 else 1 + slip_rate)
                gross = qty * close_px
                fee = abs(gross) * fee_rate
                cash += gross - fee
                fills.append({"index": i, "action": "CLOSE_LONG" if qty > 0 else "COVER_SHORT", "price": close_px, "qty": abs(qty), "fee": fee})
                qty = 0.0
            if target:
                notional = max(0.0, cash) * config.position_fraction
                if notional > 0:
                    open_px = px * (1 + slip_rate if target > 0 else 1 - slip_rate)
                    new_qty = target * notional / open_px
                    fee = notional * fee_rate
                    if target > 0:
                        cash -= abs(new_qty) * open_px + fee
                    else:
                        # Short-sale proceeds credit cash; the negative quantity marks the liability.
                        cash += abs(new_qty) * open_px - fee
                    qty = new_qty
                    fills.append({"index": i, "action": "OPEN_LONG" if target > 0 else "OPEN_SHORT", "price": open_px, "qty": abs(qty), "fee": fee})
        equity_curve.append(cash + qty * px)
    if not equity_curve:
        equity_curve = [config.initial_cash]
    returns = [equity_curve[i] / equity_curve[i-1] - 1 for i in range(1, len(equity_curve)) if equity_curve[i-1] > 0]
    peak = config.initial_cash
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak-value)/peak)
    net = equity_curve[-1] - config.initial_cash
    return {"initial_equity": config.initial_cash, "final_equity": round(equity_curve[-1], 4), "net_pnl": round(net, 4),
            "return_pct": round(net/config.initial_cash*100, 4), "max_drawdown_pct": round(max_dd*100, 4),
            "volatility_per_step_pct": round(pstdev(returns)*100, 4) if len(returns)>1 else 0.0,
            "sharpe_per_step": round(mean(returns)/pstdev(returns)*sqrt(len(returns)), 4) if len(returns)>1 and pstdev(returns)>0 else None,
            "fill_count": len(fills), "fills": fills, "equity_curve": [round(x,4) for x in equity_curve],
            "assumptions": {"fee_bps": config.fee_bps, "slippage_bps": config.slippage_bps, "allow_shorts": config.allow_shorts,
                            "execution": "signal from prior candles, fill at next close; educational approximation"}}
