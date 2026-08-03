"""Golden end-to-end test: the interpretation chain, run through the
REAL backtest engine (BT adapter + broker), produces an actual trade whose
``why_entry`` carries the reasoning chain.

Unlike test_interp_chain.py (executor-level, stops at OrderRequest) this exercises
the full path FACT -> interp chain -> exec.order_constructor -> GraphStrategy ->
BTStrategyAdapter -> broker -> TRADE_OPENED event, proving the deliverable the
design spec §5/§7 promised. A scripted FACT feeder (counter-based, so it is
independent of the adapter's absolute bar numbering) deterministically fires the
``order_block -> choch`` sequence; real detectors on synthetic data cannot be
relied on to fire a CHoCH, which is why this uses a scripted source.
"""

import numpy as np
import pytest

import koval.strategy.nodes  # noqa: F401
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import MarketEvent
from koval.strategy.graph.node import NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import NODE_CATALOG, register_node
from koval.strategy.schemas import _StrictModel

pytestmark = pytest.mark.backtrader


class _ScriptParams(_StrictModel):
    emit_on_count: dict[str, list[str]] = {}  # str(call_count) -> [kind, direction]


def _scripted_factory(p):
    def evaluate(ctx, inputs, state):
        n = state.get("n", 0) + 1
        state["n"] = n
        entry = p.emit_on_count.get(str(n))
        if not entry:
            return {"event": None}
        kind, direction = entry
        return {
            "event": MarketEvent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="scripted",
                kind=kind,
                direction=direction,
            )
        }

    return evaluate


def _register_scripted():
    register_node(
        NodeSpec(
            "test.scripted_fact_engine",
            Domain.FACT,
            "scripted",
            "",
            _ScriptParams,
            {},
            {"event": PortSpec("event", (MarketEvent,))},
            _scripted_factory,
        )
    )


def teardown_function():
    NODE_CATALOG.pop("test.scripted_fact_engine", None)


def _graph() -> dict:
    return {
        "blocks": [
            {
                "id": "sf",
                "type": "test.scripted_fact_engine",
                "params": {
                    "emit_on_count": {
                        "10": ["order_block", "bullish"],
                        "15": ["choch", "bullish"],
                    }
                },
            },
            {
                "id": "agg",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": ["order_block", "choch"], "timeout_bars": 50},
            },
            {"id": "gen", "type": "interp.setup_generator", "params": {}},
            {
                "id": "ss",
                "type": "interp.setup_score",
                "params": {"points": {"order_block": 20, "choch": 25}},
            },
            {"id": "cs", "type": "interp.context_score", "params": {"points": {}}},
            {
                "id": "gate",
                "type": "interp.qualification_gate",
                "params": {"qualification_threshold": 0},
            },
            {"id": "emit", "type": "interp.signal_emitter", "params": {}},
            {
                "id": "order",
                "type": "exec.order_constructor",
                "params": {
                    "sl_pct": 5.0,
                    "risk_reward": 2.0,
                    "risk_pct": 1.0,
                    "entry_type": "market",
                },
            },
        ],
        "connections": [
            {"from": "sf", "from_port": "event", "to": "agg", "to_port": "events"},
            {"from": "agg", "from_port": "candidate", "to": "gen", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "ss", "to_port": "candidate"},
            {"from": "gen", "from_port": "candidate", "to": "cs", "to_port": "candidate"},
            {"from": "ss", "from_port": "scored", "to": "gate", "to_port": "setup_score"},
            {"from": "cs", "from_port": "scored", "to": "gate", "to_port": "context_score"},
            {"from": "gate", "from_port": "scored", "to": "emit", "to_port": "scored"},
            {"from": "gen", "from_port": "candidate", "to": "emit", "to_port": "candidate"},
            {"from": "emit", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }


def _feed(n: int = 120) -> np.ndarray:
    base = np.linspace(100.0, 160.0, n)
    ts = np.arange(n, dtype=np.float64) * 3_600_000 + 1_704_067_200_000
    out = np.empty((n, 6), dtype=np.float64)
    out[:, 0] = ts
    out[:, 1] = base
    out[:, 2] = base + 1.0
    out[:, 3] = base - 1.0
    out[:, 4] = base
    out[:, 5] = 1000.0
    return out


def test_interp_chain_produces_trade_with_reasoning_through_engine():
    _register_scripted()
    spec = EngineRunSpec(graph=_graph(), feeds={"1h": _feed()}, initial_capital=10_000.0)
    events: list[dict] = []
    load_backtest_engine().run(spec, on_event=events.append)

    # 1. The chain produces a real trade through the broker.
    opened = [e for e in events if e["event_type"] == "TRADE_OPENED"]
    assert opened, "interpretation chain should open a trade"

    # 2. The reasoning chain surfaces into TradeSetup.why_entry and rides the WS
    # event payload on both SIGNAL_DETECTED (decision time) and TRADE_OPENED
    # (fill). Event fields live under ``payload`` (see backtest_runner._make_sink).
    why = opened[0]["payload"].get("why_entry") or []
    assert why, "TRADE_OPENED must carry the reasoning chain in why_entry"
    assert "Gate PASS" in why[0]
    assert "Setup 45 (order_block +20, choch +25)" in why[0]

    signals = [e for e in events if e["event_type"] == "SIGNAL_DETECTED"]
    assert any(s["payload"].get("why_entry") for s in signals), (
        "SIGNAL_DETECTED should also carry the reasoning chain"
    )
