from __future__ import annotations

import pytest

from koval.strategy.helpers.exits.trailing import trailing_stop_price


def test_long_trail_moves_up_with_price():
    # Price moved to 110; trail 5% → new stop = 110 * 0.95 = 104.5
    new_stop = trailing_stop_price(
        direction="long",
        current_price=110.0,
        trail_pct=5.0,
        current_stop=90.0,
    )
    assert new_stop == pytest.approx(104.5)


def test_long_trail_does_not_move_down():
    # Price dropped; stop stays at current (higher) level
    new_stop = trailing_stop_price(
        direction="long",
        current_price=105.0,
        trail_pct=5.0,
        current_stop=104.5,
    )
    assert new_stop == pytest.approx(104.5)


def test_long_trail_initial_stop():
    new_stop = trailing_stop_price(
        direction="long",
        current_price=100.0,
        trail_pct=3.0,
        current_stop=0.0,
    )
    assert new_stop == pytest.approx(97.0)


def test_short_trail_moves_down_with_price():
    # Price fell to 90; trail 5% → new stop = 90 * 1.05 = 94.5
    new_stop = trailing_stop_price(
        direction="short",
        current_price=90.0,
        trail_pct=5.0,
        current_stop=110.0,
    )
    assert new_stop == pytest.approx(94.5)


def test_short_trail_does_not_move_up():
    # Price rose; stop stays at current (lower) level
    new_stop = trailing_stop_price(
        direction="short",
        current_price=92.0,
        trail_pct=5.0,
        current_stop=94.5,
    )
    assert new_stop == pytest.approx(94.5)


def test_short_trail_initial_stop():
    new_stop = trailing_stop_price(
        direction="short",
        current_price=100.0,
        trail_pct=3.0,
        current_stop=0.0,
    )
    assert new_stop == pytest.approx(103.0)


def test_returns_float():
    result = trailing_stop_price(
        direction="long",
        current_price=100.0,
        trail_pct=2.0,
        current_stop=95.0,
    )
    assert isinstance(result, float)
