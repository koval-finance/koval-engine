"""MIT contract for comparable warmup, evaluation and initial risk baselines."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from koval.exchanges.base import timeframe_ms

RUNTIME_BOUNDARIES_CAPABILITY = "runtime_boundaries_v1"


@dataclass(frozen=True)
class RuntimeBoundaries:
    version: str
    warmup_start_ms: int
    evaluation_start_ms: int
    evaluation_end_ms: int
    decision_clock: str
    initial_balance: float
    daily_baseline_equity: float
    peak_equity: float
    end_of_data_policy: str

    def __post_init__(self) -> None:
        if self.version != "koval_runtime_boundaries_v1":
            raise ValueError("unsupported runtime boundaries version")
        for name in ("warmup_start_ms", "evaluation_start_ms", "evaluation_end_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"runtime {name} must be a non-negative integer")
        if not self.warmup_start_ms <= self.evaluation_start_ms < self.evaluation_end_ms:
            raise ValueError("runtime boundaries must order warmup before evaluation")
        if self.decision_clock != "bar_close":
            raise ValueError("runtime decision_clock must be bar_close")
        for name in ("initial_balance", "daily_baseline_equity", "peak_equity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"runtime {name} must be a positive finite number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"runtime {name} must be a positive finite number")
        if self.peak_equity < self.initial_balance:
            raise ValueError("runtime peak_equity cannot be below initial_balance")
        if self.end_of_data_policy not in {"mark_at_last_close", "flatten_at_last_close"}:
            raise ValueError("unsupported runtime end_of_data_policy")

    def validate_inputs(self, *, timeframe: str, initial_capital: float) -> None:
        step = timeframe_ms(timeframe)
        if any(
            value % step
            for value in (self.warmup_start_ms, self.evaluation_start_ms, self.evaluation_end_ms)
        ):
            raise ValueError("runtime boundaries must align to the primary timeframe")
        if self.initial_balance != initial_capital:
            raise ValueError("runtime initial_balance must match initial_capital")

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_runtime_boundaries(value: dict | None) -> RuntimeBoundaries | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("runtime_contract must be a dictionary")
    try:
        return RuntimeBoundaries(**value)
    except TypeError as exc:
        raise ValueError("runtime_contract has missing or unknown fields") from exc
