"""Public graph discovery, historical composition and post-bar restore contracts."""

import json
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from koval.strategy.block_assembler import GraphValidationError, assemble_from_graph
from koval.strategy.graph.evidence import DecisionRecorder
from koval.strategy.graph.executor import GraphExecutor
from koval.strategy.graph.registry import get_node
from tests.strategy.graph.test_value_integration import edge
from tests.strategy.nodes.test_levels import PARAMS, context, evaluate


def graph():
    return {
        "blocks": [
            {"id": "trigger", "type": "fact.every_bar", "params": {}},
            {
                "id": "lower",
                "type": "source.value",
                "params": {"component": "channel.lower", "period": 2},
            },
            {
                "id": "upper",
                "type": "source.value",
                "params": {"component": "channel.upper", "period": 2},
            },
            {"id": "level", "type": "state.level_setup", "params": PARAMS},
        ],
        "connections": [
            edge("trigger", "event", "level", "trigger"),
            edge("lower", "value", "level", "lower"),
            edge("upper", "value", "level", "upper"),
        ],
    }


def test_level_semantic_report_is_versioned_and_separate_from_execution():
    from koval.strategy.graph.levels import level_capabilities

    report = level_capabilities()
    assert report["schema"] == "koval_historical_levels_capabilities_v1"
    assert report["history_schema"] == "koval_level_history_v1"
    assert report["adapter_execution_verified"] is False
    assert report["adapter_resume_supported"] is False
    assert report["availability_model"] == "historical_ideal_close_v1"
    assert report["state_version"] == 1
    assert report["formula_version"] == "level_setup_v1"
    assert report["source_history_limit"] == 1000
    assert report["temporal_prefix_limit"] is None
    assert report["supported_modes"] == ["sweep_reclaim", "breakout_retest", "breakout"]
    assert report["entity_schema"]["additionalProperties"] is False
    assert report["parameter_schema"]["required"] == [
        "mode",
        "expires_after_bars",
        "invalidation_buffer_pct",
    ]
    assert report["ports"]["trigger"]["max_connections"] == 1


@pytest.mark.parametrize("change", ["fanin", "type", "units", "cycle", "retest_upper"])
def test_bad_level_graphs_refuse_at_build(change):
    g = graph()
    if change == "fanin":
        g["blocks"].append({"id": "other", "type": "fact.every_bar", "params": {}})
        g["connections"].append(edge("other", "event", "level", "trigger"))
    elif change == "type":
        g["connections"][0] = edge("lower", "value", "level", "trigger")
    elif change == "units":
        g["blocks"][1]["params"] = {"component": "volume"}
    elif change == "cycle":
        g["connections"][0] = edge("level", "event", "level", "trigger")
    else:
        g["blocks"][3]["params"] = PARAMS | {"mode": "breakout_retest", "tolerance_pct": 1.0}
    with pytest.raises(GraphValidationError):
        GraphExecutor.build(g)


def test_new_params_roundtrip_without_defaults_on_old_entities():
    for mode in ["sweep_reclaim", "breakout", "breakout_retest"]:
        params = PARAMS | {"mode": mode}
        if mode == "breakout_retest":
            params["tolerance_pct"] = 1.0
        schema = get_node("state.level_setup").params_schema
        parsed = schema(**params)
        assert schema.model_validate_json(parsed.model_dump_json()) == parsed


def ctx(rows, end, *, window=1000, **changes):
    a = np.asarray(rows[max(0, end - window) : end], dtype=float)
    return context(
        end - 1,
        open=float(a[-1, 1]),
        high=float(a[-1, 2]),
        low=float(a[-1, 3]),
        close=float(a[-1, 4]),
        opens=a[:, 1],
        highs=a[:, 2],
        lows=a[:, 3],
        closes=a[:, 4],
        volumes=a[:, 5],
        history_start_ms=int(a[0, 0]),
        evidence=DecisionRecorder(),
        account_value=1000.0,
        **changes,
    )


def test_graph_node_id_binds_frozen_identity_and_restore_validates_before_mutation():
    g = graph()
    rows = [[i * 60000, 105, 110, 100, 105, 100] for i in range(5)]
    executors = []
    for node_id in ["level", "another"]:
        copy = deepcopy(g)
        copy["blocks"][-1]["id"] = node_id
        for connection in copy["connections"]:
            connection["to"] = node_id
        ex = GraphExecutor.build(copy)
        for end in range(1, 4):
            out = ex.step(ctx(rows, end))
        executors.append(out.entities_by_node[node_id]["level"])
    assert executors[0].level_id != executors[1].level_id
    strategy = assemble_from_graph(g)
    strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 4):
        inject(strategy, ctx(rows, end))
        strategy.on_bar()
    saved = strategy.export_graph_state()
    saved["states"]["level"]["output"]["lower"] = 999.0
    before = strategy.export_graph_state()
    with pytest.raises(ValueError):
        strategy.restore_graph_state(saved)
    assert strategy.export_graph_state() == before


