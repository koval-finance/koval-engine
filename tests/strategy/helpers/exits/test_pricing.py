import pytest

from koval.strategy.helpers.exits.pricing import (
    liquidity_target,
    structural_sl,
    volatility_sl,
    zone_entry,
)


def test_structural_sl_long_below_nearest_level():
    # entry 100; nearest protective level below at 97 → stop = 97
    assert structural_sl(price_levels=[97.0, 90.0], direction="long", entry=100.0) == 97.0


def test_structural_sl_short_above_nearest_level():
    assert structural_sl(price_levels=[103.0, 110.0], direction="short", entry=100.0) == 103.0


def test_structural_sl_no_valid_level_returns_none():
    # long but all levels are above entry → no protective stop
    assert structural_sl(price_levels=[101.0], direction="long", entry=100.0) is None


def test_volatility_sl_long():
    # entry 100, atr 2, mult 1.5 → 100 - 3 = 97
    assert volatility_sl(entry=100.0, atr=2.0, mult=1.5, direction="long") == 97.0


def test_volatility_sl_short():
    assert volatility_sl(entry=100.0, atr=2.0, mult=1.5, direction="short") == 103.0


def test_liquidity_target_long_nearest_above():
    assert liquidity_target(price_levels=[105.0, 120.0], direction="long", entry=100.0) == 105.0


def test_zone_entry_offset():
    # nearest level 99, long, offset 1% below → 99 * 0.99
    assert zone_entry(price_levels=[99.0], direction="long", offset_pct=1.0) == pytest.approx(98.01)
