from __future__ import annotations

import numpy as np
import pytest

from koval.strategy.registry import (
    BLOCK_CATALOG,
    BlockSpec,
    get_all_blocks,
    get_block,
    register_block,
)


def test_catalog_contains_all_expected_types():
    expected = {
        "signal.bos",
        "signal.choch",
        "signal.fvg",
        "signal.ob",
        "signal.hammer",
        "signal.shooting_star",
        "signal.bullish_engulfing",
        "signal.bearish_engulfing",
        "signal.every_bar",
        "signal.ema_cross",
        "signal.rsi_cross",
        "filter.rsi",
        "filter.stoch",
        "filter.macd",
        "filter.adx",
        "filter.ema_trend",
        "filter.atr_volatility",
        "filter.bb_volatility",
        "filter.is_doji",
        "entry.long_only",
        "entry.short_only",
        "entry.both",
        "exit.fixed_sl_tp",
        "exit.trailing_stop",
        "exit.breakeven",
        "risk.pct_risk",
    }
    assert expected.issubset(set(BLOCK_CATALOG.keys()))


def test_get_block_returns_spec():
    spec = get_block("signal.ema_cross")
    assert isinstance(spec, BlockSpec)
    assert spec.category == "signal"


def test_get_block_unknown_raises():
    with pytest.raises(KeyError):
        get_block("signal.does_not_exist")


def test_get_all_blocks_non_empty():
    all_blocks = get_all_blocks()
    assert len(all_blocks) >= 25
    assert all(isinstance(b, BlockSpec) for b in all_blocks)


def test_register_block_then_lookup():
    from pydantic import BaseModel

    class P(BaseModel):
        x: int = 1

    spec = BlockSpec(
        type="test.tmp",
        category="filter",
        display_name="tmp",
        description="x",
        params_schema=P,
        factory=lambda p: lambda s: True,
    )
    register_block(spec)
    try:
        assert get_block("test.tmp") is spec
    finally:
        del BLOCK_CATALOG["test.tmp"]


def test_register_block_rejects_duplicate():
    from pydantic import BaseModel

    class P(BaseModel):
        pass

    spec = BlockSpec(
        type="signal.ema_cross",
        category="signal",
        display_name="x",
        description="x",
        params_schema=P,
        factory=lambda p: lambda s: "none",
    )
    with pytest.raises(ValueError):
        register_block(spec)


# ---- Factory-bound behaviour: ensure each factory wires the helper correctly ----


class _Stub:
    """Fake DeclarativeStrategy with just enough attributes for binding tests."""

    def __init__(self, **kw):
        self.closes = kw.get("closes")
        self.highs = kw.get("highs")
        self.lows = kw.get("lows")
        self.opens = kw.get("opens")
        self.volumes = kw.get("volumes")
        self.close = kw.get("close", 100.0)
        self.high = kw.get("high", 101.0)
        self.low = kw.get("low", 99.0)
        self.open = kw.get("open", 100.5)
        self.account_value = kw.get("account_value", 10_000.0)


def test_signal_ema_cross_returns_string_literals():
    spec = get_block("signal.ema_cross")
    bound = spec.factory(spec.params_schema())
    closes = np.linspace(100.0, 120.0, 50)
    out = bound(_Stub(closes=closes))
    assert out in ("bullish", "bearish", "none")


def test_signal_ema_cross_no_array_returns_none():
    spec = get_block("signal.ema_cross")
    bound = spec.factory(spec.params_schema())
    assert bound(_Stub(closes=None)) == "none"


def test_signal_every_bar_returns_configured_direction():
    spec = get_block("signal.every_bar")
    bullish = spec.factory(spec.params_schema(direction="bullish"))
    bearish = spec.factory(spec.params_schema(direction="bearish"))

    assert bullish(_Stub()) == "bullish"
    assert bearish(_Stub()) == "bearish"


def test_filter_rsi_returns_bool():
    spec = get_block("filter.rsi")
    bound = spec.factory(spec.params_schema())
    closes = np.linspace(100.0, 110.0, 30)
    assert isinstance(bound(_Stub(closes=closes)), bool)


def test_filter_is_doji_uses_current_bar_scalars():
    spec = get_block("filter.is_doji")
    bound = spec.factory(spec.params_schema())
    s = _Stub(open=100.0, high=105.0, low=95.0, close=100.05)
    assert bound(s) is True


