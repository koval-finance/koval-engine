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
        self.last_received_timestamp_ms: int | None = None

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
        self.last_received_timestamp_ms = record["received_timestamp_ms"]
        if kind == "bar_received":
            self.accepted_bar = timestamp_ms
        if kind == "bar_processed":
            self.processed_bar = timestamp_ms
        return record_id

    def restore(self, checkpoint: dict, records) -> None:
        """Continue one verified journal prefix without rewriting prior events.

        The host must provide the retained records because a digest alone cannot
        prove that the archive prefix is complete.  A checkpoint behind or ahead
        of the supplied head is rejected; recovery never truncates evidence.
        """
        retained = tuple(deepcopy(list(records)))
        if checkpoint.get("version") != "koval_runtime_checkpoint_v1":
            raise ValueError("unsupported runtime checkpoint version")
        if checkpoint.get("session_id") != self.session_id:
            raise ValueError("runtime checkpoint session mismatch")
        sequence = checkpoint.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or sequence != len(retained)
            or checkpoint.get("record_sha256") != verify_runtime_records(retained)
        ):
            raise ValueError("runtime checkpoint does not match retained journal prefix")

        accepted = next(
            (
                record["event_timestamp_ms"]
                for record in reversed(retained)
                if record.get("kind") == "bar_received"
            ),
            None,
        )
        processed = next(
            (
                record["event_timestamp_ms"]
                for record in reversed(retained)
                if record.get("kind") == "bar_processed"
            ),
            None,
        )
        if (
            checkpoint.get("last_accepted_bar_ms") != accepted
            or checkpoint.get("last_processed_bar_ms") != processed
        ):
            raise ValueError("runtime checkpoint bar cursor does not match retained journal")
        if bool(checkpoint.get("archive_enabled")) != (self.sink is not None):
            raise ValueError("runtime checkpoint archive mode mismatch")

        self.sequence = sequence
        self.sha256 = checkpoint.get("record_sha256")
        self.accepted_bar = accepted
        self.processed_bar = processed
        self.last_received_timestamp_ms = (
            retained[-1]["received_timestamp_ms"] if retained else None
        )

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
