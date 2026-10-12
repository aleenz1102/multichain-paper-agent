import pytest

from app.backtest import BacktestConfig, run_backtest


def candles(closes):
    return [{"timestamp": str(i), "close": price} for i, price in enumerate(closes)]


def hold(_seen):
    return {"signal": "HOLD"}


@pytest.mark.parametrize("config", [
    BacktestConfig(initial_cash=0),
    BacktestConfig(initial_cash=float("nan")),
    BacktestConfig(fee_bps=-1),
    BacktestConfig(slippage_bps=-1),
    BacktestConfig(fee_bps=10_000),
    BacktestConfig(slippage_bps=10_000),
    BacktestConfig(position_fraction=0),
    BacktestConfig(position_fraction=1.01),
    BacktestConfig(position_fraction=float("inf")),
])
def test_invalid_backtest_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        run_backtest(candles([100, 101]), hold, config)


@pytest.mark.parametrize("bad_close", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_non_positive_or_non_finite_close_is_rejected(bad_close):
    with pytest.raises(ValueError, match="finite and positive"):
        run_backtest(candles([100, bad_close]), hold)


def test_missing_close_is_reported_as_invalid_candle():
    with pytest.raises(ValueError, match="numeric close"):
        run_backtest([{"timestamp": "1", "close": 100}, {"timestamp": "2"}], hold)


def test_hold_strategy_does_not_create_fills():
    result = run_backtest(candles([100, 101, 99, 102]), hold)
    assert result["fill_count"] == 0
    assert result["final_equity"] == result["initial_equity"]
