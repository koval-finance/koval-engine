"""Durable input/event interface independent of UI callbacks."""

import copy

import numpy as np
import pytest

from koval.engine.event_timeline import CanonicalEvent, CanonicalEventTimeline
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import ReplayFeed, StopSignal
from tests.engine.live_fixtures import ema_cross_graph
from tests.engine.test_live_engine import _SingleEntryStrategy
from tests.engine.test_runtime_contract import EXECUTION

ROWS = np.array([[t, 100, 101, 99, 100, 17] for t in range(0, 180_000, 60_000)])


def runtime(monkeypatch, records, **kwargs):
    import inspect

    assert "on_record" in inspect.signature(LiveEngine).parameters
    monkeypatch.setattr(
        "koval.engine.live_engine.assemble_from_graph",
        lambda _: _SingleEntryStrategy(stop_loss=90, take_profit=120),
    )
    return LiveEngine(
        {},
        LiveEngineConfig("BTCUSDT", "1m", 10_000, execution=EXECUTION | {"commission_bps": 10}),
        session_id="replay-1",
        on_record=records.append,
        **kwargs,
    )


def test_input_is_archived_before_decision_and_volume_survives(monkeypatch):
    records, bars = [], []
    engine = runtime(monkeypatch, records, on_bar=bars.append)
    engine.run(ReplayFeed(ROWS), StopSignal())
    kinds = [r["kind"] for r in records]
    assert kinds.index("bar_received") < kinds.index("decision") < kinds.index("order_intent")
    archived = [r for r in records if r["kind"] == "bar_received"]
    assert [r["payload"]["ohlcv"] for r in archived] == ROWS.tolist()
    assert [b["volume"] for b in bars] == [17, 17, 17]
    assert all(r["received_timestamp_ms"] > 0 for r in records)
    assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))
    assert len({r["record_id"] for r in records}) == len(records)
    assert engine.checkpoint["last_processed_bar_ms"] == 120_000
    assert engine.checkpoint["last_accepted_bar_ms"] == 120_000
    assert engine.checkpoint["record_sha256"] == records[-1]["sha256"]
    intents = [r for r in records if r["kind"] == "order_intent"]
    fills = [r for r in records if r["kind"] == "fill"]
    cashflows = [r for r in records if r["kind"] == "cashflow"]
    assert intents[0]["payload"]["decision_id"]
    assert fills and cashflows
    assert all(r["payload"]["fill_id"] for r in fills)
    assert all(r["payload"]["cashflow_id"] for r in cashflows)
    assert all(r["payload"]["reference_id"] for r in cashflows)
    assert any(r["kind"] == "account_snapshot" for r in records)


