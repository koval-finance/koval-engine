"""Explicit paper replay endings without weakening sandbox containment."""

import numpy as np
import pytest

from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from tests.engine.test_live_engine import _ImmediateFillBroker, _SingleEntryStrategy
from tests.engine.test_runtime_contract import EXECUTION
from tests.engine.test_strategy_account_binding import GRAPH


@pytest.mark.parametrize("policy", ["mark_at_last_close", "flatten_at_last_close"])
def test_paper_terminal_policy_controls_exposure_and_exit_costs(monkeypatch, policy):
    assert "end_of_data_policy" in LiveEngineConfig.__dataclass_fields__
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph",
        lambda _: _SingleEntryStrategy(stop_loss=90, take_profit=120),
    )
    statuses, trades = [], []
    runtime = LiveEngine(
        {},
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            execution=EXECUTION | {"commission_bps": 10},
            end_of_data_policy=policy,
        ),
        on_status=statuses.append,
        on_trade=trades.append,
    )
    runtime.run(
        ReplayFeed(np.array([[0, 100, 101, 99, 100, 10], [60_000, 100, 106, 99, 105, 10]])),
        StopSignal(),
    )
    terminal = statuses[-1]
    assert terminal["terminal"]
    assert terminal["run_identity"]["run_parameters"]["end_of_data_policy"] == policy
    retained = policy == "mark_at_last_close"
    assert (terminal["open_position"] is not None) == retained
    assert len(trades) == (0 if retained else 1)
    quantity = statuses[1]["open_position"]["quantity"]
    expected_fees = quantity * (0.1 if retained else 0.205)
    assert terminal["metrics"]["fees"] == pytest.approx(expected_fees)
    assert terminal["metrics"]["equity"] == pytest.approx(10_000 + 5 * quantity - expected_fees)


def test_retaining_exposure_is_not_available_for_sandbox():
    assert "end_of_data_policy" in LiveEngineConfig.__dataclass_fields__
    with pytest.raises(ValueError, match="paper"):
        LiveEngine(
            GRAPH,
            LiveEngineConfig("BTCUSDT", "1m", 10_000, end_of_data_policy="mark_at_last_close"),
            broker=_ImmediateFillBroker(),
        )


def test_unknown_terminal_policy_is_rejected():
    assert "end_of_data_policy" in LiveEngineConfig.__dataclass_fields__
    with pytest.raises(ValueError, match="end_of_data_policy"):
        LiveEngine({}, LiveEngineConfig("BTCUSDT", "1m", 10_000, end_of_data_policy="ignore"))


@pytest.mark.parametrize("ending", ["stop", "error"])
def test_mark_policy_still_flattens_on_stop_or_error(monkeypatch, ending):
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph",
        lambda _: _SingleEntryStrategy(stop_loss=90, take_profit=120),
    )

    class InterruptedFeed:
        def bars(self, stop):
            yield np.array([0, 100, 101, 99, 100, 10])
            yield np.array([60_000, 100, 101, 99, 100, 10])
            if ending == "error":
                raise RuntimeError("feed failed")
            stop.set()

    statuses = []
    runtime = LiveEngine(
        {},
        LiveEngineConfig(
            "BTCUSDT", "1m", 10_000, execution=EXECUTION, end_of_data_policy="mark_at_last_close"
        ),
        on_status=statuses.append,
    )
    if ending == "error":
        with pytest.raises(RuntimeError, match="feed failed"):
            runtime.run(InterruptedFeed(), StopSignal())
    else:
        runtime.run(InterruptedFeed(), StopSignal())
    assert statuses[-1]["terminal"]
    assert statuses[-1]["open_position"] is None
    assert runtime._broker.position is None
    assert (
        statuses[-1]["run_identity"]["run_parameters"]["end_of_data_policy"]
        == "flatten_at_last_close"
    )
