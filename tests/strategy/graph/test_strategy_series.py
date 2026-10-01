import pytest

from koval.strategy.graph.executor import GraphExecutor
from tests.strategy.graph.test_decision_evidence import context, graph, rows


def test_series_matches_recorded_bounded_window_values_for_every_indicator():
    from koval.strategy.graph.series import strategy_indicator_series

    data, compiled = rows(), graph()
    series = strategy_indicator_series(compiled, data, timeframe="1h", history_bars=400)
    assert {(s["node_id"], s["component"]) for s in series} == {
        ("sig", "current"),
        ("macd", "macd"),
        ("macd", "signal"),
        ("macd", "histogram"),
        ("trend", "ema"),
    }
    assert next(s for s in series if s["node_id"] == "trend")["points"][0]["value"] is None
    for end in [301, 401, 550]:
        result = GraphExecutor.build(compiled).step(context(data, end))
        nodes = {n["node_id"]: n for n in result.decision_context["nodes"]}
        for s in series:
            point = s["points"][end - 1]
            assert point["t"] == data[end - 1, 0]
            assert point["value"] == pytest.approx(
                nodes[s["node_id"]]["values"][s["component"]], abs=1e-10
            )
    mutated = data.copy()
    mutated[551:, 4] *= 100
    again = strategy_indicator_series(compiled, mutated, timeframe="1h", history_bars=400)
    assert [s["points"][:551] for s in series] == [s["points"][:551] for s in again]


def test_two_emas_keep_distinct_node_ids_and_periods():
    from koval.strategy.graph.series import strategy_indicator_series

    compiled = {
        "blocks": [
            {"id": "a", "type": "policy.ema_trend", "params": {"period": 5}},
            {"id": "b", "type": "policy.ema_trend", "params": {"period": 300}},
        ],
        "connections": [],
    }
    result = strategy_indicator_series(compiled, rows(), timeframe="1h", history_bars=400)
    assert [s["node_id"] for s in result] == ["a", "b"]
    assert [s["params"]["period"] for s in result] == [5, 300]
    assert result[0]["points"][310]["value"] != result[1]["points"][310]["value"]
