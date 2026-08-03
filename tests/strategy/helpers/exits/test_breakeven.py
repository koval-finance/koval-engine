from __future__ import annotations

import pytest

from koval.strategy.helpers.exits.breakeven import breakeven_trigger


def test_long_breakeven_triggered_when_price_moved_enough():
    result = breakeven_trigger(
        entry_price=100.0,
        direction="long",
        current_price=106.0,
        stop_loss=95.0,
        trigger_r=1.0,
    )
    assert result == pytest.approx(100.0)


def test_long_breakeven_not_triggered_too_early():
    result = breakeven_trigger(
        entry_price=100.0,
        direction="long",
        current_price=102.0,
        stop_loss=95.0,
        trigger_r=1.0,
    )
    assert result is None


def test_long_breakeven_at_exact_trigger():
    result = breakeven_trigger(
        entry_price=100.0,
        direction="long",
        current_price=105.0,
        stop_loss=95.0,
        trigger_r=1.0,
    )
    assert result == pytest.approx(100.0)


def test_long_breakeven_higher_trigger_r():
    # trigger_r=2.0 needs price >= 110
    result = breakeven_trigger(
        entry_price=100.0,
        direction="long",
        current_price=108.0,
        stop_loss=95.0,
        trigger_r=2.0,
    )
    assert result is None


def test_short_breakeven_triggered_when_price_moved_enough():
    result = breakeven_trigger(
        entry_price=100.0,
        direction="short",
        current_price=94.0,
        stop_loss=105.0,
        trigger_r=1.0,
    )
    assert result == pytest.approx(100.0)


def test_short_breakeven_not_triggered_too_early():
    result = breakeven_trigger(
        entry_price=100.0,
        direction="short",
        current_price=98.0,
        stop_loss=105.0,
        trigger_r=1.0,
    )
    assert result is None


def test_zero_sl_distance_returns_none():
    result = breakeven_trigger(
        entry_price=100.0,
        direction="long",
        current_price=105.0,
        stop_loss=100.0,
        trigger_r=1.0,
    )
    assert result is None
