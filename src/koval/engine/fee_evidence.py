"""Fee-schedule evidence and deterministic maker/taker rate resolution."""

from __future__ import annotations

import math
from dataclasses import dataclass

_EVIDENCE_STATUSES = frozenset({"historical", "current_snapshot", "approximation"})
_LIQUIDITY_ROLES = frozenset({"maker", "taker"})


@dataclass(frozen=True)
class FeeScheduleEvidence:
    """A versioned fee schedule together with enough provenance to audit it."""

    evidence_id: str
    maker_bps: float
    taker_bps: float
    currency: str
    evidence_status: str
    source: str
    effective_from_ms: int | None = None
    effective_to_ms: int | None = None
    discount_treatment: str = "none"
    tier_id: str = "unspecified"

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_id",
            "currency",
            "source",
            "discount_treatment",
            "tier_id",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"fee {field_name} is required")
        if self.evidence_status not in _EVIDENCE_STATUSES:
            raise ValueError(
                "fee evidence_status must be historical, current_snapshot, or approximation"
            )
        for field_name in ("maker_bps", "taker_bps"):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or not -10_000 < value < 10_000:
                raise ValueError(f"fee {field_name} must be finite and within one notional")
        for field_name in ("effective_from_ms", "effective_to_ms"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"fee {field_name} must be a non-negative integer")
        if self.evidence_status == "historical" and (
            self.effective_from_ms is None or self.effective_to_ms is None
        ):
            raise ValueError("historical fee evidence requires a bounded effective interval")
        if (
            self.effective_from_ms is not None
            and self.effective_to_ms is not None
            and int(self.effective_from_ms) > int(self.effective_to_ms)
        ):
            raise ValueError("fee effective_from_ms must not exceed effective_to_ms")


@dataclass(frozen=True)
class FeeApplication:
    liquidity_role: str
    rate_bps: float
    currency: str
    evidence_id: str
    evidence_status: str
    source: str
    discount_treatment: str
    tier_id: str


def resolve_fee_application(
    schedule: FeeScheduleEvidence,
    *,
    role: str,
    timestamp_ms: int,
) -> FeeApplication:
    """Resolve an auditable rate without presenting a time mismatch as historical."""
    if role not in _LIQUIDITY_ROLES:
        raise ValueError("fee liquidity role must be maker or taker")
    timestamp = int(timestamp_ms)
    before = schedule.effective_from_ms is not None and timestamp < int(schedule.effective_from_ms)
    after = schedule.effective_to_ms is not None and timestamp > int(schedule.effective_to_ms)
    outside_window = before or after
    if outside_window and schedule.evidence_status == "historical":
        raise ValueError("historical fee evidence does not cover the fill timestamp")
    evidence_status = "approximation" if outside_window else schedule.evidence_status
    rate_bps = schedule.maker_bps if role == "maker" else schedule.taker_bps
    return FeeApplication(
        liquidity_role=role,
        rate_bps=float(rate_bps),
        currency=schedule.currency,
        evidence_id=schedule.evidence_id,
        evidence_status=evidence_status,
        source=schedule.source,
        discount_treatment=schedule.discount_treatment,
        tier_id=schedule.tier_id,
    )


__all__ = ["FeeApplication", "FeeScheduleEvidence", "resolve_fee_application"]