def test_exit_fixed_sl_tp_returns_sl_tp_pair():
    spec = get_block("exit.fixed_sl_tp")
    bound = spec.factory(spec.params_schema())
    sl, tp = bound(_Stub(), "long", 100.0)
    assert sl < 100.0
    assert tp > 100.0


def test_risk_pct_returns_size():
    spec = get_block("risk.pct_risk")
    bound = spec.factory(spec.params_schema(risk_pct=1.0))
    size = bound(_Stub(account_value=10_000.0), 100.0, 95.0, "long")
    # 1% of 10k = 100; SL distance 5 → size = 100/5 = 20
    assert size == pytest.approx(20.0)


def test_dynamic_exit_trailing_stop_returns_sl_or_none():
    spec = get_block("exit.trailing_stop")
    bound = spec.factory(spec.params_schema(trail_pct=2.0))
    s = _Stub(close=110.0)
    new_sl = bound(s, trade_id=1, direction="long", entry_price=100.0, current_stop=95.0)
    assert new_sl is None or isinstance(new_sl, float)


def test_dynamic_exit_breakeven_returns_entry_when_triggered():
    spec = get_block("exit.breakeven")
    bound = spec.factory(spec.params_schema(trigger_r=1.0))
    # entry=100, SL=95 → risk=5; current=106 → triggered
    out = bound(
        _Stub(close=106.0),
        trade_id=1,
        direction="long",
        entry_price=100.0,
        current_stop=95.0,
    )
    assert out == pytest.approx(100.0)


def test_entry_long_only_returns_entry_config():
    from koval.strategy.base.trade_setup import EntryConfig

    spec = get_block("entry.long_only")
    cfg = spec.factory(spec.params_schema(entry_type="market"))
    assert isinstance(cfg, EntryConfig)
    assert cfg.allow_long is True
    assert cfg.allow_short is False
    assert cfg.entry_type == "market"


def test_entry_short_only_returns_entry_config():
    from koval.strategy.base.trade_setup import EntryConfig

    spec = get_block("entry.short_only")
    cfg = spec.factory(spec.params_schema(entry_type="limit"))
    assert isinstance(cfg, EntryConfig)
    assert cfg.allow_long is False
    assert cfg.allow_short is True
    assert cfg.entry_type == "limit"


def test_entry_both_returns_entry_config():
    from koval.strategy.base.trade_setup import EntryConfig

    spec = get_block("entry.both")
    cfg = spec.factory(spec.params_schema())
    assert isinstance(cfg, EntryConfig)
    assert cfg.allow_long is True
    assert cfg.allow_short is True
    assert cfg.entry_type == "market"  # default


def test_signal_rsi_cross_maps_direction_to_bullish_or_bearish():
    spec = get_block("signal.rsi_cross")
    # Build closes that cross RSI=50 upward on the last bar.
    closes_up = np.concatenate([np.linspace(100.0, 90.0, 20), np.linspace(90.0, 110.0, 10)])
    bound_up = spec.factory(spec.params_schema(direction="cross_up", level=50.0, period=14))
    out = bound_up(_Stub(closes=closes_up))
    assert out in ("bullish", "none")

    bound_down = spec.factory(spec.params_schema(direction="cross_down", level=50.0, period=14))
    out2 = bound_down(_Stub(closes=closes_up))
    assert out2 in ("bearish", "none")


def test_signal_fvg_maps_gap_position_to_direction():
    """Gap above current close → bearish; gap below → bullish; straddle → none."""
    spec = get_block("signal.fvg")
    bound = spec.factory(spec.params_schema())

    # Bullish FVG (gap below close): high[-3] < low[-1].
    # Construct: highs = [..., 95, 100, 110], lows = [..., 90, 95, 105], close = 110.
    highs = np.array([100.0, 95.0, 100.0, 110.0])
    lows = np.array([99.0, 90.0, 95.0, 105.0])
    out = bound(_Stub(highs=highs, lows=lows, close=110.0))
    assert out == "bullish"

    # Bearish FVG (gap above close): low[-3] > high[-1].
    highs2 = np.array([100.0, 110.0, 105.0, 95.0])
    lows2 = np.array([99.0, 105.0, 100.0, 90.0])
    out2 = bound(_Stub(highs=highs2, lows=lows2, close=92.0))
    assert out2 == "bearish"


