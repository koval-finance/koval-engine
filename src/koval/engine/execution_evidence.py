"""MIT-owned JSON boundary for normalized simulation evidence.

Strict JSON decoding prevents coercion of booleans/fractional timestamps and
rejects unknown nested settings. Domain dataclasses retain their own validation.
Supplied evidence is caller-provided: this transport does not certify its source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal

from pydantic import ConfigDict, TypeAdapter

from koval.engine.execution_proxy import ExecutionProxyConfig
from koval.engine.fee_evidence import FeeScheduleEvidence
from koval.engine.funding import FundingSeries
from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MarkPriceRecord,
    MarkPriceSeries,
)
from koval.engine.run_identity import execution_evidence_manifest

EVIDENCE_KEYS = frozenset(
    {"funding", "fee_schedule", "instrument_specs", "mark_prices", "execution_proxy"}
)


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if field.init and field.name != "raw_responses"
        }
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


@dataclass(frozen=True)
class ExecutionEvidence:
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    funding: FundingSeries | None = None
    fee_schedule: FeeScheduleEvidence | None = None
    instrument_specs: tuple[InstrumentSpecEvidence, ...] = ()
    mark_prices: MarkPriceSeries | None = None
    execution_proxy: ExecutionProxyConfig | None = None

    def as_kwargs(self) -> dict:
        return {key: getattr(self, key) for key in sorted(EVIDENCE_KEYS)}

    def as_config(self) -> dict:
        supplied = {key: value for key, value in self.as_kwargs().items() if value}
        return json.loads(json.dumps(supplied, default=_json_default, allow_nan=False))

    def manifest(self) -> dict:
        return execution_evidence_manifest(**self.as_kwargs())

    def realism_report(self) -> dict:
        """Describe model coverage without inventing an empirical accuracy score."""
        return {
            "version": "koval_realism_report_v1",
            "accuracy": "unmeasured",
            "maximum_error_pct": None,
            "evidence_source": "caller_supplied_unverified",
            "effects": {
                "funding": "supplied_evidence" if self.funding else "unavailable",
                "fee_tiers": self.fee_schedule.evidence_status
                if self.fee_schedule
                else "configured_rate",
                "instrument_rules": "supplied_evidence" if self.instrument_specs else "unavailable",
                "liquidation": "sampled_mark_model"
                if self.mark_prices and self.instrument_specs
                else "unavailable",
                "partial_fills": "ohlcv_proxy" if self.execution_proxy else "unavailable",
                "latency": "bar_quantized_assumption" if self.execution_proxy else "unavailable",
            },
            "unavailable_effects": [
                "intrabar_price_path",
                "queue_position",
                "order_book_depth",
                "own_market_impact",
                "exchange_cancel_races",
                "network_outages",
                "auto_deleveraging",
                "multi_asset_collateral",
            ],
        }


@dataclass(frozen=True)
class ExecutionEvidenceUpdate:
    """One idempotent live-evidence update applied before its matching bar."""

    __pydantic_config__ = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    update_id: str
    timestamp_ms: int
    funding: FundingSeries | None = None
    fee_schedule: FeeScheduleEvidence | None = None
    instrument_spec: InstrumentSpecEvidence | None = None
    mark_price: MarkPriceRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.update_id, str) or not self.update_id.strip():
            raise ValueError("execution evidence update_id is required")
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("execution evidence update timestamp must be non-negative")
        if not any((self.funding, self.fee_schedule, self.instrument_spec, self.mark_price)):
            raise ValueError("execution evidence update must contain evidence")
        if self.mark_price is not None and self.mark_price.timestamp_ms != self.timestamp_ms:
            raise ValueError("mark-price update must match the bar timestamp")
        if self.funding is not None and (
            not self.funding.coverage_complete
            or self.funding.requested_start_ms != self.timestamp_ms
            or self.funding.requested_end_ms != self.timestamp_ms
        ):
            raise ValueError("funding update must prove coverage for exactly one bar timestamp")
        if self.instrument_spec is not None and not (
            self.instrument_spec.effective_from_ms <= self.timestamp_ms
            and (
                self.instrument_spec.effective_to_ms is None
                or self.timestamp_ms <= self.instrument_spec.effective_to_ms
            )
        ):
            raise ValueError("instrument update does not cover its bar timestamp")
        if self.fee_schedule is not None and not (
            (
                self.fee_schedule.effective_from_ms is None
                or self.fee_schedule.effective_from_ms <= self.timestamp_ms
            )
            and (
                self.fee_schedule.effective_to_ms is None
                or self.timestamp_ms <= self.fee_schedule.effective_to_ms
            )
        ):
            raise ValueError("fee update does not cover its bar timestamp")

    def as_config(self) -> dict:
        return json.loads(json.dumps(self, default=_json_default, allow_nan=False))


_CODEC = TypeAdapter(ExecutionEvidence)
_UPDATE_CODEC = TypeAdapter(ExecutionEvidenceUpdate)


def decode_execution_evidence(value: dict) -> ExecutionEvidence:
    """Decode portable evidence with strict nested validation and exact decimals."""
    if not isinstance(value, dict):
        raise ValueError("execution evidence must be an object")
    try:
        payload = json.dumps(value, default=_json_default, allow_nan=False)
        return _CODEC.validate_json(payload)
    except (TypeError, OverflowError) as exc:
        raise ValueError("execution evidence must contain finite JSON-compatible values") from exc


def decode_execution_evidence_update(value: dict) -> ExecutionEvidenceUpdate:
    """Decode a strict idempotent live update without lossy coercion."""
    if not isinstance(value, dict):
        raise ValueError("execution evidence update must be an object")
    try:
        payload = json.dumps(value, default=_json_default, allow_nan=False)
        return _UPDATE_CODEC.validate_json(payload)
    except (TypeError, OverflowError) as exc:
        raise ValueError("execution evidence update must contain finite JSON values") from exc


__all__ = [
    "EVIDENCE_KEYS",
    "ExecutionEvidence",
    "ExecutionEvidenceUpdate",
    "decode_execution_evidence",
    "decode_execution_evidence_update",
]
