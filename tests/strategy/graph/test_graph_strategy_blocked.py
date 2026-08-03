"""Tests for _GraphStrategy.was_blocked() — intent-present-but-no-order detection."""

import koval.strategy.nodes  # noqa: F401  — populates NODE_CATALOG
from koval.strategy.graph.executor import StepResult
from koval.strategy.graph.strategy import build_graph_strategy

# Minimal valid graph: one node whose output port is consumed by another node so
# that the executor doesn't complain about an unresolved source.  Both blocks
# exist in NODE_CATALOG (confirmed by test_graph_strategy.py line 61-72).
_MINIMAL_GRAPH = {
    "blocks": [
        {"id": "source", "type": "fact.every_bar", "params": {}},
        {"id": "confluence", "type": "interp.confluence_and", "params": {}},
        {
            "id": "gate",
            "type": "interp.direction_gate",
            "params": {"allow_long": True, "allow_short": True},
        },
        {"id": "order", "type": "exec.order_constructor", "params": {}},
    ],
    "connections": [
        {
            "from": "source",
            "from_port": "event",
            "to": "confluence",
            "to_port": "events",
        },
        {
            "from": "confluence",
            "from_port": "agreement",
            "to": "gate",
            "to_port": "agreement",
        },
        {"from": "gate", "from_port": "intent", "to": "order", "to_port": "intent"},
    ],
}


def test_was_blocked_true_when_intent_present_but_no_order():
    from koval.strategy.graph.entities import TradingIntent

    cls = build_graph_strategy(_MINIMAL_GRAPH)
    s = cls()
    s._result = StepResult(
        intents=[
            TradingIntent(
                bar_index=0,
                timestamp_ms=0,
                source_node_id="gate",
                side="buy",
            )
        ],
        orders=[],
    )
    assert s.was_blocked() is True


def test_was_blocked_false_when_order_present():
    from koval.strategy.graph.entities import OrderRequest, TradingIntent

    cls = build_graph_strategy(_MINIMAL_GRAPH)
    s = cls()
    s._result = StepResult(
        intents=[
            TradingIntent(
                bar_index=0,
                timestamp_ms=0,
                source_node_id="gate",
                side="buy",
            )
        ],
        orders=[
            OrderRequest(
                bar_index=0,
                timestamp_ms=0,
                source_node_id="order",
                symbol="BTCUSDT",
                side="buy",
                entry_price=1.0,
                stop_price=0.9,
                target_price=1.2,
                quantity=1.0,
                order_type="market",
            )
        ],
    )
    assert s.was_blocked() is False


def test_was_blocked_false_when_no_step_yet():
    cls = build_graph_strategy(_MINIMAL_GRAPH)
    assert cls().was_blocked() is False
