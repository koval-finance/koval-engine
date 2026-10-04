"""Small native graphs for historical position-management contract tests."""

import numpy as np

from koval.strategy.graph.evidence import DecisionRecorder
from koval.strategy.graph.node import BarContext


def context(closes=(10, 10, 8), **overrides):
    values = dict(
        close=closes[-1],
        high=closes[-1],
        low=closes[-1],
        open=closes[-1],
        volume=1,
        bar_index=7,
        timestamp_ms=7000,
        closes=np.array(closes, dtype=float),
        account_value=10000,
        symbol="ETHUSDT",
        position_size=2,
        position_direction="long",
        position_id=42,
        evidence=DecisionRecorder(),
        decision_timestamp_ms=8000,
        history_start_ms=5000,
    )
    return BarContext(**(values | overrides))


def exit_graph(*, entry=False, mode=None):
    graph = {
        "blocks": [
            {
                "id": "bearish",
                "type": "policy.ema_trend",
                "params": {"period": 3, "direction": "bearish"},
            },
            {"id": "close_long", "type": "exec.position_exit", "params": {}},
        ],
        "connections": [
            {
                "from": "bearish",
                "from_port": "policy",
                "to": "close_long",
                "to_port": "condition",
            },
        ],
    }
    if entry:
        graph["blocks"] += [
            {"id": "tick", "type": "fact.every_bar", "params": {}},
            {"id": "and", "type": "interp.confluence_and", "params": {}},
            {
                "id": "gate",
                "type": "interp.direction_gate",
                "params": {"allow_long": True, "allow_short": False},
            },
            {
                "id": "order",
                "type": "exec.order_constructor",
                "params": {} if mode is None else {"take_profit_mode": mode},
            },
        ]
        graph["connections"] += [
            {"from": "tick", "from_port": "event", "to": "and", "to_port": "events"},
            {"from": "and", "from_port": "agreement", "to": "gate", "to_port": "agreement"},
            {"from": "gate", "from_port": "intent", "to": "order", "to_port": "intent"},
        ]
    return graph


def entry_graph(mode=None):
    graph = exit_graph(entry=True, mode=mode)
    graph["blocks"] = graph["blocks"][2:]
    graph["connections"] = graph["connections"][1:]
    return graph