def inject(strategy, c):
    for name, value in vars(c).items():
        if name not in {"evidence", "account", "timeframe", "position_id", "indicators"}:
            setattr(strategy, name, value)


def test_composable_history_separates_source_and_unbounded_streamed_prefix():
    from koval.strategy.graph.condition_history import graph_condition_history_requirements
    from koval.strategy.graph.levels import graph_level_history_requirements

    g = graph()
    g["blocks"][0] = {"id": "trigger", "type": "fact.condition_edge", "params": {}}
    previous = "compare"
    g["blocks"] += [
        {
            "id": "compare",
            "type": "fact.compare",
            "params": {"operator": "gt", "literal": 0.0, "unit": "price_quote_per_base"},
        }
    ]
    g["connections"].append(edge("lower", "value", "compare", "left"))
    for i in range(4):
        nid = f"hold{i}"
        g["blocks"].append({"id": nid, "type": "state.condition_hold", "params": {"bars": 400}})
        g["connections"].append(edge(previous, "condition", nid, "condition"))
        previous = nid
    g["connections"].append(edge(previous, "condition", "trigger", "condition"))
    GraphExecutor.build(g)
    report = graph_level_history_requirements(g)
    assert report["source_history_bars"] == 3
    assert report["first_ready_bar_by_node"]["level"] == 1599
    assert report["first_opportunity_bar_by_node"]["level"] == 1600
    assert report["replay_prefix_bars"] == 1601
    assert report["level_expiry_bars_by_node"] == {"level": 3}
    # Existing schema remains untouched and still marks the new node unknown.
    old = graph_condition_history_requirements(g)
    assert old["schema"] == "koval_condition_history_v1"
    assert old["first_ready_bar_by_node"]["level"] is None


def test_rolling_1000_history_and_10000_setups_keep_state_bounded():
    state = {}
    max_size = 0
    for i in range(20004):
        # New setups consume on the next bar; terminal cursor never rearms.
        result = evaluate(
            state,
            i,
            event=i % 2 == 0,
            ctx_changes={
                "open": 100.0,
                "low": 99.0,
                "close": 101.0,
                "closes": np.full(min(1000, i + 1), 101.0),
                "history_start_ms": max(0, i - 999) * 60000,
            },
        )
        assert result["level"].status == ("armed" if i % 2 == 0 else "consumed")
        max_size = max(max_size, len(json.dumps(state)))
    assert max_size < 6000
    assert state["arming_evidence"] == []


def test_physical_history_limit_applies_to_level_graph_without_p3_nodes():
    g = graph()
    g["blocks"][1]["params"]["offset_bars"] = 1000
    with pytest.raises(GraphValidationError, match="1000"):
        GraphExecutor.build(g)


def portable_graph_fixture():
    from importlib.resources import files

    return json.loads(
        files("koval").joinpath("examples/runtime/historical_level_graph_v1.json").read_text()
    )


@pytest.mark.parametrize("veto", [False, True])
def test_compression_hold_edge_arms_frozen_levels_and_rvol_veto_consumes_once(veto):
    fixture = portable_graph_fixture()
    rows = deepcopy(fixture["candles"])
    if veto:
        for key, value in fixture["veto_variant"]["volume_changes"].items():
            rows[int(key)][5] = value
    ex = GraphExecutor.build(fixture["graph"])
    events, orders, edges = [], [], []
    frozen = None
    for end in range(1, len(rows) + 1):
        c = ctx(rows, end)
        out = ex.step(c)
        entities = out.entities_by_node
        assert (
            entities["hold"]["condition"].value == fixture["expected"]["compression_hold"][end - 1]
        )
        if entities["edge"]["event"] is not None:
            edges.append(end - 1)
        level = entities["level"]["level"]
        if end - 1 == fixture["expected"]["armed_index"]:
            frozen = level.level_id
        if level.level_id:
            assert level.level_id == frozen
            assert [level.lower, level.upper, level.center] == fixture["expected"]["bounds"]
        if entities["level"]["event"] is not None:
            events.append(end - 1)
            assert entities["rvol"]["value"].value == (0.5 if veto else 2.0)
            assert level.status == "consumed"
            observation = next(n for n in c.evidence.nodes if n["runtime_node_id"] == "level")
            assert observation["values"]["level"]["trigger_source_node_id"] == "edge"
            if not veto:
                assert out.decision_context["status"] == "recorded"
                observed = {
                    (n["runtime_node_id"], n["timestamp_ms"]) for n in out.decision_context["nodes"]
                }
                assert ("edge", 300000) in observed
                assert ("lower", 300000) in observed
                assert ("upper", 300000) in observed
                assert ("upper", 360000) not in observed
        if end == 8 and veto:
            assert entities["rvol"]["value"].value == 2.0
        orders += [end - 1] * len(out.orders)
    assert edges == [5]
    assert events == [6]
    assert orders == ([] if veto else [6])


