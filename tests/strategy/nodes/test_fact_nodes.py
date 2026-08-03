import numpy as np

import koval.strategy.nodes  # noqa: F401
from koval.strategy.graph.entities import MarketEvent
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.registry import get_node


def _ctx(highs, lows, closes, opens=None):
    return BarContext(
        close=float(closes[-1]),
        high=float(highs[-1]),
        low=float(lows[-1]),
        open=float((opens or closes)[-1]),
        volume=1.0,
        bar_index=len(closes) - 1,
        timestamp_ms=1000,
        closes=np.array(closes, float),
        highs=np.array(highs, float),
        lows=np.array(lows, float),
        opens=np.array(opens or closes, float),
    )


def test_fact_bos_emits_bullish_event():
    spec = get_node("fact.bos")
    ev = spec.factory(spec.params_schema(lookback=3))
    highs = [10, 11, 12, 13, 20]
    lows = [9, 9, 9, 9, 19]
    closes = [9.5, 10, 11, 12, 20]
    out = ev(_ctx(highs, lows, closes), {}, {})
    event = out["event"]
    assert isinstance(event, MarketEvent)
    assert event.kind == "bos"
    assert event.direction == "bullish"


def test_fact_fvg_emits_price_levels():
    # The key regression: FVG levels are no longer discarded.
    spec = get_node("fact.fvg")
    ev = spec.factory(spec.params_schema())
    # bullish FVG: highs[-3] < lows[-1]
    highs = [10, 11, 12]
    lows = [9, 10, 13]
    closes = [9.5, 10.5, 13.5]
    out = ev(_ctx(highs, lows, closes), {}, {})
    event = out["event"]
    assert event.direction == "bullish"
    assert len(event.price_levels) >= 2  # [gap_low, gap_high, ...]


def test_fact_ema_cross_emits_event():
    spec = get_node("fact.ema_cross")
    ev = spec.factory(spec.params_schema(fast=2, slow=3))
    closes = [10, 9, 8, 7, 12]  # fast crosses above slow on last bar
    out = ev(_ctx(closes, closes, closes), {}, {})
    event = out["event"]
    assert isinstance(event, MarketEvent)
    assert event.kind == "ema_cross"
    assert event.direction == "bullish"


def test_fact_every_bar_emits_configured_direction():
    spec = get_node("fact.every_bar")
    ev = spec.factory(spec.params_schema(direction="bullish"))

    out = ev(_ctx([101], [99], [100]), {}, {})

    event = out["event"]
    assert isinstance(event, MarketEvent)
    assert event.kind == "every_bar"
    assert event.direction == "bullish"
