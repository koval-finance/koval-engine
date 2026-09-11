"""Shared deterministic scenarios and strict cross-runtime result comparison."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Relative slack allowed between two runtimes' floats. Two implementations of
#: the same arithmetic disagree in the last bits for roughly half of realistic
#: inputs purely from operation order; this is orders of magnitude below any
#: economically meaningful difference, so it separates representation noise
#: from an actual disagreement about execution.
DEFAULT_RELATIVE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ConformanceAction:
    """A deterministic state change applied after one indexed scenario bar."""

    after_bar_index: int
    kind: str
    value: float | None = None


@dataclass(frozen=True)
class ConformanceScenario:
    scenario_id: str
    side: str
    order_type: str
    entry_price: float
    stop_price: float
    target_price: float
    bars: tuple[tuple[float, float, float, float], ...]
    coverage_tags: tuple[str, ...]
    initial_capital: float = 10_000.0
    quantity: float = 1.0
    after_bar_actions: tuple[ConformanceAction, ...] = ()
    expected_outcome: str = "closed"
    market: str = "future"


@dataclass(frozen=True)
class IntentionalDifference:
    path: str
    reason_code: str


def generate_conformance_scenarios() -> tuple[ConformanceScenario, ...]:
    """Return a stable minimum matrix suitable for paper/plugin differential runs."""
    baseline = tuple(
        ConformanceScenario(
            scenario_id=f"{side}_{order_type}_{index}",
            side=side,
            order_type=order_type,
            entry_price=100.0,
            stop_price=90.0 if side == "buy" else 110.0,
            target_price=120.0 if side == "buy" else 80.0,
            bars=bars,
            coverage_tags=(f"{order_type}_entry", *tags),
        )
        for index, (side, order_type, bars, tags) in enumerate(
            (
                (
                    "buy",
                    "market",
                    ((100.0, 121.0, 89.0, 105.0),),
                    ("stop_loss", "take_profit", "ambiguous_protection", "oco"),
                ),
                (
                    "sell",
                    "market",
                    ((100.0, 111.0, 79.0, 95.0),),
                    ("stop_loss", "take_profit", "ambiguous_protection", "oco"),
                ),
                (
                    "buy",
                    "limit",
                    ((95.0, 99.0, 94.0, 97.0),),
                    ("favorable_gap",),
                ),
                (
                    "sell",
                    "limit",
                    ((105.0, 106.0, 101.0, 103.0),),
                    ("favorable_gap",),
                ),
                (
                    "buy",
                    "stop",
                    ((105.0, 106.0, 104.0, 105.0), (90.0, 91.0, 89.0, 90.0)),
                    ("adverse_gap", "stop_loss", "end_of_data_open_at_stop"),
                ),
                (
                    "sell",
                    "stop",
                    ((95.0, 96.0, 94.0, 95.0), (110.0, 111.0, 109.0, 110.0)),
                    ("adverse_gap", "stop_loss", "end_of_data_open_at_stop"),
                ),
            ),
            start=1,
        )
    )
    state_transitions = (
        ConformanceScenario(
            scenario_id="buy_limit_cancel_7",
            side="buy",
            order_type="limit",
            entry_price=100.0,
            stop_price=90.0,
            target_price=120.0,
            bars=((101.0, 102.0, 100.5, 101.0),),
            coverage_tags=("limit_entry", "cancellation"),
            after_bar_actions=(ConformanceAction(0, "cancel_entry"),),
            expected_outcome="canceled_without_fill",
        ),
        ConformanceScenario(
            scenario_id="buy_market_dynamic_stop_8",
            side="buy",
            order_type="market",
            entry_price=100.0,
            stop_price=90.0,
            target_price=120.0,
            bars=(
                (100.0, 105.0, 96.0, 102.0),
                (102.0, 103.0, 94.0, 95.0),
            ),
            coverage_tags=("market_entry", "dynamic_protection", "stop_loss"),
            after_bar_actions=(ConformanceAction(0, "modify_stop", 95.0),),
            expected_outcome="stop_loss",
        ),
        ConformanceScenario(
            scenario_id="buy_market_insufficient_margin_9",
            side="buy",
            order_type="market",
            entry_price=100.0,
            stop_price=90.0,
            target_price=120.0,
            bars=((100.0, 101.0, 99.0, 100.0),),
            coverage_tags=("market_entry", "insufficient_margin"),
            initial_capital=50.0,
            quantity=1.0,
            expected_outcome="rejected_insufficient_margin",
        ),
    )
    dynamic_targets = tuple(
        ConformanceScenario(
            scenario_id=f"{side}_market_dynamic_target",
            side=side,
            order_type="market",
            entry_price=100,
            stop_price=90 if side == "buy" else 110,
            target_price=120 if side == "buy" else 80,
            bars=((100, 106, 94, 100), (100, 106, 94, 100)),
            coverage_tags=("market_entry", "dynamic_protection", "dynamic_target", "take_profit"),
            after_bar_actions=(
                ConformanceAction(0, "modify_target", 105 if side == "buy" else 95),
            ),
            expected_outcome="take_profit",
        )
        for side in ("buy", "sell")
    )
    spot_refusal = ConformanceScenario(
        scenario_id="spot_short_rejected",
        side="sell",
        order_type="market",
        entry_price=100,
        stop_price=110,
        target_price=90,
        bars=((100, 101, 99, 100),),
        coverage_tags=("spot_short_unsupported", "market_entry"),
        market="spot",
        expected_outcome="rejected_spot_short_unsupported",
    )
    return (*baseline, *state_transitions, *dynamic_targets, spot_refusal)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _differences(
    expected: Any, observed: Any, *, path: str = "", relative_tolerance: float
) -> list[str]:
    if isinstance(expected, dict) and isinstance(observed, dict):
        differences: list[str] = []
        for key in sorted(set(expected) | set(observed)):
            child = f"{path}.{key}" if path else str(key)
            if key not in expected or key not in observed:
                differences.append(child)
            else:
                differences.extend(
                    _differences(
                        expected[key],
                        observed[key],
                        path=child,
                        relative_tolerance=relative_tolerance,
                    )
                )
        return differences
    if isinstance(expected, (list, tuple)) and isinstance(observed, (list, tuple)):
        differences = []
        if len(expected) != len(observed):
            differences.append(f"{path}.length")
        for index, (left, right) in enumerate(zip(expected, observed, strict=False)):
            differences.extend(
                _differences(
                    left,
                    right,
                    path=f"{path}[{index}]",
                    relative_tolerance=relative_tolerance,
                )
            )
        return differences
    if _numeric(expected) and _numeric(observed):
        equal = math.isclose(
            float(expected),
            float(observed),
            rel_tol=relative_tolerance,
            abs_tol=relative_tolerance,
        )
        return [] if equal else [path or "<root>"]
    return [] if expected == observed else [path or "<root>"]


def compare_execution_results(
    expected: Any,
    observed: Any,
    *,
    allowed_differences: tuple[IntentionalDifference, ...] = (),
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
) -> None:
    """Require equality except for used, explicitly reason-coded paths.

    Numbers are compared within ``relative_tolerance``, applied as both relative
    and absolute slack so values near zero are covered too, and everything else
    is compared exactly. That holds two runtimes to the same execution result
    rather than to the same float operation order. Pass ``0.0`` to demand
    bit-identical numbers.
    """
    if any(not waiver.path or not waiver.reason_code for waiver in allowed_differences):
        raise ValueError("every allowed difference needs a path and reason_code")
    if not math.isfinite(relative_tolerance) or relative_tolerance < 0:
        raise ValueError("relative_tolerance must be finite and non-negative")
    differences = _differences(expected, observed, relative_tolerance=relative_tolerance)
    allowed = {waiver.path: waiver.reason_code for waiver in allowed_differences}
    unexpected = [path for path in differences if path not in allowed]
    if unexpected:
        raise AssertionError(f"execution results differ at: {', '.join(unexpected)}")
    unused = sorted(set(allowed) - set(differences))
    if unused:
        raise AssertionError(f"unused intentional-difference waiver: {', '.join(unused)}")


__all__ = [
    "DEFAULT_RELATIVE_TOLERANCE",
    "ConformanceAction",
    "ConformanceScenario",
    "IntentionalDifference",
    "compare_execution_results",
    "generate_conformance_scenarios",
]