def test_identical_reconnect_tail_does_not_repeat_decision_or_fee(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    engine.run(ReplayFeed(np.vstack([ROWS[0], ROWS[0], ROWS[1:]])), StopSignal())
    assert len([r for r in records if r["kind"] == "bar_received"]) == 3
    assert len([r for r in records if r["kind"] == "bar_duplicate"]) == 1


def test_each_completed_bar_emits_a_broker_and_strategy_checkpoint(monkeypatch):
    records, checkpoints = [], []
    engine = runtime(
        monkeypatch, records, on_checkpoint=lambda value: checkpoints.append(copy.deepcopy(value))
    )

    engine.run(ReplayFeed(ROWS), StopSignal())

    assert [item["last_processed_bar_ms"] for item in checkpoints] == [0, 60_000, 120_000]
    assert all(
        item["broker"]["version"] == "koval_paper_broker_checkpoint_v1" for item in checkpoints
    )
    assert all(
        item["strategy"]["version"] == "koval_strategy_checkpoint_v1" for item in checkpoints
    )
    assert checkpoints[-1]["record_sha256"]


def test_runtime_journal_restores_only_an_exact_verified_prefix():
    from koval.engine.runtime_journal import RuntimeJournal

    records = []
    original = RuntimeJournal("resume-1", records.append)
    original.write("bar_received", 0, {"ohlcv": ROWS[0].tolist(), "phase": "evaluation"})
    original.write("bar_processed", 0, {"phase": "evaluation"})

    resumed_records = list(records)
    resumed = RuntimeJournal("resume-1", resumed_records.append)
    resumed.restore(original.checkpoint, records)
    resumed.write("bar_received", 60_000, {"ohlcv": ROWS[1].tolist(), "phase": "evaluation"})

    assert resumed_records[-1]["sequence"] == 3
    assert resumed_records[-1]["previous_sha256"] == records[-1]["sha256"]
    assert resumed.checkpoint["last_processed_bar_ms"] == 0

    wrong = {**original.checkpoint, "record_sha256": "0" * 64}
    with pytest.raises(ValueError, match="checkpoint does not match"):
        RuntimeJournal("resume-1", None).restore(wrong, records)


def test_live_engine_journals_canonical_market_events_exactly_once(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    timeline = CanonicalEventTimeline.build(
        [
            CanonicalEvent(
                event_id="trade-41",
                kind="trade",
                timestamp_ms=1,
                source="binance_usdm_aggregate_trades",
                source_sequence=41,
                payload={"price": "100.1", "quantity": "0.2"},
            ),
            CanonicalEvent(
                event_id="trade-42",
                kind="trade",
                timestamp_ms=2,
                source="binance_usdm_aggregate_trades",
                source_sequence=42,
                payload={"price": "100.2", "quantity": "0.3"},
            ),
        ]
    )

    assert engine.apply_canonical_event_timeline(timeline) == 2
    assert engine.apply_canonical_event_timeline(timeline) == 0
    archived = [record for record in records if record["kind"] == "market_event"]
    assert [record["payload"]["event_id"] for record in archived] == ["trade-41", "trade-42"]
    assert engine.checkpoint["engine_state"]["canonical_event_ids"] == [
        "trade-41",
        "trade-42",
    ]


def test_live_engine_rejects_conflicting_market_event_retry_before_and_after_restore(monkeypatch):
    records = []
    config = LiveEngineConfig("BTCUSDT", "1m", 10_000)
    engine = LiveEngine(
        ema_cross_graph(),
        config,
        session_id="market-event-retry",
        on_record=records.append,
    )
    original = CanonicalEventTimeline.build(
        [
            CanonicalEvent(
                event_id="trade-41",
                kind="trade",
                timestamp_ms=1,
                source="trades",
                payload={"price": "100"},
                source_sequence=41,
            )
        ]
    )
    conflict = CanonicalEventTimeline.build(
        [
            CanonicalEvent(
                event_id="trade-41",
                kind="trade",
                timestamp_ms=1,
                source="trades",
                payload={"price": "101"},
                source_sequence=41,
            )
        ]
    )

    engine.apply_canonical_event_timeline(original)
    with pytest.raises(ValueError, match="changed content"):
        engine.apply_canonical_event_timeline(conflict)

    checkpoint = copy.deepcopy(engine.checkpoint)
    restored = LiveEngine(
        ema_cross_graph(),
        config,
        session_id="market-event-retry",
        on_record=[].append,
    )
    restored.restore_checkpoint(checkpoint, records)
    with pytest.raises(ValueError, match="changed content"):
        restored.apply_canonical_event_timeline(conflict)


def test_live_engine_accepts_identical_rebatched_market_event_before_and_after_restore():
    records = []
    config = LiveEngineConfig("BTCUSDT", "1m", 10_000)
    engine = LiveEngine(
        ema_cross_graph(),
        config,
        session_id="market-event-rebatch",
        on_record=records.append,
    )
    trade_41 = CanonicalEvent(
        event_id="trade-41",
        kind="trade",
        timestamp_ms=1,
        source="trades",
        payload={"price": "100"},
        source_sequence=41,
    )
    trade_42 = CanonicalEvent(
        event_id="trade-42",
        kind="trade",
        timestamp_ms=2,
        source="trades",
        payload={"price": "101"},
        source_sequence=42,
    )

    assert (
        engine.apply_canonical_event_timeline(CanonicalEventTimeline.build([trade_41, trade_42]))
        == 2
    )
    rebatched = CanonicalEventTimeline.build([trade_42])
    assert engine.apply_canonical_event_timeline(rebatched) == 0

    checkpoint = copy.deepcopy(engine.checkpoint)
    restored = LiveEngine(
        ema_cross_graph(),
        config,
        session_id="market-event-rebatch",
        on_record=[].append,
    )
    restored.restore_checkpoint(checkpoint, records)

    assert restored.apply_canonical_event_timeline(rebatched) == 0


def test_live_engine_restores_economic_state_without_repeating_prior_bars():
    graph = ema_cross_graph()
    rows = np.array(
        [
            [t, 100 + t / 60_000, 102 + t / 60_000, 99, 101 + t / 60_000, 17]
            for t in range(0, 300_000, 60_000)
        ]
    )
    records = []
    checkpoints = []
    original = LiveEngine(
        graph,
        LiveEngineConfig("BTCUSDT", "1m", 10_000),
        session_id="resume-economic",
        on_record=records.append,
        on_checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)),
    )
    for row in rows[:3]:
        original._process_bar(row)  # noqa: SLF001

    checkpoint = checkpoints[-1]
    prefix = copy.deepcopy(records)
    original._process_bar(rows[3])  # noqa: SLF001
    expected = original.checkpoint

    resumed_records = copy.deepcopy(prefix)
    restored = LiveEngine(
        graph,
        LiveEngineConfig("BTCUSDT", "1m", 10_000),
        session_id="resume-economic",
        on_record=resumed_records.append,
    )
    restored.restore_checkpoint(checkpoint, prefix)
    restored._process_bar(rows[3])  # noqa: SLF001
    actual = restored.checkpoint

    assert actual["bar_index"] == expected["bar_index"]
    assert actual["last_processed_bar_ms"] == expected["last_processed_bar_ms"]
    assert actual["broker"] == expected["broker"]
    assert actual["strategy"] == expected["strategy"]
    assert actual["engine_state"] == expected["engine_state"]
    assert [
        record["payload"]["ohlcv"] for record in resumed_records if record["kind"] == "bar_received"
    ] == rows[:4].tolist()


