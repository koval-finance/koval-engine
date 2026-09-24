"""Canonical Futures event ordering and continuity tests."""

import pytest

from koval.engine.event_timeline import (
    CanonicalEvent,
    CanonicalEventTimeline,
    EventTimelineError,
)

T0 = 1_700_000_000_000


def event(
    kind: str,
    event_id: str,
    *,
    timestamp_ms: int = T0,
    source: str = "binance:BTCUSDT",
    sequence: int | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        kind=kind,
        timestamp_ms=timestamp_ms,
        source=source,
        source_sequence=sequence,
        payload={"event_id": event_id},
    )


def test_equal_timestamp_events_follow_the_published_risk_before_decision_policy():
    timeline = CanonicalEventTimeline.build(
        [
            event("order_intent", "intent"),
            event("strategy_decision", "decision"),
            event("trade", "trade"),
            event("funding", "funding"),
            event("mark_price", "mark"),
            event("instrument_rules", "rules"),
            event("liquidation_check", "liquidation"),
        ]
    )

    assert [item.kind for item in timeline.events] == [
        "instrument_rules",
        "mark_price",
        "funding",
        "trade",
        "liquidation_check",
        "strategy_decision",
        "order_intent",
    ]


def test_source_sequence_orders_events_within_the_same_phase_and_timestamp():
    timeline = CanonicalEventTimeline.build(
        [event("book_delta", "second", sequence=12), event("book_delta", "first", sequence=11)]
    )

    assert [item.event_id for item in timeline.events] == ["first", "second"]


def test_identical_input_produces_a_stable_timeline_hash():
    left = CanonicalEventTimeline.build(
        [event("mark_price", "mark"), event("strategy_decision", "decision")]
    )
    right = CanonicalEventTimeline.build(
        [event("strategy_decision", "decision"), event("mark_price", "mark")]
    )

    assert left.timeline_sha256 == right.timeline_sha256
    assert left.as_records() == right.as_records()


def test_duplicate_event_identity_is_rejected_even_when_payload_matches():
    item = event("trade", "duplicate", sequence=1)

    with pytest.raises(EventTimelineError, match="duplicate event_id duplicate"):
        CanonicalEventTimeline.build([item, item])


def test_gap_in_a_sequence_preserving_source_is_rejected():
    with pytest.raises(EventTimelineError, match="sequence gap.*expected 8.*received 9"):
        CanonicalEventTimeline.build(
            [event("book_delta", "seven", sequence=7), event("book_delta", "nine", sequence=9)]
        )


def test_sequence_regression_at_a_later_timestamp_is_rejected():
    with pytest.raises(EventTimelineError, match="sequence regression"):
        CanonicalEventTimeline.build(
            [
                event("trade", "later-sequence", timestamp_ms=T0, sequence=4),
                event("trade", "lower-sequence", timestamp_ms=T0 + 1, sequence=3),
            ]
        )


def test_sequence_presence_cannot_change_inside_one_source_stream():
    with pytest.raises(EventTimelineError, match="mixed sequenced and unsequenced"):
        CanonicalEventTimeline.build(
            [event("trade", "sequenced", sequence=1), event("trade", "unsequenced")]
        )


def test_unknown_event_kind_is_rejected():
    with pytest.raises(ValueError, match="unsupported canonical event kind"):
        event("mystery", "unknown")


def test_timeline_requires_monotonic_source_timestamps():
    with pytest.raises(EventTimelineError, match="timestamp regression"):
        CanonicalEventTimeline.build(
            [
                event("trade", "newer", timestamp_ms=T0 + 1),
                event("trade", "older", timestamp_ms=T0),
            ]
        )