def test_portable_graph_every_checkpoint_prepared_and_adversarial_suffix_are_causal():
    from koval.strategy.graph.indicators import PreparedIndicators

    fixture = portable_graph_fixture()
    graph, rows = fixture["graph"], fixture["candles"]
    prepared = PreparedIndicators.build(graph, np.array(rows, dtype=float), 1000)
    direct, optimized = GraphExecutor.build(graph), GraphExecutor.build(graph)
    for end in range(1, len(rows) + 1):
        c = ctx(rows, end)
        a = direct.step(c)
        b = optimized.step(
            replace(
                c,
                evidence=DecisionRecorder(),
                indicators=prepared.at(c.timestamp_ms, len(c.closes)),
            )
        )
        assert a == b
        # Full software/input identity remains the host's exact-replay gate.
        saved = json.loads(json.dumps(direct.state._states))
        restored = GraphExecutor.build(graph)
        restored.state._states = deepcopy(saved)
        if end < len(rows):
            suffix = ctx(rows, end + 1)
            assert direct.step(suffix) == restored.step(
                replace(suffix, evidence=DecisionRecorder())
            )
            # Recreate prefix state to avoid evaluating a cursor twice in this loop.
            direct.state._states = saved
    changed = deepcopy(rows)
    for row in changed[7:]:
        row[1:5] = [10000.0, 12000.0, 1.0, 11000.0]
        row[5] = 999999.0
    changed_prepared = PreparedIndicators.build(graph, np.array(changed, dtype=float), 1000)
    left, right = GraphExecutor.build(graph), GraphExecutor.build(graph)
    for end in range(1, 8):
        c = ctx(rows, end)
        assert left.step(c) == right.step(
            replace(
                c,
                evidence=DecisionRecorder(),
                indicators=changed_prepared.at(c.timestamp_ms, len(c.closes)),
            )
        )


def test_public_strategy_every_checkpoint_suppresses_same_bar_then_matches_suffix():
    fixture = portable_graph_fixture()
    graph, rows = fixture["graph"], fixture["candles"]
    for checkpoint in range(1, len(rows) + 1):
        original, restored = assemble_from_graph(graph), assemble_from_graph(graph)
        for strategy in (original, restored):
            strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
        for end in range(1, checkpoint + 1):
            inject(original, ctx(rows, end))
            original.on_bar()
        saved = json.loads(json.dumps(original.export_graph_state()))
        restored.restore_graph_state(saved)
        inject(restored, ctx(rows, checkpoint))
        restored.on_bar()
        assert restored._result.orders == []
        assert restored.export_graph_state() == saved
        for end in range(checkpoint + 1, len(rows) + 1):
            for strategy in (original, restored):
                inject(strategy, ctx(rows, end))
                strategy.on_bar()
            assert original._result == restored._result
            assert original.export_graph_state() == restored.export_graph_state()


def test_level_history_composes_event_port_latency_for_nested_owners():
    from koval.strategy.graph.levels import graph_level_history_requirements

    g = portable_graph_fixture()["graph"]
    g["blocks"].append({"id": "second", "type": "state.level_setup", "params": PARAMS})
    g["connections"] += [
        edge("level", "event", "second", "trigger"),
        edge("lower", "value", "second", "lower"),
    ]
    report = graph_level_history_requirements(g)
    first = report["first_opportunity_bar_by_node"]["level"]
    assert report["first_ready_bar_by_node"]["second"] == first
    assert report["first_opportunity_bar_by_node"]["second"] == first + 1
    assert report["source_history_bars_by_node"]["second"] == 3


