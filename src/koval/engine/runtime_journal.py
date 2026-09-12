"""Synchronous append acknowledgement and tamper-evident runtime records.

The host owns storage, single-writer ownership and fsync/transaction semantics.
A successful sink return acknowledges that record. This is an archive boundary,
not a broker checkpoint or permission to resume exposure after a restart.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy

from koval.engine.run_identity import content_sha256


class RuntimeJournal:
    def __init__(self, session_id: str, sink: Callable[[dict], None] | None) -> None:
        self.session_id = session_id
        self.sink = sink
        self.sequence = 0
        self.sha256: str | None = None
        self.accepted_bar: int | None = None
        self.processed_bar: int | None = None

    def write(self, kind: str, timestamp_ms: int, payload: dict) -> str:
        sequence = self.sequence + 1
        record_id = f"{self.session_id}:{sequence}"
        record = {
            "version": "koval_runtime_journal_v1",
            "session_id": self.session_id,
            "record_id": record_id,
            "sequence": sequence,
            "kind": kind,
            "event_timestamp_ms": timestamp_ms,
            "received_timestamp_ms": time.time_ns() // 1_000_000,
            "previous_sha256": self.sha256,
            "payload": deepcopy(payload),
        }
        digest = content_sha256(record)
        record["sha256"] = digest
        if self.sink is not None:
            self.sink(record)
        self.sequence, self.sha256 = sequence, digest
        if kind == "bar_received":
            self.accepted_bar = timestamp_ms
        if kind == "bar_processed":
            self.processed_bar = timestamp_ms
        return record_id

    @property
    def checkpoint(self) -> dict:
        return {
            "version": "koval_runtime_checkpoint_v1",
            "session_id": self.session_id,
            "archive_enabled": self.sink is not None,
            "sequence": self.sequence,
            "record_sha256": self.sha256,
            "last_accepted_bar_ms": self.accepted_bar,
            "last_processed_bar_ms": self.processed_bar,
            "recovery": "reconcile_broker_and_replay_required",
        }


def verify_runtime_records(records) -> str | None:
    """Verify a complete prefix and return its last hash for checkpoint comparison.

    A missing final suffix is detectable only against a separately retained
    checkpoint. Hash chaining alone cannot prove the archive has its last record.
    """
    previous, session = None, None
    for expected, record in enumerate(records, 1):
        value = deepcopy(record)
        digest = value.pop("sha256", None)
        if session is None:
            session = value.get("session_id")
        if (
            value.get("version") != "koval_runtime_journal_v1"
            or not session
            or value.get("session_id") != session
            or value.get("sequence") != expected
            or value.get("record_id") != f"{session}:{expected}"
            or value.get("previous_sha256") != previous
            or digest != content_sha256(value)
        ):
            raise ValueError(f"runtime journal integrity failure at sequence {expected}")
        previous = digest
    return previous
