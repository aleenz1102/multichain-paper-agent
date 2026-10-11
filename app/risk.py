"""Pure risk checks for a paper-only trading engine.

No networking, exchange integration, or real-order execution lives here.
"""
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class RiskLimits:
    max_trade_pct: float = 5.0
    max_position_pct: float = 15.0
    max_gross_exposure_pct: float = 100.0
    max_net_exposure_pct: float = 75.0
    daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 10.0
    max_positions: int = 6

@dataclass(frozen=True)
class RiskSnapshot:
    equity: float
    order_notional: float
    current_position_count: int
    existing_symbol_position: bool
    gross_exposure: float
    net_exposure: float
    daily_pnl: float
    peak_equity: float
    price_is_fresh: bool = True
    market_is_valid: bool = True
    paused: bool = False

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


def check_entry(snapshot: RiskSnapshot, limits: RiskLimits) -> RiskDecision:
    """Validate a proposed entry; callers must run this immediately before fill."""
    if snapshot.paused:
        return RiskDecision(False, "Trading is paused.")
    if not snapshot.market_is_valid:
        return RiskDecision(False, "Market data is invalid; new entries are blocked.")
    if not snapshot.price_is_fresh:
        return RiskDecision(False, "Market price is stale; new entries are blocked.")
    if snapshot.equity <= 0 or snapshot.order_notional <= 0:
        return RiskDecision(False, "Equity and order notional must be positive.")
    if snapshot.order_notional > snapshot.equity * limits.max_trade_pct / 100:
        return RiskDecision(False, "Order exceeds the per-trade notional limit.")
    if not snapshot.existing_symbol_position and snapshot.current_position_count >= limits.max_positions:
        return RiskDecision(False, "Maximum open-position count reached.")
    if snapshot.gross_exposure + snapshot.order_notional > snapshot.equity * limits.max_gross_exposure_pct / 100:
        return RiskDecision(False, "Order would breach the gross-exposure limit.")
    if abs(snapshot.net_exposure) + snapshot.order_notional > snapshot.equity * limits.max_net_exposure_pct / 100:
        return RiskDecision(False, "Order would breach the net-exposure limit.")
    if snapshot.daily_pnl <= -(snapshot.equity * limits.daily_loss_pct / 100):
        return RiskDecision(False, "Daily loss limit reached.")
    if snapshot.peak_equity > 0 and (snapshot.peak_equity - snapshot.equity) / snapshot.peak_equity * 100 >= limits.max_drawdown_pct:
        return RiskDecision(False, "Maximum drawdown limit reached.")
    return RiskDecision(True, "Entry passed paper risk checks.")