@pytest.mark.parametrize("end", [6, 7, 8])
def test_full_executor_identical_duplicate_suppresses_transient_edges_and_orders(end):
    fixture = portable_graph_fixture()
    ex = GraphExecutor.build(fixture["graph"])
    for count in range(1, end + 1):
        first = ex.step(ctx(fixture["candles"], count))
    saved = deepcopy(ex.state._states)
    result = ex.step(ctx(fixture["candles"], end))
    assert result.entities_by_node["level"]["level"] == first.entities_by_node["level"]["level"]
    assert result.entities_by_node["level"]["event"] is None
    assert result.orders == []
    assert ex.state._states == saved
    restored = GraphExecutor.build(fixture["graph"])
    restored.state._states = deepcopy(saved)
    assert restored.step(ctx(fixture["candles"], end)).entities_by_node["level"]["event"] is None
    assert restored.state._states == saved


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("change", ["close", "timestamp", "decision", "bound_history", "symbol"])
def test_public_strategy_conflicting_cached_cursor_refuses_before_state_changes(restored, change):
    fixture = portable_graph_fixture()
    s = assemble_from_graph(fixture["graph"])
    s.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 8):
        inject(s, ctx(fixture["candles"], end))
        s.on_bar()
    saved = s.export_graph_state()
    if restored:
        s = assemble_from_graph(fixture["graph"])
        s.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
        s.restore_graph_state(saved)
        inject(s, ctx(fixture["candles"], 7))
    if change == "close":
        s.close = 999.0
    elif change == "timestamp":
        s.timestamp_ms += 60000
    elif change == "decision":
        s.decision_timestamp_ms += 1
    elif change == "symbol":
        s.config["symbol"] = "ETHUSDT"
    else:
        s.highs = s.highs.copy()
        s.highs[-2] = 150.0
    with pytest.raises(ValueError):
        s.on_bar()
    assert s.export_graph_state() == saved


@pytest.mark.parametrize("change", ["source_formula", "rewire", "upstream_hold", "envelope_cursor"])
def test_restore_refuses_changed_upstream_dependency_or_envelope_before_mutation(change):
    fixture = portable_graph_fixture()
    s = assemble_from_graph(fixture["graph"])
    s.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 7):
        inject(s, ctx(fixture["candles"], end))
        s.on_bar()
    saved = s.export_graph_state()
    g = deepcopy(fixture["graph"])
    if change == "source_formula":
        next(b for b in g["blocks"] if b["id"] == "lower")["params"]["period"] = 3
    elif change == "rewire":
        next(c for c in g["connections"] if c["to"] == "level" and c["to_port"] == "lower")[
            "from"
        ] = "upper"
    elif change == "upstream_hold":
        next(b for b in g["blocks"] if b["id"] == "hold")["params"]["bars"] = 2
    else:
        saved["stepped_bar"] += 1
    target = assemble_from_graph(g)
    before = target.export_graph_state()
    with pytest.raises(ValueError):
        target.restore_graph_state(saved)
    assert target.export_graph_state() == before


def test_malformed_retained_evidence_is_semantically_validated_before_restore():
    from koval.strategy.graph.level_identity import canonical_hash

    fixture = portable_graph_fixture()
    s = assemble_from_graph(fixture["graph"])
    s.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 7):
        inject(s, ctx(fixture["candles"], end))
        s.on_bar()
    saved = s.export_graph_state()
    level = saved["states"]["level"]
    level["arming_evidence"] = [
        {
            "runtime_node_id": ["bad"],
            "timestamp_ms": 0,
            "node_type": "source.value",
            "result": "recorded",
            "values": {},
        }
    ]
    level["arming_evidence_hash"] = canonical_hash(level["arming_evidence"])
    level["integrity_hash"] = canonical_hash(
        {k: v for k, v in level.items() if k != "integrity_hash"}
    )
    before = s.export_graph_state()
    with pytest.raises(ValueError):
        s.restore_graph_state(saved)
    assert s.export_graph_state() == before


@pytest.mark.parametrize(
    "change", ["missing_hold", "missing_source", "empty_hold", "unstepped_with_states"]
)
def test_p4_restore_requires_complete_coherent_postbar_state(change):
    fixture = portable_graph_fixture()
    original = assemble_from_graph(fixture["graph"])
    original.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 5):
        inject(original, ctx(fixture["candles"], end))
        original.on_bar()
    saved = original.export_graph_state()
    if change == "missing_hold":
        del saved["states"]["hold"]
    elif change == "missing_source":
        del saved["states"]["width"]
    elif change == "empty_hold":
        saved["states"]["hold"] = {}
    else:
        saved["stepped_bar"] = -1
    target = assemble_from_graph(fixture["graph"])
    before = target.export_graph_state()
    with pytest.raises(ValueError):
        target.restore_graph_state(saved)
    assert target.export_graph_state() == before


