from app.research import walk_forward_report
from app.strategy import build_trade_plan


def make_candles(count=180):
    candles = []
    price = 100.0
    for i in range(count):
        # Deterministic, low-volatility rising series with valid OHLC ranges.
        price *= 1.001
        candles.append({
            "timestamp": f"{i:06d}",
            "open": price * 0.999,
            "high": price * 1.004,
            "low": price * 0.996,
            "close": price,
            "volume": 1000.0 + i,
        })
    return candles


def test_trade_plan_is_withheld_for_non_buy_signal():
    plan = build_trade_plan(make_candles(), {"signal": "HOLD", "regime": "trending"})
    assert plan["available"] is False
    assert plan["stop_loss"] is None
    assert plan["take_profit_1"] is None


def test_buy_plan_has_positive_risk_levels_and_reward_risk():
    plan = build_trade_plan(make_candles(), {"signal": "BUY", "regime": "trending"})
    assert plan["available"] is True
    assert plan["stop_loss"] < plan["entry_reference"]
    assert plan["take_profit_1"] > plan["entry_reference"]
    assert plan["take_profit_2"] > plan["take_profit_1"]
    assert plan["risk_reward_1"] == 1.5
    assert plan["risk_reward_2"] == 2.0


def test_walk_forward_reports_cost_aware_holdout_benchmark():
    report = walk_forward_report(make_candles(), fee_bps=10, slippage_bps=8)
    test_window = report["windows"]["test"]
    benchmark = report["windows"]["test_buy_and_hold_benchmark"]
    assert benchmark["candles"] == test_window["candles"]
    assert benchmark["fill_count"] == 2
    assert "test_excess_return_vs_buy_and_hold_pct_points" in report
    assert isinstance(report["test_outperformed_buy_and_hold_after_costs"], bool)
    assert "untouched test" in report["method"].lower()
