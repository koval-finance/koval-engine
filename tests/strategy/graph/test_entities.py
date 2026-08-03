import pickle

import pytest
from pydantic import ValidationError

from koval.strategy.graph.entities import (
    MarketEvent,
    OrderRequest,
    PolicyDecision,
)


def test_market_event_minimal():
    ev = MarketEvent(bar_index=3, timestamp_ms=1000, source_node_id="n1", kind="bos")
    assert ev.kind == "bos"
    assert ev.direction is None
    assert ev.price_levels == []


def test_market_event_carries_strength_and_levels():
    ev = MarketEvent(
        bar_index=3,
        timestamp_ms=1000,
        source_node_id="n1",
        kind="fvg_created",
        direction="bullish",
        strength=0.7,
        price_levels=[100.0, 101.0],
    )
    assert ev.strength == 0.7
    assert ev.price_levels == [100.0, 101.0]


def test_market_event_rejects_confidence_field():
    # Manifesto §3/§4: FACT nodes must not emit a probability/confidence.
    with pytest.raises(ValidationError):
        MarketEvent(bar_index=1, timestamp_ms=1, source_node_id="n", kind="bos", confidence=0.9)


def test_policy_decision_reason():
    p = PolicyDecision(
        bar_index=1,
        timestamp_ms=1,
        source_node_id="n",
        allowed=False,
        reason="high_spread",
        scope="execution",
    )
    assert p.allowed is False
    assert p.reason == "high_spread"


def test_order_request_roundtrip_pickle():
    o = OrderRequest(
        bar_index=1,
        timestamp_ms=1,
        source_node_id="n",
        symbol="BTCUSDT",
        side="buy",
        entry_price=64000.0,
        stop_price=63500.0,
        target_price=65500.0,
        quantity=0.5,
        order_type="limit",
    )
    assert pickle.loads(pickle.dumps(o)) == o


def test_order_request_carries_metadata():
    o = OrderRequest(
        bar_index=0,
        timestamp_ms=0,
        source_node_id="r",
        symbol="BTCUSDT",
        side="buy",
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        quantity=1.0,
        metadata={"required_margin": 50.0, "leverage": 2.0},
    )
    assert o.metadata["required_margin"] == 50.0
    # default is an empty dict, not shared across instances
    assert (
        OrderRequest(
            bar_index=0,
            timestamp_ms=0,
            source_node_id="r",
            symbol="",
            side="sell",
            entry_price=1.0,
            stop_price=2.0,
            quantity=1.0,
        ).metadata
        == {}
    )