def test_live_engine_rejects_resume_when_journal_advanced_past_checkpoint():
    graph = ema_cross_graph()
    records = []
    engine = LiveEngine(
        graph,
        LiveEngineConfig("BTCUSDT", "1m", 10_000),
        session_id="resume-gap",
        on_record=records.append,
    )
    engine._process_bar(ROWS[0])  # noqa: SLF001
    checkpoint = copy.deepcopy(engine.checkpoint)
    engine._journal.write("uncheckpointed_event", 1, {})  # noqa: SLF001

    restored = LiveEngine(
        graph,
        LiveEngineConfig("BTCUSDT", "1m", 10_000),
        session_id="resume-gap",
    )
    with pytest.raises(ValueError, match="journal head does not equal runtime checkpoint"):
        restored.restore_checkpoint(checkpoint, records)


def test_conflicting_reconnect_tail_is_visible_and_fails(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    conflict = ROWS[0].copy()
    conflict[5] = 0
    with pytest.raises(ValueError, match="conflicting"):
        engine.run(ReplayFeed(np.vstack([ROWS[0], conflict])), StopSignal())
    assert any(r["kind"] == "bar_conflict" for r in records)


def test_archive_failure_prevents_the_unrecorded_decision(monkeypatch):
    records = []

    class Sink:
        def append(self, record):
            if record["kind"] == "bar_received":
                raise OSError("archive offline")
            records.append(copy.deepcopy(record))

    engine = runtime(monkeypatch, Sink())
    with pytest.raises(OSError, match="archive offline"):
        engine.run(ReplayFeed(ROWS), StopSignal())
    assert not any(r["kind"] in {"decision", "order_intent"} for r in records)
    assert engine.checkpoint["last_accepted_bar_ms"] is None


def test_each_execution_cashflow_links_to_its_actual_fill(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    engine.run(ReplayFeed(ROWS), StopSignal())
    fills = {r["payload"]["fill_id"]: r["payload"] for r in records if r["kind"] == "fill"}
    cashflows = [r["payload"] for r in records if r["kind"] == "cashflow"]
    assert cashflows
    for cashflow in cashflows:
        assert cashflow["fill_id"] in fills
        fill = fills[cashflow["fill_id"]]
        assert cashflow["timestamp_ms"] == fill["timestamp_ms"]
        if cashflow["kind"] == "commission":
            assert -cashflow["amount"] == pytest.approx(fill["metadata"]["commission"])


def test_journal_verifier_detects_deleted_or_mutated_records(monkeypatch):
    import koval.engine.runtime_journal as journal

    assert hasattr(journal, "verify_runtime_records")
    records = []
    engine = runtime(monkeypatch, records)
    engine.run(ReplayFeed(ROWS), StopSignal())
    assert journal.verify_runtime_records(records) == engine.checkpoint["record_sha256"]
    broken = copy.deepcopy(records)
    broken[1]["payload"]["changed"] = True
    for invalid in (broken, records[:2] + records[3:]):
        with pytest.raises(ValueError, match="journal"):
            journal.verify_runtime_records(invalid)


def test_protection_decisions_also_have_recorded_account_context(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    engine.run(ReplayFeed(ROWS), StopSignal())
    decisions = [r for r in records if r["kind"] == "decision"]
    assert [r["event_timestamp_ms"] for r in decisions] == [60_000, 120_000, 180_000]
    assert decisions[1]["payload"]["account"]["open_positions"] == 1
    results = [r for r in records if r["kind"] == "decision_result"]
    assert results[0]["payload"]["decision_id"] == decisions[0]["record_id"]


def test_epoch_fill_event_time_is_not_replaced_with_last_bar_time(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    engine._process_bar(ROWS[1])
    engine._on_fill({"timestamp_ms": 0, "metadata": {}})
    assert records[-1]["event_timestamp_ms"] == 0


def test_entry_intent_clock_is_the_decision_close_not_the_candle_open(monkeypatch):
    records = []
    engine = runtime(monkeypatch, records)
    engine.run(ReplayFeed(ROWS), StopSignal())
    intent = next(r for r in records if r["kind"] == "order_intent")
    assert intent["event_timestamp_ms"] == 60_000
    assert intent["payload"]["timestamp_basis"] == "decision_clock"
