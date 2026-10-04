"""A signal close targets only the current identified long; it is never an entry."""

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.node import BarContext
from tests.strategy.graph.position_exit_fixtures import context, entry_graph, exit_graph


@pytest.mark.parametrize(
    "overrides",
    [
        {"position_size": 0},
        {"position_size": -2, "position_direction": "short"},
        {"position_direction": "short"},
        {"position_direction": None},
        {"position_id": None},
    ],
)
def test_no_exit_without_identified_current_long(overrides):
    result = GraphExecutor.build(exit_graph()).step(context(**overrides))
    assert result.position_exits == []
    assert result.orders == []


@pytest.mark.parametrize("closes", [(10, 10, 12), (10, 10, 10), (10, 8)])
def test_bearish_policy_requires_strict_close_below_ema_and_sufficient_history(closes):
    result = GraphExecutor.build(exit_graph()).step(context(closes))
    assert result.position_exits == []


def test_exit_has_current_identity_full_fraction_and_separate_terminal_collection():
    result = GraphExecutor.build(exit_graph(entry=True)).step(context())
    request = result.position_exits[0]
    assert request.model_dump(exclude={"decision_context"}) == {
        "bar_index": 7,
        "timestamp_ms": 7000,
        "source_node_id": "close_long",
        "symbol": "ETHUSDT",
        "position_id": 42,
        "position_side": "long",
        "quantity_fraction": 1.0,
        "order_type": "market",
        "reason": "signal",
    }
    assert result.entities_by_node["close_long"]["exit"] == request
    assert len(result.orders) == 1
    assert result.orders[0].source_node_id == "order"


@pytest.mark.parametrize("change", ["missing", "wrong_type", "fan_in", "params"])
def test_exit_condition_port_is_required_typed_single_and_has_no_quantity_parameters(change):
    graph = exit_graph()
    if change == "missing":
        graph["connections"] = []
    elif change == "wrong_type":
        graph["blocks"][0] = {"id": "bearish", "type": "fact.every_bar", "params": {}}
        graph["connections"][0]["from_port"] = "event"
    elif change == "fan_in":
        graph["blocks"].append({"id": "other", "type": "policy.ema_trend", "params": {}})
        graph["connections"].append(graph["connections"][0] | {"from": "other"})
    else:
        graph["blocks"][1]["params"] = {"quantity_fraction": 0.5}
    with pytest.raises(GraphValidationError):
        GraphExecutor.build(graph)


def test_graph_steps_once_and_position_identity_comes_from_existing_hooks(monkeypatch):
    strategy = assemble_from_graph(exit_graph(entry=True))
    strategy.config = {"symbol": "ETHUSDT"}
    strategy.on_open_position(42, TradeSetup("long", 10, 9, 12, 2))
    for key, value in vars(context()).items():
        if key != "position_id":
            setattr(strategy, key, value)
    steps = []
    original = strategy._executor.step

    def step(ctx):
        steps.append(ctx.bar_index)
        return original(ctx)

    monkeypatch.setattr(strategy._executor, "step", step)
    strategy.on_bar()
    assert strategy.get_position_exit_request().position_id == 42
    strategy.should_long()
    strategy.should_short()
    strategy.go_long()
    strategy.get_position_exit_request()
    strategy.on_bar()
    assert steps == [7]
    strategy.on_close_position(42, {"pnl": 0})
    assert (
        strategy.get_position_exit_request() is None
    )  # Cached intent must not outlive its position.
    strategy.bar_index = 8
    strategy.on_bar()
    assert strategy.get_position_exit_request() is None
    assert steps == [7, 8]
    strategy.on_open_position(43, TradeSetup("long", 10, 9, 12, 2))
    assert strategy.get_position_exit_request() is None  # Old decision cannot target a new trade.
    strategy.bar_index = 9
    strategy.on_bar()
    assert strategy.get_position_exit_request().position_id == 43


def test_exit_identity_is_isolated_between_strategy_instances():
    first, second = assemble_from_graph(exit_graph()), assemble_from_graph(exit_graph())
    first.on_open_position(42, TradeSetup("long", 10, 9, 12, 2))
    assert first._ctx().position_id == 42
    assert second._ctx().position_id is None


def test_late_close_callback_cannot_clear_a_new_position_identity():
    strategy = assemble_from_graph(exit_graph())
    setup = TradeSetup("long", 10, 9, 12, 2)
    strategy.on_open_position(42, setup)
    strategy.on_close_position(42, {"pnl": 0})
    strategy.on_open_position(43, setup)
    strategy.on_close_position(42, {"pnl": 0})
    assert strategy._ctx().position_id == 43


def test_missing_history_emits_no_exit_and_old_contexts_default_to_no_identity():
    ctx = context()
    ctx.closes = None
    assert GraphExecutor.build(exit_graph()).step(ctx).position_exits == []
    assert BarContext(10, 10, 10, 10, 1, 0, 0).position_id is None


@pytest.mark.parametrize(
    "graph, required",
    [
        (entry_graph(), frozenset()),
        (entry_graph("bracket"), frozenset()),
        (entry_graph("disabled"), frozenset({"optional_take_profit_v1"})),
        (exit_graph(), frozenset({"position_exit_v1"})),
        (
            exit_graph(entry=True, mode="disabled"),
            frozenset({"position_exit_v1", "optional_take_profit_v1"}),
        ),
    ],
)
def test_graph_declares_only_required_execution_capabilities(graph, required):
    strategy = assemble_from_graph(graph)
    assert strategy.required_execution_capabilities() == required


def test_exit_evidence_contains_consumed_ema_policy_and_does_not_overwrite_entry():
    strategy = assemble_from_graph(exit_graph(entry=True))
    strategy.config = {"symbol": "ETHUSDT"}
    for key, value in vars(context((10, 10, 12), position_size=0, position_direction=None)).items():
        if key != "position_id":
            setattr(strategy, key, value)
    entry = strategy.go_long()
    saved = deepcopy(entry)
    strategy.on_open_position(42, entry)
    for key, value in vars(context()).items():
        if key != "position_id":
            setattr(strategy, key, value)
    strategy.bar_index = 8
    strategy.on_bar()
    request = strategy.get_position_exit_request()
    evidence = request.decision_context
    nodes = {n["node_id"]: n for n in evidence["nodes"]}
    assert set(nodes) == {"bearish", "close_long"}
    assert evidence["status"] == "recorded"
    assert evidence["risk"] is None
    assert evidence["signal_bar_open_ms"] == 7000
    assert evidence["decision_timestamp_ms"] == 8000
    # Three-bar EMA seeds with the arithmetic mean: (10 + 10 + 8) / 3.
    assert nodes["bearish"]["values"] == {"ema": 28 / 3, "close": 8.0}
    assert nodes["bearish"]["predicate"] == "close < ema"
    assert nodes["close_long"]["contributing_nodes"] == ["bearish"]
    assert nodes["close_long"]["source_node_ids"] == ["close_long"]
    assert entry == saved
    saved_exit = deepcopy(evidence)
    strategy.bar_index = 9
    strategy.close = 7
    strategy.closes = np.array([10, 8, 7], dtype=float)
    strategy.on_bar()
    assert request.decision_context == saved_exit
    assert entry == saved


def test_exit_evidence_is_independent_for_multiple_terminals_and_ignored_branches():
    graph = exit_graph(entry=True)
    graph["blocks"] += [
        {
            "id": "a_other_policy",
            "type": "policy.ema_trend",
            "params": {"period": 2, "direction": "bearish"},
        },
        {"id": "a_other_exit", "type": "exec.position_exit", "params": {}},
        {"id": "ignored", "type": "policy.ema_trend", "params": {"period": 200}},
    ]
    graph["connections"].append(
        {
            "from": "a_other_policy",
            "from_port": "policy",
            "to": "a_other_exit",
            "to_port": "condition",
        }
    )
    executor = GraphExecutor.build(graph)
    result = executor.step(context())
    assert len(result.position_exits) == 2
    for request in result.position_exits:
        ids = {n["node_id"] for n in request.decision_context["nodes"]}
        assert ids == (
            {"bearish", "close_long"}
            if request.source_node_id == "close_long"
            else {"a_other_policy", "a_other_exit"}
        )
    assert {n["node_id"] for n in result.decision_context["nodes"]} == {
        "tick",
        "and",
        "gate",
        "order",
    }
    plain = GraphExecutor.build(graph).step(replace(context(), evidence=None))
    assert all(r.decision_context is None for r in plain.position_exits)
