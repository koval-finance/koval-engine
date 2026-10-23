"""Typed native recipe terminals are separate from the legacy trade bridge."""

import pytest

from tests.engine.test_spot_multi_contract import recipe


def graph(kind="grid"):
    return {
        "blocks": [
            {
                "id": "start",
                "type": "policy.ema_trend",
                "params": {"period": 2, "direction": "bullish"},
            },
            {"id": "recipe", "type": f"exec.spot_{kind}", "params": recipe(kind).params},
        ],
        "connections": [
            {"from": "start", "from_port": "policy", "to": "recipe", "to_port": "start"}
        ],
    }


def test_native_nodes_declare_meaningful_distinct_terminal_ports():
    from koval.strategy.graph.registry import get_node

    for kind in ("dca", "grid"):
        spec = get_node(f"exec.spot_{kind}")
        assert set(spec.input_ports) == {"start", "stop_hold", "stop_flatten"}
        assert spec.output_ports["recipe"].terminal
        assert spec.output_ports["status"].accepts[0].__name__ == "SpotRecipeStatus"


def test_static_requirements_refuse_old_historical_and_live_hosts_without_effects():
    from koval.engine.backtest_engine import (
        EngineRunSpec,
        ExecutionCapabilities,
        negotiate_execution_capabilities,
    )
    from koval.engine.live_engine import LiveEngine, LiveEngineConfig
    from koval.strategy.graph.execution_capabilities import graph_execution_capabilities

    g = graph()
    required = graph_execution_capabilities(g)
    assert "spot_grid_v1" in required
    spec = EngineRunSpec(graph=g, feeds={}, initial_capital=200)
    with pytest.raises((ValueError, RuntimeError), match="contract|capabilit"):
        negotiate_execution_capabilities(spec, ExecutionCapabilities((1,), ()))
    spec.execution_contract_version = 2
    assert negotiate_execution_capabilities(
        spec, ExecutionCapabilities((2,), tuple(required))
    ).features
    effects = []
    with pytest.raises(ValueError, match="capabilit"):
        LiveEngine(
            g,
            LiveEngineConfig("BTCUSDT", "1m", 200, market="spot"),
            on_record=lambda row: effects.append(row),
        )
    assert effects == []


def test_mixed_or_multiple_recipe_terminals_are_refused():
    from koval.strategy.graph.validation import validate_graph

    g = graph()
    g["blocks"].append({"id": "second", "type": "exec.spot_grid", "params": recipe().params})
    g["connections"].append(
        {"from": "start", "from_port": "policy", "to": "second", "to_port": "start"}
    )
    with pytest.raises(ValueError, match="one|multiple|mixed"):
        validate_graph(g)
