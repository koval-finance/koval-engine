import math

import pytest

from koval.strategy.helpers.risk.position_sizer import calculate_position_size


def test_basic_long_size():
    # risk 1% of 10_000 = 100; sl_distance = 100 - 95 = 5; size = 100/5 = 20
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        direction="long",
    )
    assert size == pytest.approx(20.0, rel=1e-6)


def test_basic_short_size():
    # risk 1% of 10_000 = 100; sl_distance = 105 - 100 = 5; size = 100/5 = 20
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=105.0,
        direction="short",
    )
    assert size == pytest.approx(20.0, rel=1e-6)


def test_leverage_does_not_multiply_loss_at_stop():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        direction="long",
        leverage=3.0,
    )
    # Leverage changes required margin, not the quantity whose price-distance
    # loss equals the configured 100-unit risk budget.
    assert size == pytest.approx(20.0, rel=1e-6)
    assert size * (100.0 - 95.0) == pytest.approx(100.0, rel=1e-6)


def test_size_is_capped_by_affordable_notional_at_leverage():
    size = calculate_position_size(
        account_value=1_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=99.9,
        direction="long",
        leverage=2.0,
    )

    assert size == pytest.approx(20.0)
    assert size * 100.0 / 2.0 <= 1_000.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_inputs_fail_closed(value):
    assert (
        calculate_position_size(
            account_value=10_000.0,
            risk_per_trade_pct=1.0,
            entry_price=value,
            stop_loss=95.0,
            direction="long",
        )
        == 0.0
    )
    assert math.isfinite(
        calculate_position_size(
            account_value=10_000.0,
            risk_per_trade_pct=1.0,
            entry_price=100.0,
            stop_loss=95.0,
            direction="long",
        )
    )


def test_zero_account_value_returns_zero():
    size = calculate_position_size(
        account_value=0.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        direction="long",
    )
    assert size == 0.0


def test_sl_on_wrong_side_long_returns_zero():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=105.0,  # above entry for long
        direction="long",
    )
    assert size == 0.0


def test_sl_on_wrong_side_short_returns_zero():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=95.0,  # below entry for short
        direction="short",
    )
    assert size == 0.0


def test_unknown_direction_returns_zero():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=95.0,
        direction="sideways",
    )

    assert size == 0.0


def test_non_positive_stop_returns_zero():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=0.0,
        direction="long",
    )

    assert size == 0.0


def test_zero_sl_distance_returns_zero():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=100.0,
        stop_loss=100.0,
        direction="long",
    )
    assert size == 0.0


def test_negative_entry_price_returns_zero():
    size = calculate_position_size(
        account_value=10_000.0,
        risk_per_trade_pct=1.0,
        entry_price=-1.0,
        stop_loss=95.0,
        direction="long",
    )
    assert size == 0.0


def test_result_is_positive_float():
    size = calculate_position_size(
        account_value=50_000.0,
        risk_per_trade_pct=0.5,
        entry_price=30_000.0,
        stop_loss=29_400.0,
        direction="long",
    )
    assert size > 0.0
    assert isinstance(size, float)


def test_no_bt_imports_in_position_sizer():
    import importlib
    import sys

    mod = sys.modules.get("koval.strategy.helpers.risk.position_sizer") or importlib.import_module(
        "koval.strategy.helpers.risk.position_sizer"
    )
    for name in dir(mod):
        obj = getattr(mod, name)
        assert not str(getattr(obj, "__module__", "")).startswith("backtrader"), (
            f"backtrader symbol '{name}' found in position_sizer"
        )
