from __future__ import annotations

import pytest

from koval.strategy.helpers.exits.fixed import fixed_sl_tp


def test_long_sl_below_entry():
    sl, tp = fixed_sl_tp(entry_price=100.0, direction="long", sl_pct=2.0, risk_reward=2.0)
    assert sl == pytest.approx(98.0)


def test_long_tp_above_entry():
    sl, tp = fixed_sl_tp(entry_price=100.0, direction="long", sl_pct=2.0, risk_reward=2.0)
    assert tp == pytest.approx(104.0)


def test_short_sl_above_entry():
    sl, tp = fixed_sl_tp(entry_price=100.0, direction="short", sl_pct=2.0, risk_reward=2.0)
    assert sl == pytest.approx(102.0)


def test_short_tp_below_entry():
    sl, tp = fixed_sl_tp(entry_price=100.0, direction="short", sl_pct=2.0, risk_reward=2.0)
    assert tp == pytest.approx(96.0)


def test_explicit_tp_pct_overrides_risk_reward():
    sl, tp = fixed_sl_tp(
        entry_price=100.0, direction="long", sl_pct=2.0, risk_reward=999.0, tp_pct=5.0
    )
    assert tp == pytest.approx(105.0)


def test_risk_reward_ratio_is_respected():
    sl, tp = fixed_sl_tp(entry_price=100.0, direction="long", sl_pct=1.0, risk_reward=3.0)
    sl_dist = 100.0 - sl
    tp_dist = tp - 100.0
    assert tp_dist / sl_dist == pytest.approx(3.0, rel=1e-6)


def test_returns_tuple_of_two_floats():
    result = fixed_sl_tp(entry_price=50000.0, direction="long", sl_pct=1.5, risk_reward=2.0)
    assert len(result) == 2
    assert all(isinstance(v, float) for v in result)
