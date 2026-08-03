import numpy as np

import koval.strategy.nodes  # noqa: F401
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.block_assembler import assemble_from_graph

LEGACY = {
    "blocks": [
        {"id": "sig", "type": "signal.ema_cross", "params": {"fast": 2, "slow": 3}},
        {"id": "ent", "type": "entry.long_only", "params": {"entry_type": "market"}},
        {"id": "ext", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 2.0, "risk_reward": 2.0}},
        {"id": "rsk", "type": "risk.pct_risk", "params": {"risk_pct": 1.0, "leverage": 1.0}},
    ],
    "connections": [
        {"from": "sig", "to": "ent"},
        {"from": "ent", "to": "ext"},
        {"from": "ext", "to": "rsk"},
    ],
}


def _inject(s, closes):
    s.close = float(closes[-1])
    s.high = s.close
    s.low = s.close
    s.open = s.close
    s.volume = 1.0
    s.bar_index = len(closes) - 1
    s.timestamp_ms = s.bar_index * 1000
    s.closes = np.array(closes, float)
    s.highs = s.closes.copy()
    s.lows = s.closes.copy()
    s.opens = s.closes.copy()
    s.volumes = np.ones_like(s.closes)
    s.account_value = 10_000.0


def test_assemble_returns_declarative_strategy_for_legacy_graph():
    strat = assemble_from_graph(LEGACY)
    assert isinstance(strat, DeclarativeStrategy)


def test_graph_strategy_goes_long_on_golden_cross():
    strat = assemble_from_graph(LEGACY)
    _inject(strat, [10, 9, 8, 7])  # downtrend, no cross up
    assert strat.should_long() is False
    _inject(strat, [10, 9, 8, 7, 12])  # fast crosses above slow
    assert strat.should_long() is True
    setup = strat.go_long()
    assert isinstance(setup, TradeSetup)
    assert setup.direction == "long"
    assert setup.stop_loss < setup.entry_price


def test_graph_strategy_injects_account_snapshot_into_context():
    from koval.engine.account_state import AccountSnapshot
    from koval.strategy.graph.strategy import build_graph_strategy

    # Minimal valid compat-style graph (order_constructor terminal).
    graph = {
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
    cls = build_graph_strategy(graph)
    strat = cls()
    strat.config = {"symbol": "BTCUSDT"}
    strat.account_value = 12_345.0
    strat.timestamp_ms = 0
    ctx = strat._ctx()
    assert isinstance(ctx.account, AccountSnapshot)
    assert ctx.account.equity == 12_345.0
    assert ctx.symbol == "BTCUSDT"


def test_on_bar_keeps_graph_and_account_state_fresh_while_position_is_open():
    from koval.strategy.base.trade_setup import TradeSetup
    from koval.strategy.graph.strategy import build_graph_strategy

    graph = {
        "blocks": [{"id": "source", "type": "fact.every_bar", "params": {}}],
        "connections": [],
    }
    strat = build_graph_strategy(graph)()
    strat.account_value = 10_000.0
    strat.bar_index = 0
    strat.timestamp_ms = 0

    strat.on_bar()
    strat.on_open_position(
        1,
        TradeSetup(
            direction="long",
            entry_price=100.0,
            stop_loss=95.0,
            size=2.0,
            entry_type="market",
        ),
    )
    first_result = strat._result

    # Calling the hook again for the same bar must not evaluate twice.
    strat.on_bar()
    assert strat._result is first_result

    # The host injects current broker state before the next hook call.
    strat.account_value = 9_000.0
    strat.position_size = 2.0
    strat.position_direction = "long"
    strat.bar_index = 1
    strat.timestamp_ms = 1_000
    strat.on_bar()

    assert strat._result.entities_by_node["source"]["event"].bar_index == 1
    snapshot = strat._account.snapshot()
    assert snapshot.equity == 9_000.0
    assert snapshot.drawdown_pct == 10.0
    assert snapshot.open_positions == 1
