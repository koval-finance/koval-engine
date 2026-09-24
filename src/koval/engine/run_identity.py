"""Versioned content identities; archive retention and promotion belong to the host."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from decimal import Decimal

import numpy as np

from koval.engine.market_data import canonical_candle_bytes
from koval.engine.market_identity import MarketIdentity
from koval.exchanges.base import timeframe_ms

RUN_IDENTITY_VERSION = "koval_run_identity_v1"
RUNTIME_CONTRACT_VERSION = "koval_runtime_v2"


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("_")
            and field.name not in {"raw_response", "raw_responses"}
        }
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def content_sha256(value: object) -> str:
    """Hash normalized JSON (sorted keys, UTF-8, compact, no non-finite values).

    Dataclass private caches and raw transport responses are excluded. Decimal
    values are strings; callers archive normalized evidence alongside raw pages.
    """
    payload = json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def execution_evidence_manifest(
    *,
    funding=None,
    fee_schedule=None,
    instrument_specs=(),
    mark_prices=None,
    execution_proxy=None,
) -> dict[str, dict]:
    values = dict(
        funding=funding,
        fee_schedule=fee_schedule,
        instrument_specs=instrument_specs,
        mark_prices=mark_prices,
        execution_proxy=execution_proxy,
    )
    return {
        name: {
            "status": "supplied" if value else "unavailable",
            "sha256": content_sha256(value) if value else None,
        }
        for name, value in values.items()
    }


class CandleStreamIdentity:
    """Constant-memory identity of chronological closed candles actually consumed.

    The preimage is ``b'KOVAL-CANDLE-STREAM-V1\\n'`` followed by each row encoded
    using ``canonical_candle_bytes(row.reshape(1, 6))``. This streaming encoding
    is explicitly distinct from a batch CandleDataset's encoding and hash.
    """

    def __init__(self, timeframe: str) -> None:
        self._step = timeframe_ms(timeframe)
        self._timeframe = timeframe
        self._hash = hashlib.sha256(b"KOVAL-CANDLE-STREAM-V1\n")
        self._count = 0
        self._start: int | None = None
        self._end: int | None = None

    def append(self, row) -> None:
        values = np.asarray(row, dtype=np.float64)
        if values.shape != (6,):
            raise ValueError("stream candle must have six values")
        payload = canonical_candle_bytes(values.reshape(1, 6))
        timestamp = int(values[0])
        if timestamp % self._step or (self._end is not None and timestamp != self._end):
            raise ValueError("stream candle continuity or alignment violation")
        self._hash.update(payload)
        if self._start is None:
            self._start = timestamp
        self._end = timestamp + self._step
        self._count += 1

    def as_dict(self) -> dict:
        return {
            "encoding_version": "koval_candle_stream_sha256_v1",
            "sha256": self._hash.hexdigest(),
            "timeframe": self._timeframe,
            "row_count": self._count,
            "actual_start_ms": self._start,
            "actual_end_ms": self._end,
        }


def build_run_identity(
    *,
    market_identity: MarketIdentity | None,
    primary: dict,
    warmup: dict,
    execution_profile: dict,
    execution_evidence: dict,
    strategy_sha256: str,
    run_parameters: dict,
    engine_version: str,
    execution_mode: str,
) -> dict:
    """Identify a simulation, never certify archival completeness or venue parity."""
    identified = (
        market_identity is not None and primary["row_count"] > 0 and execution_mode == "paper"
    )
    execution = {
        "profile": execution_profile,
        "evidence": execution_evidence,
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
    }
    return {
        "schema_version": RUN_IDENTITY_VERSION,
        "engine_version": engine_version,
        "execution_mode": execution_mode,
        "market_identity": None if market_identity is None else market_identity.as_dict(),
        "dataset_identity": {"primary": primary, "warmup": warmup},
        "execution_identity": {**execution, "sha256": content_sha256(execution)},
        "strategy_sha256": strategy_sha256,
        "run_parameters": run_parameters,
        "reproducibility_grade": "identified_simulation" if identified else "not_comparable",
        "unavailable_effects": sorted(
            name for name, item in execution_evidence.items() if item["status"] == "unavailable"
        ),
    }


__all__ = [
    "RUN_IDENTITY_VERSION",
    "RUNTIME_CONTRACT_VERSION",
    "CandleStreamIdentity",
    "build_run_identity",
    "content_sha256",
    "execution_evidence_manifest",
]
