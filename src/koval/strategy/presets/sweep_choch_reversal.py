"""Native typed-graph preset: an order-block -> change-of-character reversal
expressed on the interpretation chain.

A hand-authored *native* (non-compat) strategy graph, used by the golden tests;
not part of the public API. A dedicated ``fact.liquidity_sweep`` node does not
exist, so this uses the ``order_block -> choch`` sequence; the name documents
the intended SMC setup.
"""

from __future__ import annotations


def build_sweep_choch_reversal_graph() -> dict:
    """Return the native typed dataflow graph for the OB->CHoCH reversal setup."""
    return {
        "blocks": [
            {"id": "ob", "type": "fact.ob", "params": {"lookback": 5}},
            {"id": "choch", "type": "fact.choch", "params": {"lookback": 5}},
            {"id": "trend", "type": "state.trend_bias", "params": {"period": 50}},
            {"id": "vol", "type": "state.volatility_regime", "params": {"period": 14}},
            {
                "id": "agg",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": ["order_block", "choch"], "timeout_bars": 20},
            },
            {
                "id": "gen",
                "type": "interp.setup_generator",
                "params": {"setup_type_map": {"order_block->choch": "ob_reversal"}},
            },
            {
                "id": "sscore",
                "type": "interp.setup_score",
                "params": {"points": {"order_block": 20, "choch": 25}},
            },
            {
                "id": "cscore",
                "type": "interp.context_score",
                "params": {
                    "points": {
                        "trend_aligned": 15,
                        "counter_trend": -10,
                        "volatility_expansion": 5,
                    }
                },
            },
            {
                "id": "gate",
                "type": "interp.qualification_gate",
                "params": {"qualification_threshold": 40},
            },
            {"id": "emit", "type": "interp.signal_emitter", "params": {}},
            {
                "id": "order",
                "type": "exec.order_constructor",
                "params": {"sl_pct": 2.0, "risk_reward": 2.0, "risk_pct": 1.0},
            },
        ],
        "connections": [
            {"from": "ob", "from_port": "event", "to": "agg", "to_port": "events"},
            {"from": "choch", "from_port": "event", "to": "agg", "to_port": "events"},
            {"from": "agg", "from_port": "candidate", "to": "gen", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "sscore", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "cscore", "to_port": "candidate"},
            {"from": "trend", "from_port": "state", "to": "cscore", "to_port": "states"},
            {"from": "vol", "from_port": "state", "to": "cscore", "to_port": "states"},
            {"from": "sscore", "from_port": "scored", "to": "gate", "to_port": "setup_score"},
            {"from": "cscore", "from_port": "scored", "to": "gate", "to_port": "context_score"},
            {"from": "gate", "from_port": "scored", "to": "emit", "to_port": "scored"},
            {"from": "gen", "from_port": "candidate", "to": "emit", "to_port": "candidate"},
            {"from": "emit", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }
