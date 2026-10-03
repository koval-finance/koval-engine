"""Decision evidence observes the evaluated inputs without changing execution."""

import json
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from koval.strategy.graph.compat import compile_legacy
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.indicators import PreparedIndicators
from koval.strategy.graph.node import BarContext
from koval.strategy.graph.strategy import build_graph_strategy


def graph():
    blocks = [
        {"id": "sig", "type": "signal.rsi_cross", "params": {"period": 14, "level": 50}},
        {"id": "macd", "type": "filter.macd", "params": {}},
        {"id": "trend", "type": "filter.ema_trend", "params": {"period": 300}},
        {"id": "entry", "type": "entry.long_only", "params": {}},
        {"id": "exit", "type": "exit.fixed_sl_tp", "params": {"sl_pct": 4}},
        {"id": "risk", "type": "risk.pct_risk", "params": {"risk_pct": 1}},
    ]
    return compile_legacy(
        {
            "blocks": blocks,
            "connections": [
                {"from": a["id"], "to": b["id"]} for a, b in zip(blocks, blocks[1:], strict=False)
            ],
        }
    )


def rows():
    close = 100 + np.random.default_rng(42).normal(0.15, 1, 700).cumsum()
    return np.column_stack(
        (np.arange(700) * 3600000, close, close + 1, close - 1, close, np.ones(700))
    )


def context(data, end, evidence=True):
    from koval.strategy.graph.evidence import DecisionRecorder

    h = data[max(0, end - 400) : end]
    t, op, hi, lo, cl, vol = h[-1]
    return BarContext(
        cl,
        hi,
        lo,
        op,
        vol,
        end,
        int(t),
        closes=h[:, 4],
        highs=h[:, 2],
        lows=h[:, 3],
        account_value=10000,
        evidence=DecisionRecorder() if evidence else None,
        decision_timestamp_ms=int(t) + 3600000,
        history_start_ms=int(h[0, 0]),
        timeframe="1h",
    )


def test_macd_entry_records_actual_conditions():
    executor = GraphExecutor.build(graph())
    data = rows()
    for end in range(301, len(data)):
        result = executor.step(context(data, end))
        if result.orders:
            break
    assert result.orders
    evidence = result.decision_context
    assert evidence["status"] == "recorded"
    nodes = {n["node_id"]: n for n in evidence["nodes"]}
    sig = nodes["sig"]
    assert sig["values"]["previous"] < 50 <= sig["values"]["current"]
    assert sig["result"] == "passed"
    assert nodes["macd"]["values"]["histogram"] > 0
    assert nodes["macd"]["values"]["macd"] - nodes["macd"]["values"]["signal"] == pytest.approx(
        nodes["macd"]["values"]["histogram"]
    )
    assert nodes["trend"]["values"]["close"] > nodes["trend"]["values"]["ema"]
    assert nodes["trend"]["params"]["period"] == 300
    assert evidence["risk"]["risk_budget"] == 100
    assert evidence["risk"]["requested_quantity"] == result.orders[0].quantity
    assert evidence["decision_timestamp_ms"] == evidence["signal_bar_open_ms"] + 3600000
    json.dumps(evidence, allow_nan=False)


def test_legacy_source_ids_survive_compilation():
    compiled = graph()
    order = next(b for b in compiled["blocks"] if b["type"] == "exec.order_constructor")
    assert order["source_node_ids"] == ["entry", "exit", "risk"]


def test_recorded_context_is_not_mutated_by_next_bar():
    executor = GraphExecutor.build(graph())
    data = rows()
    result = executor.step(context(data, 330))
    snapshot = deepcopy(result.decision_context)
    executor.step(context(data, 331))
    data[331:, 4] = 0  # future data cannot rewrite the decision
    assert result.decision_context == snapshot
    assert "closes" not in json.dumps(snapshot)


@pytest.mark.parametrize("prepared", [False, True])
def test_recording_preserves_orders_intents_and_scalar_prepared_values(prepared):
    data, compiled = rows(), graph()
    table = PreparedIndicators.build(compiled, data, 400)
    plain, recorded = GraphExecutor.build(compiled), GraphExecutor.build(compiled)
    for end in range(301, 390):
        ctx = context(data, end)
        if prepared:
            ctx.indicators = table.at(ctx.timestamp_ms, len(ctx.closes))
        a, b = plain.step(replace(ctx, evidence=None)), recorded.step(ctx)
        assert a.orders == b.orders
        assert a.intents == b.intents
        assert a.entities_by_node == b.entities_by_node
        assert b.decision_context is not None


def test_graph_setup_exposes_detached_snapshot_and_unknown_nodes_are_partial():
    g = {
        "blocks": [
            {"id": "sig", "type": "fact.every_bar", "params": {}},
            {"id": "gate", "type": "interp.direction_gate", "params": {}},
            {"id": "and", "type": "interp.confluence_and", "params": {}},
            {"id": "order", "type": "exec.order_constructor", "params": {}},
        ],
        "connections": [
            {"from": "sig", "from_port": "event", "to": "and", "to_port": "events"},
            {"from": "and", "from_port": "agreement", "to": "gate", "to_port": "agreement"},
            {"from": "gate", "from_port": "intent", "to": "order", "to_port": "intent"},
        ],
    }
    strategy = build_graph_strategy(g)()
    ctx = context(rows(), 330)
    for key in (
        "close",
        "open",
        "high",
        "low",
        "volume",
        "closes",
        "bar_index",
        "timestamp_ms",
        "account_value",
    ):
        setattr(strategy, key, getattr(ctx, key))
    assert strategy.should_long()
    first = strategy.go_long()
    assert first.decision_context
    first.decision_context["nodes"].clear()
    assert strategy.go_long().decision_context["nodes"]


def test_native_sequence_keeps_original_fact_times():
    from koval.strategy.graph.entities import MarketEvent
    from koval.strategy.graph.evidence import DecisionRecorder
    from koval.strategy.graph.node import GraphNode
    from koval.strategy.graph.registry import get_node

    spec = get_node("interp.event_aggregator")
    params = spec.params_schema(event_sequence=["ema_cross", "rsi_cross"])
    evaluate, state = spec.factory(params), {}
    facts = []
    for index, kind in [(320, "ema_cross"), (323, "rsi_cross")]:
        ctx = context(rows(), index)
        ctx.evidence = DecisionRecorder()
        event = MarketEvent(
            bar_index=index,
            timestamp_ms=ctx.timestamp_ms,
            source_node_id=kind,
            kind=kind,
            direction="bullish",
        )
        fact_spec = get_node("fact." + kind)
        ctx.evidence.begin(
            GraphNode(
                kind,
                fact_spec,
                fact_spec.params_schema(),
                fact_spec.factory(fact_spec.params_schema()),
            ),
            ctx,
            {},
        )
        ctx.evidence.observe(values={"current": index, "previous": index - 1}, result="passed")
        ctx.evidence.finish({"event": event})
        ctx.evidence.begin(GraphNode("seq", spec, params, evaluate), ctx, {"events": [event]})
        result = evaluate(ctx, {"events": [event]}, state)
        facts = ctx.evidence.contributing
    assert result["candidate"] is not None
    assert [f["timestamp_ms"] for f in facts] == [319 * 3600000, 322 * 3600000]
    assert facts[0]["values"]["current"] == 320


def test_failed_false_zero_and_warmup_stay_distinct():
    compiled = {
        "blocks": [{"id": "macd", "type": "policy.macd", "params": {"require_positive": False}}],
        "connections": [],
    }
    data = rows()
    data[:, 4] = 100
    executor = GraphExecutor.build(compiled)
    node = executor.step(context(data, 40)).decision_context["nodes"][0]
    assert node["values"]["require_positive"] is False
    assert node["values"]["histogram"] == 0
    assert node["result"] == "failed"
    warmup = executor.step(context(data, 3)).decision_context["nodes"][0]
    assert warmup["values"] == {}
    assert warmup["result"] == "unavailable"


def test_legacy_setup_surfaces_recorded_reason_and_periods():
    strategy = build_graph_strategy(graph())()
    data = rows()
    for end in range(301, len(data)):
        ctx = context(data, end)
        for key in (
            "close",
            "open",
            "high",
            "low",
            "volume",
            "closes",
            "highs",
            "lows",
            "bar_index",
            "timestamp_ms",
            "account_value",
        ):
            setattr(strategy, key, getattr(ctx, key))
        if strategy.should_long():
            break
    setup = strategy.go_long()
    assert "rsi_cross" in setup.why_entry[0]
    assert setup.indicators_at_entry["trend"]["period"] == 300


def test_native_qualification_exposes_scores_and_boolean_without_fake_fact_pass():
    from koval.strategy.graph.entities import ScoredSetup
    from koval.strategy.graph.evidence import DecisionRecorder
    from koval.strategy.graph.node import GraphNode
    from koval.strategy.graph.registry import get_node

    recorder = DecisionRecorder()
    ctx = context(rows(), 330)
    spec = get_node("interp.qualification_gate")
    params = spec.params_schema(qualification_threshold=70)
    recorder.begin(GraphNode("gate", spec, params, spec.factory(params)), ctx, {})
    recorder.finish(
        {
            "scored": ScoredSetup(
                bar_index=330,
                timestamp_ms=ctx.timestamp_ms,
                source_node_id="gate",
                setup_id="test",
                setup_quality_score=55,
                context_score=35,
                final_score=90,
                qualified=True,
            )
        }
    )
    node = recorder.snapshot(ctx)["nodes"][0]
    assert node["values"]["final_score"] == 90
    assert node["values"]["qualified"] is True
    assert node["result"] == "passed"
    assert node["predicate"] == "final_score >= qualification_threshold"


@pytest.mark.parametrize(
    "node_type,params,status",
    [
        ("state.volatility_regime", {"min_atr_pct": 100}, "compression"),
        ("state.volatility_regime", {"min_atr_pct": 0}, "expansion"),
        ("state.trend_bias", {}, "bullish"),
    ],
)
def test_state_observation_is_a_recorded_value_not_a_pass_condition(node_type, params, status):
    data = rows()
    data[:, 4] = np.arange(len(data)) + 100
    data[:, 2] = data[:, 4] + 1
    data[:, 3] = data[:, 4] - 1
    executor = GraphExecutor.build(
        {"blocks": [{"id": "state", "type": node_type, "params": params}], "connections": []}
    )
    node = executor.step(context(data, 330)).decision_context["nodes"][0]
    assert node["result"] == "recorded"
    assert node["values"]["status"] == status


def _edge(source, source_port, target, target_port):
    return {"from": source, "from_port": source_port, "to": target, "to_port": target_port}


def _order_graph(*, second_order=False):
    result = {
        "blocks": [
            {"id": "signal", "type": "fact.every_bar"},
            {"id": "agreement", "type": "interp.confluence_and"},
            {"id": "gate", "type": "interp.direction_gate"},
            {
                "id": "order_a",
                "type": "exec.order_constructor",
                "params": {"risk_pct": 1, "sl_pct": 2},
            },
        ],
        "connections": [
            _edge("signal", "event", "agreement", "events"),
            _edge("agreement", "agreement", "gate", "agreement"),
            _edge("gate", "intent", "order_a", "intent"),
        ],
    }
    if second_order:
        result["blocks"].append(
            {
                "id": "order_b",
                "type": "exec.order_constructor",
                "params": {"risk_pct": 3, "sl_pct": 5},
            }
        )
        result["connections"].append(_edge("gate", "intent", "order_b", "intent"))
    return result


def _drive_strategy(strategy, end=30):
    strategy.closes = np.full(end, 100.0)
    strategy.highs = np.full(end, 101.0)
    strategy.lows = np.full(end, 99.0)
    strategy.close = strategy.open = 100.0
    strategy.high, strategy.low, strategy.volume = 101.0, 99.0, 1000.0
    strategy.account_value = 10000.0
    strategy.bar_index = end
    strategy.timestamp_ms = (end - 1) * 3600000
    strategy.decision_timestamp_ms = end * 3600000
    strategy.config = {"timeframe": "1h"}


def test_first_terminal_orders_risk_matches_the_executed_setup():
    strategy = build_graph_strategy(_order_graph(second_order=True))()
    _drive_strategy(strategy)
    assert strategy.should_long()
    setup = strategy.go_long()
    assert (setup.size, setup.stop_loss, setup.take_profit) == (50.0, 98.0, 104.0)
    assert setup.decision_context["risk"] == {
        "basis_name": "equity",
        "basis_value": 10000.0,
        "risk_pct": 1.0,
        "risk_budget": 100.0,
        "reference_entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "requested_quantity": 50.0,
    }
    assert {n["runtime_node_id"] for n in setup.decision_context["nodes"]} == {
        "signal",
        "agreement",
        "gate",
        "order_a",
    }
    setup.decision_context["risk"]["risk_pct"] = 99
    setup.decision_context["nodes"][0]["params"]["direction"] = "bearish"
    fresh = strategy.go_long().decision_context
    assert fresh["risk"]["risk_pct"] == 1.0
    assert next(n for n in fresh["nodes"] if n["node_id"] == "signal")["params"] == {
        "direction": "bullish"
    }


@pytest.mark.parametrize("second_order", [False, True])
def test_disconnected_policies_are_not_entry_reasons_or_indicators(second_order):
    compiled = _order_graph(second_order=second_order)
    compiled["blocks"].extend(
        [
            {"id": "required", "type": "policy.rsi", "params": {"min_val": 0, "max_val": 100}},
            {
                "id": "unrelated_passed",
                "type": "policy.rsi",
                "params": {"min_val": 0, "max_val": 100},
            },
            {
                "id": "unrelated_failed",
                "type": "policy.rsi",
                "params": {"min_val": 80, "max_val": 90},
            },
            {"id": "unrelated_unsupported", "type": "policy.adx"},
        ]
    )
    compiled["connections"].append(_edge("required", "policy", "gate", "policies"))
    strategy = build_graph_strategy(compiled)()
    _drive_strategy(strategy)
    assert strategy.should_long()
    setup = strategy.go_long()
    assert setup.why_entry == ["policy.rsi (required): min_val <= current <= max_val"]
    assert not any(name.startswith("unrelated_") for name in setup.indicators_at_entry)
    assert setup.decision_context["status"] == "recorded"
    assert setup.decision_context["missing_reasons"] == []


def _native_sequence_graph(sequence):
    return {
        "blocks": [
            {"id": "fact", "type": "fact.every_bar"},
            {
                "id": "sequence",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": sequence},
            },
            {"id": "score", "type": "interp.setup_score", "params": {"base": 10}},
            {"id": "qualify", "type": "interp.qualification_gate"},
            {"id": "intent", "type": "interp.signal_emitter"},
            {"id": "order", "type": "exec.order_constructor"},
        ],
        "connections": [
            _edge("fact", "event", "sequence", "events"),
            _edge("sequence", "candidate", "score", "candidate"),
            _edge("sequence", "candidate", "intent", "candidate"),
            _edge("score", "scored", "qualify", "setup_score"),
            _edge("qualify", "scored", "intent", "scored"),
            _edge("intent", "intent", "order", "intent"),
        ],
    }


def test_aggregator_records_only_the_chosen_alternative_chain():
    compiled = _native_sequence_graph(["every_bar"])
    compiled["blocks"].append({"id": "z_alternative", "type": "fact.every_bar"})
    # Both chains complete; the existing source-ID tie-break selects fact.
    compiled["connections"].append(_edge("z_alternative", "event", "sequence", "events"))
    strategy = build_graph_strategy(compiled)()
    _drive_strategy(strategy)
    assert strategy.should_long()
    nodes = strategy.go_long().decision_context["nodes"]
    assert {n["node_id"] for n in nodes} == {
        "fact",
        "sequence",
        "score",
        "qualify",
        "intent",
        "order",
    }
    assert next(n for n in nodes if n["node_id"] == "sequence")["contributing_nodes"] == ["fact"]


def test_selected_sequence_keeps_original_observations_without_other_sequences():
    compiled = _native_sequence_graph(["every_bar", "every_bar"])
    compiled["blocks"].extend(
        [
            {"id": "unrelated_fact", "type": "fact.every_bar"},
            {
                "id": "unrelated_sequence",
                "type": "interp.event_aggregator",
                "params": {"event_sequence": ["every_bar", "every_bar"]},
            },
        ]
    )
    compiled["connections"].append(_edge("unrelated_fact", "event", "unrelated_sequence", "events"))
    data = rows()
    recorded, plain = GraphExecutor.build(compiled), GraphExecutor.build(compiled)
    first = recorded.step(context(data, 320))
    assert not first.orders
    plain.step(context(data, 320, evidence=False))
    # A consumer cannot change the retained fact by editing an earlier snapshot.
    for node in first.decision_context["nodes"]:
        node["params"]["direction"] = "bearish"
    result = recorded.step(context(data, 321))
    control = plain.step(context(data, 321, evidence=False))
    assert result.orders == control.orders
    assert result.intents == control.intents
    assert result.entities_by_node == control.entities_by_node
    evidence = result.decision_context
    facts = [n for n in evidence["nodes"] if n["node_id"] == "fact"]
    assert {n["timestamp_ms"] for n in facts} == {319 * 3600000, 320 * 3600000}
    assert all(n["params"]["direction"] == "bullish" for n in facts)
    assert not any(n["node_id"].startswith("unrelated_") for n in evidence["nodes"])
    assert all(n["timestamp_ms"] < evidence["decision_timestamp_ms"] for n in facts)
    saved = deepcopy(evidence)
    recorded.step(context(data, 322))
    data[322:, 4] = 0
    assert evidence == saved


def test_reasoning_comes_from_the_intent_that_created_the_selected_order():
    compiled = _native_sequence_graph(["every_bar"])
    alternate = _native_sequence_graph(["every_bar"])
    for block in alternate["blocks"]:
        if block["id"] == "score":
            block["params"]["base"] = 90
        block["id"] = "a_" + block["id"]
    for edge in alternate["connections"]:
        edge["from"] = "a_" + edge["from"]
        edge["to"] = "a_" + edge["to"]
    # An earlier evaluated intent has no terminal order and must not supply the reason.
    compiled["blocks"].extend(b for b in alternate["blocks"] if b["id"] != "a_order")
    compiled["connections"].extend(e for e in alternate["connections"] if e["to"] != "a_order")
    strategy = build_graph_strategy(compiled)()
    _drive_strategy(strategy)
    assert strategy.should_long()
    assert strategy.go_long().why_entry == ["Setup 10; Context 0; Gate PASS 10>=0"]


@pytest.mark.parametrize(
    "node_type,params",
    [
        ("policy.rsi", {"min_val": 0, "max_val": 100}),
        ("policy.macd", {"require_positive": True}),
        ("policy.ema_trend", {"period": 10}),
        ("policy.atr_volatility", {"period": 10, "min_atr_pct": 0}),
    ],
)
def test_ignored_policy_context_is_not_a_trade_dependency(node_type, params):
    compiled = _order_graph()
    compiled["blocks"].extend(
        [
            {"id": "required", "type": node_type, "params": params},
            {"id": "ignored_fact", "type": "fact.every_bar"},
        ]
    )
    compiled["connections"].extend(
        [
            _edge("ignored_fact", "event", "required", "context"),
            _edge("required", "policy", "gate", "policies"),
        ]
    )
    data = rows()
    # This fixture makes all four filters pass and the ignored event emit.
    data[:, 4] = 100 + np.arange(len(data), dtype=float) ** 2
    data[:, 2], data[:, 3] = data[:, 4] + 1, data[:, 4] - 1
    recorded = GraphExecutor.build(compiled).step(context(data, 100))
    plain = GraphExecutor.build(compiled).step(context(data, 100, evidence=False))
    assert recorded.orders
    assert recorded.orders == plain.orders
    assert recorded.entities_by_node == plain.entities_by_node
    assert recorded.entities_by_node["ignored_fact"]["event"] is not None
    nodes = {n["node_id"]: n for n in recorded.decision_context["nodes"]}
    assert "ignored_fact" not in nodes
    assert nodes["required"]["contributing_nodes"] == []


def test_ignored_rsi_cross_does_not_become_a_passed_entry_reason():
    compiled = _order_graph()
    compiled["blocks"].extend(
        [
            {"id": "required", "type": "policy.rsi", "params": {"min_val": 0, "max_val": 100}},
            {"id": "ignored_fact", "type": "fact.rsi_cross", "params": {"period": 3, "level": 50}},
        ]
    )
    compiled["connections"].extend(
        [
            _edge("ignored_fact", "event", "required", "context"),
            _edge("required", "policy", "gate", "policies"),
        ]
    )
    strategy = build_graph_strategy(compiled)()
    ctx = context(rows(), 35)
    for name in (
        "close",
        "open",
        "high",
        "low",
        "volume",
        "closes",
        "highs",
        "lows",
        "bar_index",
        "timestamp_ms",
        "account_value",
    ):
        setattr(strategy, name, getattr(ctx, name))
    assert strategy.should_long()
    assert strategy.go_long().why_entry == ["policy.rsi (required): min_val <= current <= max_val"]


def _scored_state_graph(states):
    compiled = _native_sequence_graph(["every_bar"])
    compiled["blocks"].extend(states)
    compiled["blocks"].append(
        {
            "id": "context_score",
            "type": "interp.context_score",
            "params": {
                "points": {
                    "trend_aligned": 20,
                    "trend_neutral": 3,
                    "volatility_expansion": 8,
                    "volatility_compression": 2,
                }
            },
        }
    )
    compiled["connections"].extend(
        [
            _edge("sequence", "candidate", "context_score", "candidate"),
            _edge("context_score", "scored", "qualify", "context_score"),
            *(_edge(s["id"], "state", "context_score", "states") for s in states),
        ]
    )
    return compiled


@pytest.mark.parametrize("node_type", ["state.trend_bias", "state.volatility_regime"])
def test_ignored_state_context_is_not_a_trade_dependency(node_type):
    compiled = _scored_state_graph([{"id": "state", "type": node_type}])
    compiled["blocks"].append({"id": "ignored_fact", "type": "fact.every_bar"})
    compiled["connections"].append(_edge("ignored_fact", "event", "state", "context"))
    data = rows()
    recorded = GraphExecutor.build(compiled).step(context(data, 330))
    plain = GraphExecutor.build(compiled).step(context(data, 330, evidence=False))
    assert recorded.orders
    assert recorded.entities_by_node == plain.entities_by_node
    nodes = {n["node_id"]: n for n in recorded.decision_context["nodes"]}
    assert "ignored_fact" not in nodes
    assert nodes["state"]["contributing_nodes"] == []


@pytest.mark.parametrize("reverse", [False, True])
def test_context_score_records_only_last_state_of_each_kind(reverse):
    states = [
        {"id": "earlier", "type": "state.volatility_regime", "params": {"min_atr_pct": 100}},
        {"id": "later", "type": "state.volatility_regime", "params": {"min_atr_pct": 0}},
    ]
    if reverse:
        states.reverse()
    compiled = _scored_state_graph(states)
    recorded = GraphExecutor.build(compiled).step(context(rows(), 330))
    plain = GraphExecutor.build(compiled).step(context(rows(), 330, evidence=False))
    assert recorded.orders
    assert recorded.entities_by_node == plain.entities_by_node
    assert recorded.entities_by_node["context_score"]["scored"].context_score == (
        2 if reverse else 8
    )
    nodes = {n["node_id"]: n for n in recorded.decision_context["nodes"]}
    assert states[0]["id"] not in nodes
    assert states[1]["id"] in nodes
    assert nodes["context_score"]["contributing_nodes"] == ["sequence", states[1]["id"]]


def _policy_graph(node_type, params):
    compiled = _order_graph()
    compiled["blocks"].append({"id": "required", "type": node_type, "params": params})
    compiled["connections"].append(_edge("required", "policy", "gate", "policies"))
    return compiled


def _atr_evidence(params):
    # _drive_strategy's flat 100 close with a 99..101 range gives atr_pct == 2.
    strategy = build_graph_strategy(_policy_graph("policy.atr_volatility", params))()
    _drive_strategy(strategy)
    assert strategy.should_long()
    setup = strategy.go_long()
    node = next(n for n in setup.decision_context["nodes"] if n["node_id"] == "required")
    return setup, node


def test_atr_volatility_evidence_is_unchanged_without_a_cap():
    setup, node = _atr_evidence({})
    assert node["params"] == {"period": 14, "min_atr_pct": 0.5}
    assert node["values"] == {"atr": 2.0, "close": 100.0, "atr_pct": 2.0, "min_atr_pct": 0.5}
    assert node["predicate"] == "atr != 0 and close != 0 and atr_pct >= min_atr_pct"
    assert node["result"] == "passed"
    assert setup.why_entry == [
        "policy.atr_volatility (required): atr != 0 and close != 0 and atr_pct >= min_atr_pct"
    ]
    assert setup.decision_context["status"] == "recorded"


def test_atr_volatility_evidence_names_the_cap_when_set():
    setup, node = _atr_evidence({"min_atr_pct": 0, "max_atr_pct": 2.0})
    assert node["params"] == {"period": 14, "min_atr_pct": 0.0, "max_atr_pct": 2.0}
    assert node["values"] == {
        "atr": 2.0,
        "close": 100.0,
        "atr_pct": 2.0,
        "min_atr_pct": 0.0,
        "max_atr_pct": 2.0,
    }
    predicate = "atr != 0 and close != 0 and atr_pct >= min_atr_pct and atr_pct <= max_atr_pct"
    assert node["predicate"] == predicate
    assert setup.why_entry == [f"policy.atr_volatility (required): {predicate}"]


def test_atr_volatility_cap_blocks_the_entry_and_records_the_failure():
    compiled = _policy_graph("policy.atr_volatility", {"min_atr_pct": 0, "max_atr_pct": 1.5})
    strategy = build_graph_strategy(compiled)()
    _drive_strategy(strategy)
    assert not strategy.should_long()
    result = strategy._ensure_stepped()
    node = next(n for n in result.decision_context["nodes"] if n["node_id"] == "required")
    assert node["result"] == "failed"
    assert node["outputs"]["policy"]["reason"] == "volatility_too_high"


def test_unset_cap_leaves_saved_graph_params_and_their_hash_unchanged():
    import hashlib

    from koval.strategy.graph.registry import get_node
    from koval.strategy.graph.series import strategy_indicator_series

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    schema = get_node("policy.atr_volatility").params_schema
    released = {"period": 14, "min_atr_pct": 0.5}  # The 0.12.2 dump.
    assert digest(schema().model_dump()) == digest(released)
    assert digest(schema(**released).model_dump()) == digest(released)
    assert digest(schema(max_atr_pct=3).model_dump()) != digest(released)
    compiled = _policy_graph("policy.atr_volatility", {})
    series = strategy_indicator_series(compiled, rows()[:40], timeframe="1h")
    assert [s["params"] for s in series] == [released]


def test_cooldown_is_recorded_as_a_passed_entry_reason():
    strategy = build_graph_strategy(_policy_graph("policy.cooldown", {"bars": 2}))()
    _drive_strategy(strategy)
    assert strategy.should_long()
    setup = strategy.go_long()
    predicate = "position_size == 0 and (flat_bars is None or flat_bars > bars)"
    assert setup.why_entry == [f"policy.cooldown (required): {predicate}"]
    context = setup.decision_context
    assert context["status"] == "recorded"
    assert context["missing_reasons"] == []
    node = next(n for n in context["nodes"] if n["node_id"] == "required")
    assert node["params"] == {"bars": 2}
    assert node["values"] == {"flat_bars": None, "bars": 2, "position_size": 0.0}
    assert node["predicate"] == predicate
    assert node["result"] == "passed"
    assert node["contributing_nodes"] == []


def test_cooldown_blocks_reentry_through_the_strategy_and_records_the_count():
    strategy = build_graph_strategy(_policy_graph("policy.cooldown", {"bars": 2}))()
    seen = []
    for end, size in ((30, 0.0), (31, 1.0), (32, 0.0), (33, 0.0), (34, 0.0)):
        _drive_strategy(strategy, end)
        strategy.position_size = size
        allowed = strategy.should_long()
        node = next(
            n
            for n in strategy._ensure_stepped().decision_context["nodes"]
            if n["node_id"] == "required"
        )
        seen.append((allowed, node["result"], node["values"]["flat_bars"]))
    assert seen == [
        (True, "passed", None),
        (False, "failed", 0),
        (False, "failed", 1),
        (False, "failed", 2),
        (True, "passed", 3),
    ]
