"""Portable JSON literals exercise the installed public source/strategy APIs."""

import json
from copy import deepcopy
from importlib.resources import files

import numpy as np

from koval.strategy.block_assembler import assemble_from_graph
from koval.strategy.graph.values import ValueSpecV1, value_at
from tests.strategy.graph.test_values import context


def subset(actual, expected):
    if isinstance(expected, dict):
        for key, value in expected.items():
            subset(actual[key], value)
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, e in zip(actual, expected, strict=True):
            subset(a, e)
    else:
        assert actual == expected


def test_portable_values_hold_edge_and_snapshot_suffix():
    path = files("koval").joinpath("examples/runtime/historical_conditions_v1.json")
    assert path.is_file(), "portable historical condition fixture is missing"
    fixture = json.loads(path.read_text())
    assert fixture["schema"] == "koval_historical_conditions_fixture_v1"
    for case in fixture["values"]:
        arrays = {name: np.array(values, dtype=float) for name, values in case["arrays"].items()}
        ctx = context(arrays.pop("closes"), **arrays)
        subset(value_at(ctx, ValueSpecV1(**case["spec"])).model_dump(), case["expected"])
    replay = fixture["replay"]
    graph = replay["graph"]
    original, restored = assemble_from_graph(graph), assemble_from_graph(graph)
    for strategy in (original, restored):
        strategy.config = {"symbol": "BTCUSDT", "timeframe": "1m"}

    def run(strategy, end):
        rows = np.array(replay["candles"][:end], dtype=float)
        ctx = context(
            rows[:, 4], opens=rows[:, 1], highs=rows[:, 2], lows=rows[:, 3], volumes=rows[:, 5]
        )
        for key, value in vars(ctx).items():
            if key not in {"account", "timeframe", "evidence", "indicators", "position_id"}:
                setattr(strategy, key, value)
        strategy.on_bar()
        outputs = strategy._result.entities_by_node
        expected = replay["expected_trace"][end - 1]
        assert outputs["source"]["value"].value == expected["value"]
        assert outputs["hold"]["condition"].value == expected["hold"]
        event = outputs["edge"]["event"]
        assert (event is not None) == expected["edge"]
        if event is not None:
            subset(event.model_dump(), replay["expected_event"])
        return outputs

    checkpoint = replay["snapshot_after_rows"]
    for end in range(1, checkpoint + 1):
        run(original, end)
    saved = json.loads(json.dumps(original.export_graph_state()))
    subset(saved, replay["expected_snapshot"])
    restored.restore_graph_state(saved)
    # Same-bar suppression is independent of restored transient outputs.
    restored.bar_index = checkpoint - 1
    restored.on_bar()
    assert restored.export_graph_state() == saved
    assert restored._result.entities_by_node == {}
    run_rows = deepcopy(replay["expected_trace"])
    for end in range(checkpoint + 1, len(run_rows) + 1):
        assert run(original, end) == run(restored, end)
        assert original.export_graph_state() == restored.export_graph_state()
    subset(original.export_graph_state(), replay["expected_final_state"])
    # Observations are recorded entities from the same evaluated graph.
    from koval.strategy.graph.evidence import DecisionRecorder

    ctx = context(
        (1, 1, 1, 1, 1), volumes=np.array([10, 10, 20, 30, 40.0]), evidence=DecisionRecorder()
    )
    from koval.strategy.graph.executor import GraphExecutor

    nodes = GraphExecutor.build(graph)
    for end in range(1, 5):
        nodes.step(context([1] * end, volumes=np.array([10, 10, 20, 30][:end], dtype=float)))
    nodes.step(ctx)
    by_id = {n["node_id"]: n for n in ctx.evidence.nodes}
    for node_id, expected in replay["expected_evidence"].items():
        subset(by_id[node_id], expected)
