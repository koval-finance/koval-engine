"""Deterministic market, risk, decision, and execution event ordering.

The contract is intentionally venue-neutral. Sources retain their native sequence
where one exists, while a published kind priority resolves equal timestamps across
independent streams. The resulting journal is content-addressed and replayable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from koval.engine.run_identity import content_sha256

EVENT_TIMELINE_VERSION: Final = "koval_canonical_event_timeline_v1"

EVENT_KIND_PRIORITY: Final = {
    "instrument_rules": 10,
    "fee_schedule": 20,
    "book_snapshot": 30,
    "book_delta": 40,
    "index_price": 50,
    "mark_price": 60,
    "funding": 70,
    "trade": 80,
    "candle_close": 90,
    "liquidation_check": 100,
    "strategy_decision": 110,
    "order_intent": 120,
    "order_acknowledgement": 130,
    "order_cancel": 140,
    "fill": 150,
    "fee": 160,
    "ledger_snapshot": 170,
}


class EventTimelineError(ValueError):
    """The source stream cannot be replayed without inventing event order."""


@dataclass(frozen=True)
class CanonicalEvent:
    """One normalized event before a deterministic timeline ordinal is assigned."""

    event_id: str
    kind: str
    timestamp_ms: int
    source: str
    payload: Mapping[str, object]
    source_sequence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("canonical event_id is required")
        if self.kind not in EVENT_KIND_PRIORITY:
            raise ValueError(f"unsupported canonical event kind: {self.kind}")
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("canonical event timestamp must be a non-negative integer")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("canonical event source is required")
        if not isinstance(self.payload, Mapping):
            raise ValueError("canonical event payload must be a mapping")
        if self.source_sequence is not None and (
            isinstance(self.source_sequence, bool)
            or not isinstance(self.source_sequence, int)
            or self.source_sequence < 0
        ):
            raise ValueError("canonical source sequence must be a non-negative integer")
        # Validate serializability and finite numeric values at the contract boundary.
        content_sha256(dict(self.payload))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class CanonicalEventTimeline:
    """An immutable, ordered, content-addressed replay journal."""

    events: tuple[CanonicalEvent, ...]
    timeline_sha256: str
    version: str = EVENT_TIMELINE_VERSION

    @classmethod
    def build(cls, events: list[CanonicalEvent] | tuple[CanonicalEvent, ...]):
        supplied = tuple(events)
        _validate_identities(supplied)
        _validate_sources(supplied)
        ordered = tuple(sorted(supplied, key=_sort_key))
        records = [_record(item, ordinal=index) for index, item in enumerate(ordered, 1)]
        identity = {"version": EVENT_TIMELINE_VERSION, "events": records}
        return cls(events=ordered, timeline_sha256=content_sha256(identity))

    def as_records(self) -> list[dict[str, object]]:
        """Return strict JSON-compatible records with stable one-based ordinals."""
        return [_record(item, ordinal=index) for index, item in enumerate(self.events, 1)]


def _sort_key(event: CanonicalEvent) -> tuple[object, ...]:
    sequence = event.source_sequence if event.source_sequence is not None else -1
    return (
        event.timestamp_ms,
        EVENT_KIND_PRIORITY[event.kind],
        event.source,
        sequence,
        event.event_id,
    )


def _record(event: CanonicalEvent, *, ordinal: int) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "event_id": event.event_id,
        "kind": event.kind,
        "timestamp_ms": event.timestamp_ms,
        "source": event.source,
        "source_sequence": event.source_sequence,
        "payload": dict(event.payload),
    }


def _validate_identities(events: tuple[CanonicalEvent, ...]) -> None:
    seen: set[str] = set()
    for event in events:
        if event.event_id in seen:
            raise EventTimelineError(f"duplicate event_id {event.event_id}")
        seen.add(event.event_id)


def _validate_sources(events: tuple[CanonicalEvent, ...]) -> None:
    by_source: dict[str, list[CanonicalEvent]] = {}
    for event in events:
        by_source.setdefault(event.source, []).append(event)
    for source, stream in by_source.items():
        _validate_source_timestamps(source, stream)
        _validate_source_sequence(source, stream)


def _validate_source_timestamps(source: str, events: list[CanonicalEvent]) -> None:
    previous = -1
    for event in events:
        if event.timestamp_ms < previous:
            raise EventTimelineError(
                f"timestamp regression for source {source}: {event.timestamp_ms} follows {previous}"
            )
        previous = event.timestamp_ms


def _validate_source_sequence(source: str, events: list[CanonicalEvent]) -> None:
    supplied = [event.source_sequence is not None for event in events]
    if any(supplied) and not all(supplied):
        raise EventTimelineError(f"mixed sequenced and unsequenced events for source {source}")
    if not any(supplied):
        return
    ordered = sorted(events, key=lambda event: (event.source_sequence, event.event_id))
    previous = ordered[0]
    for current in ordered[1:]:
        assert previous.source_sequence is not None
        assert current.source_sequence is not None
        expected = previous.source_sequence + 1
        if current.source_sequence != expected:
            raise EventTimelineError(
                f"sequence gap for source {source}: expected {expected}, "
                f"received {current.source_sequence}"
            )
        previous = current
    chronological = sorted(events, key=lambda event: (event.timestamp_ms, event.event_id))
    prior = chronological[0]
    for current in chronological[1:]:
        assert prior.source_sequence is not None
        assert current.source_sequence is not None
        if (
            current.timestamp_ms > prior.timestamp_ms
            and current.source_sequence <= prior.source_sequence
        ):
            raise EventTimelineError(
                f"sequence regression for source {source}: {current.source_sequence} "
                f"at {current.timestamp_ms} follows {prior.source_sequence}"
            )
        prior = current


__all__ = [
    "EVENT_KIND_PRIORITY",
    "EVENT_TIMELINE_VERSION",
    "CanonicalEvent",
    "CanonicalEventTimeline",
    "EventTimelineError",
]
