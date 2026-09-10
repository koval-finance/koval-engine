"""Deterministic, evidence-labelled OHLCV execution proxy primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def _non_negative(value: Decimal | int, *, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class ExecutionLatency:
    decision_to_submission_ms: int = 0
    submission_to_acknowledgement_ms: int = 0
    acknowledgement_to_fill_ms: int = 0
    cancellation_ms: int = 0
    replacement_ms: int = 0
    protection_activation_ms: int = 0

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or int(value) != value or value < 0:
                raise ValueError(f"execution latency {name} must be a non-negative integer")


@dataclass(frozen=True)
class ExecutionTimeline:
    decision_timestamp_ms: int
    submission_timestamp_ms: int
    acknowledgement_timestamp_ms: int
    fill_eligible_timestamp_ms: int
    protection_active_timestamp_ms: int


@dataclass(frozen=True)
class ImpactCalibrationEvidence:
    evidence_id: str
    calibrated_at_ms: int
    coefficient_bps: Decimal
    volatility: Decimal
    source: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.source:
            raise ValueError("impact calibration evidence identity and source are required")
        _non_negative(self.coefficient_bps, name="impact coefficient")
        _non_negative(self.volatility, name="impact volatility")
        if isinstance(self.calibrated_at_ms, bool) or self.calibrated_at_ms < 0:
            raise ValueError("impact calibration timestamp must be non-negative")


@dataclass(frozen=True)
class SlippageResolution:
    slippage_bps: Decimal
    model: str
    evidence_id: str | None
    evidence_status: str
    source: str | None


@dataclass(frozen=True)
class ExecutionProxyConfig:
    maximum_volume_participation: Decimal
    entry_remainder_policy: str
    latency: ExecutionLatency
    calibration: ImpactCalibrationEvidence | None = None

    def __post_init__(self) -> None:
        participation = Decimal(str(self.maximum_volume_participation))
        if not participation.is_finite() or not Decimal("0") < participation <= Decimal("1"):
            raise ValueError("maximum volume participation must be in (0, 1]")
        if self.entry_remainder_policy not in {"carry", "cancel"}:
            raise ValueError("entry remainder policy must be carry or cancel")

    @property
    def model_label(self) -> str:
        return "calibrated_ohlcv_proxy" if self.calibration is not None else "fixed_ohlcv_proxy"


class BarLiquidityBudget:
    """One shared base-volume budget; allocations mutate only this bar object."""

    def __init__(
        self,
        *,
        timestamp_ms: int,
        observed_volume: Decimal,
        maximum_participation: Decimal,
    ) -> None:
        volume = _non_negative(observed_volume, name="observed bar volume")
        participation = Decimal(str(maximum_participation))
        if not participation.is_finite() or not Decimal("0") < participation <= Decimal("1"):
            raise ValueError("maximum participation must be in (0, 1]")
        self.timestamp_ms = int(timestamp_ms)
        self.capacity = volume * participation
        self._remaining = self.capacity
        self._consumed_by_order: dict[str, Decimal] = {}

    @property
    def remaining(self) -> Decimal:
        return self._remaining

    @property
    def consumed_by_order(self) -> dict[str, Decimal]:
        return dict(self._consumed_by_order)

    def allocate(self, *, order_id: str, requested: Decimal) -> Decimal:
        if not order_id:
            raise ValueError("liquidity allocation requires a stable order_id")
        quantity = _non_negative(requested, name="requested fill quantity")
        allocated = min(quantity, self._remaining)
        self._remaining -= allocated
        self._consumed_by_order[order_id] = (
            self._consumed_by_order.get(order_id, Decimal("0")) + allocated
        )
        return allocated


def execution_timeline(decision_timestamp_ms: int, latency: ExecutionLatency) -> ExecutionTimeline:
    decision = int(decision_timestamp_ms)
    submission = decision + latency.decision_to_submission_ms
    acknowledgement = submission + latency.submission_to_acknowledgement_ms
    fill = acknowledgement + latency.acknowledgement_to_fill_ms
    return ExecutionTimeline(
        decision_timestamp_ms=decision,
        submission_timestamp_ms=submission,
        acknowledgement_timestamp_ms=acknowledgement,
        fill_eligible_timestamp_ms=fill,
        protection_active_timestamp_ms=fill + latency.protection_activation_ms,
    )


def resolve_slippage_bps(
    *,
    fixed_slippage_bps: Decimal,
    participation: Decimal,
    decision_timestamp_ms: int,
    calibration: ImpactCalibrationEvidence | None,
) -> SlippageResolution:
    fixed = _non_negative(fixed_slippage_bps, name="fixed slippage")
    fraction = Decimal(str(participation))
    if not fraction.is_finite() or not Decimal("0") <= fraction <= Decimal("1"):
        raise ValueError("fill participation must be in [0, 1]")
    if calibration is None:
        return SlippageResolution(
            slippage_bps=fixed,
            model="fixed_ohlcv_proxy",
            evidence_id=None,
            evidence_status="unavailable",
            source=None,
        )
    if calibration.calibrated_at_ms >= int(decision_timestamp_ms):
        raise ValueError("impact calibration must be strictly before the decision timestamp")
    impact = (
        Decimal(str(calibration.coefficient_bps))
        * Decimal(str(calibration.volatility))
        * fraction.sqrt()
    )
    return SlippageResolution(
        slippage_bps=impact,
        model="calibrated_ohlcv_proxy",
        evidence_id=calibration.evidence_id,
        evidence_status="historical",
        source=calibration.source,
    )


__all__ = [
    "BarLiquidityBudget",
    "ExecutionLatency",
    "ExecutionProxyConfig",
    "ExecutionTimeline",
    "ImpactCalibrationEvidence",
    "SlippageResolution",
    "execution_timeline",
    "resolve_slippage_bps",
]
