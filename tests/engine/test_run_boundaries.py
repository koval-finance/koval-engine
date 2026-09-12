"""Explicit common warmup, evaluation and initial risk contract."""

import json

import numpy as np
import pytest

from koval.engine.backtest_engine import (
    EngineRunSpec,
    ExecutionCapabilities,
    ProtocolVersionError,
    check_protocol_version,
    negotiate_execution_capabilities,
)
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from tests.engine.test_live_engine import _SingleEntryStrategy
from tests.engine.test_runtime_contract import EXECUTION

CONTRACT = {
    "version": "koval_runtime_boundaries_v1",
    "warmup_start_ms": 0,
    "evaluation_start_ms": 120_000,
    "evaluation_end_ms": 240_000,
    "decision_clock": "bar_close",
    "initial_balance": 10_000.0,
    "daily_baseline_equity": 11_000.0,
    "peak_equity": 12_000.0,
    "end_of_data_policy": "mark_at_last_close",
}
ROWS = np.array([[t, 100, 101, 99, 100, 17] for t in range(0, 300_000, 60_000)])


def test_old_plugin_must_not_silently_ignore_explicit_runtime_boundaries():
    assert "runtime_contract" in EngineRunSpec.__dataclass_fields__
    spec = EngineRunSpec({}, {}, 10_000, runtime_contract=json.loads(json.dumps(CONTRACT)))
    with pytest.raises(ProtocolVersionError, match="runtime_boundaries_v1"):
        negotiate_execution_capabilities(spec, ExecutionCapabilities((1, 2), ()))
    check_protocol_version(EngineRunSpec({}, {}, 10_000, protocol_version=1))
    with pytest.raises(ProtocolVersionError, match="protocol"):
        check_protocol_version(
            EngineRunSpec({}, {}, 10_000, protocol_version=1, runtime_contract=CONTRACT)
        )


@pytest.mark.parametrize("preload", [False, True])
def test_warmup_is_never_a_decision_or_cashflow_period(monkeypatch, preload):
    assert "runtime_contract" in LiveEngineConfig.__dataclass_fields__
    strategy = _SingleEntryStrategy(stop_loss=90, take_profit=120)
    decisions = []
    original = strategy.should_long

    def decide():
        decisions.append(strategy.timestamp_ms)
        return original()

    strategy.should_long = decide
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda _, strategy=strategy: strategy
    )
    intents, statuses = [], []
    runtime = LiveEngine(
        {},
        LiveEngineConfig(
            "BTCUSDT",
            "1m",
            10_000,
            execution=EXECUTION,
            history=ROWS[:2] if preload else None,
            runtime_contract=CONTRACT,
        ),
        on_order_intent=intents.append,
        on_status=statuses.append,
    )
    runtime.run(ReplayFeed(ROWS[2:] if preload else ROWS), StopSignal())
    assert len(intents) == 1
    assert len(decisions) == 1
    assert runtime._warmup_identity.as_dict()["row_count"] == 2
    assert runtime._primary_identity.as_dict()["row_count"] == 2
    assert runtime._account.snapshot().peak_equity == 12_000
    assert runtime._account.snapshot().daily_pnl == -1000
    assert all(e.timestamp_ms >= 120_000 for e in runtime._account.ledger.entries)
    assert statuses[-1]["run_identity"]["run_parameters"]["runtime_contract"] == CONTRACT
    assert statuses[-1]["open_position"] is not None


@pytest.mark.parametrize(
    "updates",
    [
        {"evaluation_start_ms": 1},
        {"decision_clock": "bar_open"},
        {"peak_equity": float("nan")},
        {"unknown": 42},
        {"initial_balance": 9_999},
    ],
)
def test_invalid_runtime_contract_is_refused_before_start(updates):
    assert "runtime_contract" in LiveEngineConfig.__dataclass_fields__
    with pytest.raises(ValueError):
        LiveEngine(
            {}, LiveEngineConfig("BTCUSDT", "1m", 10_000, runtime_contract=CONTRACT | updates)
        )


def test_missing_warmup_and_truncated_evaluation_fail(monkeypatch):
    assert "runtime_contract" in LiveEngineConfig.__dataclass_fields__
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph", lambda _: _SingleEntryStrategy()
    )
    for rows in (ROWS[1:4], ROWS[:3]):
        runtime = LiveEngine(
            {},
            LiveEngineConfig(
                "BTCUSDT", "1m", 10_000, execution=EXECUTION, runtime_contract=CONTRACT
            ),
        )
        with pytest.raises(ValueError, match="runtime.*coverage"):
            runtime.run(ReplayFeed(rows), StopSignal())


def test_actual_preroll_exceeding_window_has_identical_decision_indices(monkeypatch):
    indices = []
    for preload in (False, True):
        strategy = _SingleEntryStrategy(stop_loss=90, take_profit=120)
        monkeypatch.setattr(
            "koval.engine.live_engine.assemble_from_graph", lambda _, strategy=strategy: strategy
        )
        intents = []
        engine = LiveEngine(
            {},
            LiveEngineConfig(
                "BTCUSDT",
                "1m",
                10_000,
                max_window=1,
                history=ROWS[:2] if preload else None,
                execution=EXECUTION,
                runtime_contract=CONTRACT,
            ),
            session_id="same-input",
            on_order_intent=intents.append,
        )
        engine.run(ReplayFeed(ROWS[2:] if preload else ROWS), StopSignal())
        indices.append(intents[0]["intent_id"])
    assert indices[0] == indices[1]


def test_public_graph_fixture_and_future_mutation_preserve_evaluation():
    from importlib.resources import files

    path = files("koval").joinpath("examples/runtime/explicit_boundaries_v1.json")
    assert path.is_file()
    fixture = json.loads(path.read_text())
    results = []
    for future_price in (105, 1_000_000):
        candles = np.array(fixture["candles"], dtype=float)
        candles[-1, 1:5] = future_price
        records, statuses = [], []
        engine = LiveEngine(
            fixture["graph"],
            LiveEngineConfig(
                "BTCUSDT",
                "1m",
                10_000,
                exchange="binance",
                execution=fixture["execution"],
                runtime_contract=fixture["runtime_contract"],
            ),
            on_record=records.append,
            on_status=statuses.append,
            session_id="fixture",
        )
        engine.run(ReplayFeed(candles), StopSignal())
        terminal = statuses[-1]["metrics"]
        for name, value in fixture["expected_metrics"].items():
            assert terminal[name] == pytest.approx(value)
        results.append([(r["kind"], r["event_timestamp_ms"], r["payload"]) for r in records])
    assert results[0] == results[1]
