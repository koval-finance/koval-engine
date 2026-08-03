"""Native typed-graph preset wiring the discrete risk pipeline.

Demonstrates the discrete execution pipeline on top of a simple EMA-cross signal:

    fact.ema_cross -> confluence -> direction_gate -> order_pricing
        -> position_sizing -> {trade_risk_gate, portfolio_risk_gate} -> execution_router

A builder used by the golden tests; not part of the public API.
"""

from __future__ import annotations


def build_risk_pipeline_demo_graph() -> dict:
    """Return the native typed dataflow graph exercising the risk pipeline."""
    return {
        "blocks": [
            {"id": "sig", "type": "fact.ema_cross", "params": {}},
            {"id": "confluence", "type": "interp.confluence_and", "params": {"min_signals": 1}},
            {
                "id": "gate",
                "type": "interp.direction_gate",
                "params": {"allow_long": True, "allow_short": True},
            },
            {"id": "pricing", "type": "exec.order_pricing", "params": {}},
            {"id": "sizing", "type": "exec.position_sizing", "params": {"risk_pct": 1.0}},
            {"id": "trade_gate", "type": "exec.trade_risk_gate", "params": {}},
            {"id": "pf_gate", "type": "exec.portfolio_risk_gate", "params": {}},
            {"id": "router", "type": "exec.execution_router", "params": {}},
        ],
        "connections": [
            {"from": "sig", "from_port": "event", "to": "confluence", "to_port": "events"},
            {
                "from": "confluence",
                "from_port": "agreement",
                "to": "gate",
                "to_port": "agreement",
            },
            {"from": "gate", "from_port": "intent", "to": "pricing", "to_port": "intent"},
            {"from": "pricing", "from_port": "order", "to": "sizing", "to_port": "order"},
            {"from": "sizing", "from_port": "order", "to": "trade_gate", "to_port": "order"},
            {"from": "sizing", "from_port": "order", "to": "pf_gate", "to_port": "order"},
            {"from": "sizing", "from_port": "order", "to": "router", "to_port": "order"},
            {
                "from": "trade_gate",
                "from_port": "decision",
                "to": "router",
                "to_port": "approvals",
            },
            {"from": "pf_gate", "from_port": "decision", "to": "router", "to_port": "approvals"},
        ],
    }