def cross_level_graph():
    g = graph()
    g["blocks"][0] = {"id": "trigger", "type": "fact.cross", "params": {"direction": "up"}}
    g["blocks"].append({"id": "price", "type": "source.value", "params": {"component": "close"}})
    g["connections"] += [
        edge("price", "value", "trigger", "left"),
        edge("upper", "value", "trigger", "right"),
    ]
    return g


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "stale",
        "version",
        "missing_previous",
        "malformed_operand",
        "operand_bar",
        "operand_identity",
        "future_operand",
        "operand_source",
        "operand_unit",
    ],
)
def test_level_restore_refuses_incomplete_or_incoherent_cross_state_before_mutation(change):
    from tests.strategy.graph.test_values import context as value_context

    g = cross_level_graph()
    original = assemble_from_graph(g)
    original.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 4):
        inject(original, value_context([10] * end))
        original.on_bar()
    saved = original.export_graph_state()
    cross = saved["states"]["trigger"]
    if change == "empty":
        saved["states"]["trigger"] = {}
    elif change == "stale":
        cross["last_timestamp_ms"] -= 60000
    elif change == "version":
        cross["version"] = 2
    elif change == "missing_previous":
        del cross["previous"]
    elif change == "malformed_operand":
        cross["previous"]["left"] = {}
    elif change == "operand_bar":
        cross["previous"]["left"]["bar_index"] -= 1
    elif change == "operand_identity":
        cross["previous"]["left"]["symbol"] = "ETHUSDT"
    elif change == "future_operand":
        cross["previous"]["left"]["available_at_ms"] += 60000
    elif change == "operand_source":
        cross["previous"]["left"]["source_node_id"] = "upper"
    else:
        cross["previous"]["left"]["unit"] = "ratio"
    target = assemble_from_graph(g)
    before = target.export_graph_state()
    with pytest.raises(ValueError):
        target.restore_graph_state(saved)
    assert target.export_graph_state() == before


@pytest.mark.parametrize("checkpoint", [1, 3])
def test_level_cross_checkpoint_preserves_warmup_and_next_crossing(checkpoint):
    from tests.strategy.graph.test_values import context as value_context

    g = cross_level_graph()
    original, restored = assemble_from_graph(g), assemble_from_graph(g)
    for strategy in (original, restored):
        strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, checkpoint + 1):
        inject(original, value_context([10] * end))
        original.on_bar()
    restored.restore_graph_state(original.export_graph_state())
    for end in range(checkpoint + 1, 5):
        for strategy in (original, restored):
            inject(strategy, value_context([10, 10, 10, 12][:end]))
            strategy.on_bar()
        assert original._result == restored._result
        assert original.export_graph_state() == restored.export_graph_state()
    assert restored._result.entities_by_node["trigger"]["event"] is not None
    assert restored._result.entities_by_node["level"]["level"].status == "armed"


@pytest.mark.parametrize("change", ["timestamp", "decision", "symbol", "history"])
def test_multi_owner_restore_requires_one_complete_primary_cursor_identity(change):
    g = graph()
    g["blocks"].append({"id": "second", "type": "state.level_setup", "params": PARAMS})
    g["connections"] += [
        edge("trigger", "event", "second", "trigger"),
        edge("lower", "value", "second", "lower"),
        edge("upper", "value", "second", "upper"),
    ]
    rows = [[i * 60000, 105, 110, 100, 105, 100] for i in range(3)]
    original = assemble_from_graph(g)
    original.config = {"symbol": "BTCUSDT", "timeframe": "1m"}
    for end in range(1, 4):
        inject(original, ctx(rows, end))
        original.on_bar()
    saved = original.export_graph_state()
    other_ctx = ctx(rows, 3)
    if change == "timestamp":
        other_ctx = replace(
            other_ctx, timestamp_ms=180000, decision_timestamp_ms=240000, history_start_ms=60000
        )
    elif change == "decision":
        other_ctx = replace(other_ctx, decision_timestamp_ms=180001)
    elif change == "symbol":
        other_ctx = replace(other_ctx, symbol="ETHUSDT")
    else:
        other_ctx = replace(
            other_ctx,
            history_start_ms=120000,
            opens=other_ctx.opens[-1:],
            highs=other_ctx.highs[-1:],
            lows=other_ctx.lows[-1:],
            closes=other_ctx.closes[-1:],
            volumes=other_ctx.volumes[-1:],
        )
    other = GraphExecutor.build(g)
    other.step(other_ctx)
    saved["states"]["second"] = deepcopy(other.state._states["second"])
    target = assemble_from_graph(g)
    before = target.export_graph_state()
    with pytest.raises(ValueError):
        target.restore_graph_state(saved)
    assert target.export_graph_state() == before