def test_signal_fvg_no_gap_returns_none():
    """When the 3-bar pattern has no FVG (overlapping ranges), factory returns 'none'."""
    spec = get_block("signal.fvg")
    bound = spec.factory(spec.params_schema())
    # Bars [-3] and [-1] overlap — neither highs[-3]<lows[-1] nor lows[-3]>highs[-1] holds.
    highs = np.array([100.0, 103.0, 104.0, 105.0])
    lows = np.array([99.0, 98.0, 100.0, 101.0])
    assert bound(_Stub(highs=highs, lows=lows, close=102.0)) == "none"


@pytest.mark.parametrize(
    "block_type",
    [
        "filter.rsi",
        "filter.adx",
        "filter.atr_volatility",
        "filter.stoch",
        "filter.macd",
        "filter.ema_trend",
        "filter.bb_volatility",
    ],
)
def test_filter_guards_short_arrays(block_type):
    """Every array-consuming filter returns False on None and tiny arrays — no crash."""
    spec = get_block(block_type)
    bound = spec.factory(spec.params_schema())
    tiny = np.array([100.0, 101.0])
    assert bound(_Stub(closes=tiny, highs=tiny, lows=tiny)) is False
    assert bound(_Stub(closes=None, highs=None, lows=None)) is False


@pytest.mark.parametrize(
    "block_type",
    [
        "signal.ema_cross",
        "signal.rsi_cross",
        "signal.bos",
        "signal.choch",
        "signal.fvg",
        "signal.ob",
        "signal.bullish_engulfing",
        "signal.bearish_engulfing",
    ],
)
def test_signal_guards_short_arrays(block_type):
    """Every array-consuming signal returns 'none' on None and tiny arrays — no crash."""
    spec = get_block(block_type)
    bound = spec.factory(spec.params_schema())
    tiny = np.array([100.0, 101.0])
    assert bound(_Stub(closes=tiny, highs=tiny, lows=tiny, opens=tiny)) == "none"
    assert bound(_Stub(closes=None, highs=None, lows=None, opens=None)) == "none"


# ---- Smoke contract test across the whole catalog --------------------------


def _full_stub(n: int = 250) -> _Stub:
    """Stub with N-bar synthetic ascending arrays — long enough for any helper."""
    arr = np.linspace(100.0, 150.0, n)
    return _Stub(
        closes=arr,
        highs=arr + 0.5,
        lows=arr - 0.5,
        opens=arr - 0.1,
        volumes=np.full(n, 1000.0),
        close=float(arr[-1]),
        high=float(arr[-1]) + 0.5,
        low=float(arr[-1]) - 0.5,
        open=float(arr[-1]) - 0.1,
        account_value=10_000.0,
    )


@pytest.mark.parametrize("block_type", sorted(BLOCK_CATALOG.keys()))
def test_factory_returns_value_matching_category_contract(block_type):
    """Smoke: every block's factory binds without error and returns the contract type."""
    from koval.strategy.base.trade_setup import EntryConfig

    spec = get_block(block_type)
    bound = spec.factory(spec.params_schema())
    s = _full_stub()

    if spec.category == "signal":
        out = bound(s)
        assert out in ("bullish", "bearish", "none")
    elif spec.category == "filter":
        out = bound(s)
        assert isinstance(out, bool)
    elif spec.category == "entry":
        # Entry factory returns EntryConfig directly — bound IS the EntryConfig.
        assert isinstance(bound, EntryConfig)
    elif spec.category == "exit":
        sl, tp = bound(s, "long", 100.0)
        assert isinstance(sl, float)
        assert tp is None or isinstance(tp, float)
    elif spec.category == "dynamic_exit":
        out = bound(s, trade_id=1, direction="long", entry_price=100.0, current_stop=95.0)
        assert out is None or isinstance(out, float)
    elif spec.category == "risk":
        size = bound(s, 100.0, 95.0, "long")
        assert isinstance(size, float)
        assert size >= 0.0
    else:
        pytest.fail(f"Unknown category {spec.category!r} for {block_type}")
