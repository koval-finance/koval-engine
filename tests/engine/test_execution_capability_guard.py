"""Unsupported historical features fail before live feed/broker/session effects."""

import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig, _validate_live_setup
from koval.engine.live_feed import ReplayFeed, StopSignal
from koval.strategy.base.trade_setup import TradeSetup
from tests.strategy.graph.position_exit_fixtures import entry_graph, exit_graph


@pytest.mark.parametrize(
    "graph, capability",
    [
        (exit_graph(), "position_exit_v1"),
        (entry_graph("disabled"), "optional_take_profit_v1"),
        (exit_graph(entry=True, mode="disabled"), "position_exit_v1"),
    ],
)
@pytest.mark.parametrize("provided_broker", [False, True])
def test_unsupported_graph_is_rejected_before_any_host_side_effect(
    monkeypatch, graph, capability, provided_broker
):
    from koval.engine import live_engine

    effects = []

    def broker_factory(*args, **kwargs):
        effects.append("broker_created")
        raise AssertionError("unsupported graph must not construct a broker")

    class BrokerTrap:
        def __getattribute__(self, name):
            effects.append("broker_accessed")
            raise AssertionError("unsupported graph must not access an injected broker")

    class FeedTrap:
        def bars(self, stop):
            effects.append("feed_opened")
            raise AssertionError("unsupported graph must not open feed")

    monkeypatch.setattr(live_engine, "PaperBroker", broker_factory)
    with pytest.raises(ValueError, match=capability):
        engine = LiveEngine(
            graph,
            LiveEngineConfig("ETHUSDT", "1h", 10000, market="spot"),
            broker=BrokerTrap() if provided_broker else None,
            on_record=lambda row: effects.append("record"),
            on_event=lambda event: effects.append("event"),
        )
        engine.run(FeedTrap(), StopSignal())
    assert effects == []


@pytest.mark.parametrize("mode", [None, "bracket"])
def test_legacy_entry_graph_still_runs_on_live_host(mode):
    import numpy as np

    events = []
    engine = LiveEngine(
        entry_graph(mode),
        LiveEngineConfig("ETHUSDT", "1h", 10000, market="spot"),
        on_event=events.append,
    )
    engine.run(ReplayFeed(np.array([[0, 100, 101, 99, 100, 10]], dtype=float)), StopSignal())
    assert events


def test_live_setup_rejects_disabled_tp_even_if_a_custom_strategy_omits_capabilities():
    setup = TradeSetup("long", 100, 98, size=1, take_profit_mode="disabled")
    with pytest.raises(ValueError, match="optional_take_profit_v1"):
        _validate_live_setup(setup, expected_direction="long")


@pytest.mark.parametrize(
    "graph, required",
    [
        (entry_graph(), ()),
        (exit_graph(), ("position_exit_v1",)),
        (entry_graph("disabled"), ("optional_take_profit_v1",)),
        (exit_graph(entry=True, mode="disabled"), ("optional_take_profit_v1", "position_exit_v1")),
    ],
)
def test_historical_negotiation_requires_graph_features_even_without_explicit_spec_flags(
    graph, required
):
    from koval.engine.backtest_engine import (
        EngineRunSpec,
        ExecutionCapabilities,
        ProtocolVersionError,
        negotiate_execution_capabilities,
    )

    spec = EngineRunSpec(graph=graph, feeds={}, initial_capital=10000)
    old_host = ExecutionCapabilities((1,), ("entry",))
    if required:
        with pytest.raises(ProtocolVersionError, match="required execution capabilities"):
            negotiate_execution_capabilities(spec, old_host)
    else:
        assert negotiate_execution_capabilities(spec, old_host).features == ()
    offered = ExecutionCapabilities((1,), required)
    assert negotiate_execution_capabilities(spec, offered).features == required
    assert spec.required_execution_capabilities == ()  # Negotiation does not rewrite saved inputs.
