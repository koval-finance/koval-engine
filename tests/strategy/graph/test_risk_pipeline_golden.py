"""Golden test for the discrete execution pipeline at the executor level.

A scripted INTERPRETATION source emits a TradingIntent unconditionally; the five
exec nodes (pricing -> sizing -> trade_gate + portfolio_gate -> router) are wired
end-to-end. Proves: (a) exactly ONE terminal order is collected and it is the
router's sized order, (b) a portfolio-gate drawdown lockout blocks the order so
nothing is collected. Mirrors the scripted-source pattern of
test_interp_full_engine_golden.py.
"""

import koval.strategy.nodes  # noqa: F401  (registers nodes)
from koval.engine.account_state import AccountSnapshot
from koval.strategy.graph.domains import Domain
from koval.strategy.graph.entities import TradingIntent
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.node import BarContext, NodeSpec
from koval.strategy.graph.ports import PortSpec
from koval.strategy.graph.registry import NODE_CATALOG, register_node
from koval.strategy.schemas import _StrictModel


class _IntentParams(_StrictModel):
    pass


def _scripted_intent_factory(_p):
    def evaluate(ctx, inputs, state):
        return {
            "intent": TradingIntent(
                bar_index=ctx.bar_index,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="scripted_intent",
                side="buy",
            )
        }

    return evaluate


def _register_scripted_intent():
    register_node(
        NodeSpec(
            "test.scripted_intent",
            Domain.INTERPRETATION,
            "scripted intent",
            "",
            _IntentParams,
            {},
            {"intent": PortSpec("intent", (TradingIntent,))},
            _scripted_intent_factory,
        )
    )


def teardown_function():
    NODE_CATALOG.pop("test.scripted_intent", None)


def _ctx(drawdown_pct=0.0, daily_loss_pct=0.0):
    return BarContext(
        close=100.0,
        high=101.0,
        low=99.0,
        open=100.0,
        volume=1.0,
        bar_index=0,
        timestamp_ms=0,
        account_value=10_000.0,
        symbol="BTCUSDT",
        account=AccountSnapshot(
            balance=10_000.0,
            equity=10_000.0,
            free_margin=10_000.0,
            margin_used=0.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            daily_pnl=0.0,
            peak_equity=10_000.0,
            drawdown_pct=drawdown_pct,
            daily_loss_pct=daily_loss_pct,
            open_positions=0,
            open_position=None,
        ),
    )


def _pipeline_graph():
    return {
        "blocks": [
            {"id": "src", "type": "test.scripted_intent", "params": {}},
            {"id": "pricing", "type": "exec.order_pricing", "params": {}},
            {"id": "sizing", "type": "exec.position_sizing", "params": {"risk_pct": 1.0}},
            {"id": "trade_gate", "type": "exec.trade_risk_gate", "params": {}},
            {
                "id": "pf_gate",
                "type": "exec.portfolio_risk_gate",
                "params": {"max_daily_drawdown_pct": 5.0, "max_total_margin_pct": 90.0},
            },
            {"id": "router", "type": "exec.execution_router", "params": {}},
        ],
        "connections": [
            {"from": "src", "from_port": "intent", "to": "pricing", "to_port": "intent"},
            {"from": "pricing", "from_port": "order", "to": "sizing", "to_port": "order"},
            {"from": "sizing", "from_port": "order", "to": "trade_gate", "to_port": "order"},
            {"from": "sizing", "from_port": "order", "to": "pf_gate", "to_port": "order"},
            {"from": "sizing", "from_port": "order", "to": "router", "to_port": "order"},
            {"from": "trade_gate", "from_port": "decision", "to": "router", "to_port": "approvals"},
            {"from": "pf_gate", "from_port": "decision", "to": "router", "to_port": "approvals"},
        ],
    }


def test_pipeline_routes_single_terminal_order_when_approved():
    _register_scripted_intent()
    ex = GraphExecutor.build(_pipeline_graph())
    result = ex.step(_ctx())
    # Exactly one terminal order — the router's — not any intermediate draft.
    assert len(result.orders) == 1
    order = result.orders[0]
    assert order.source_node_id == "router"
    assert order.quantity > 0
    assert order.side == "buy"


def test_pipeline_blocks_terminal_order_on_drawdown_lockout():
    _register_scripted_intent()
    ex = GraphExecutor.build(_pipeline_graph())
    result = ex.step(_ctx(daily_loss_pct=6.0))  # > 5% daily loss → portfolio lockout
    assert result.orders == []


def _build_strategy(graph):
    from koval.strategy.graph.strategy import build_graph_strategy

    strat = build_graph_strategy(graph)()
    strat.account_value = 10_000.0
    strat.close = strat.high = strat.low = strat.open = 100.0
    strat.volume = 1.0
    strat.bar_index = 0
    strat.timestamp_ms = 0
    return strat


def test_graph_strategy_enters_when_pipeline_approves():
    from koval.strategy.base.trade_setup import TradeSetup

    _register_scripted_intent()
    strat = _build_strategy(_pipeline_graph())
    assert strat.should_long() is True
    setup = strat.go_long()
    assert isinstance(setup, TradeSetup)
    assert setup.direction == "long"
    assert setup.size > 0


def test_graph_strategy_skips_entry_when_gate_blocks_order():
    # Regression for the gate-blocked-order crash: the upstream intent still
    # exists, but the trade gate blocks the order → no routed order. should_long
    # must be False (not call go_long with no order → RuntimeError).
    _register_scripted_intent()
    graph = _pipeline_graph()
    for b in graph["blocks"]:
        if b["id"] == "trade_gate":
            b["params"] = {"min_rr": 999.0}  # impossible R:R → always reject
    strat = _build_strategy(graph)
    assert strat.should_long() is False
    assert strat.should_short() is False
