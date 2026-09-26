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
from koval.engine.instrument_risk import InstrumentSpecEvidence, MarkPriceSeries
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


_CODEC = TypeAdapter(ExecutionEvidence)


def decode_execution_evidence(value: dict) -> ExecutionEvidence:
    """Decode portable evidence with strict nested validation and exact decimals."""
    if not isinstance(value, dict):
        raise ValueError("execution evidence must be an object")
    try:
        payload = json.dumps(value, default=_json_default, allow_nan=False)
        return _CODEC.validate_json(payload)
    except (TypeError, OverflowError) as exc:
        raise ValueError("execution evidence must contain finite JSON-compatible values") from exc
