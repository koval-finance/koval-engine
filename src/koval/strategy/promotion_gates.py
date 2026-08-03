"""Validation rules for publishing locked system strategies.

User-owned strategies may be drafted and tested freely. This module guards the
stricter boundary where a strategy becomes a shared, immutable system strategy.
"""

from __future__ import annotations

from typing import Any

MIN_BACKTEST_SUCCESSFUL_RUNS = 100
MIN_BACKTEST_DISTINCT_RANGES = 100
MIN_LIVE_SUCCESSFUL_RUNS = 10
ALLOWED_LIVE_MODES = frozenset({"paper", "binance_sandbox"})


class StrategyPromotionGateError(ValueError):
    """Raised when a system strategy seed lacks required publication evidence."""


def validate_system_publication(publication: dict[str, Any] | None, *, strategy_name: str) -> None:
    """Validate publication metadata for a system-locked strategy seed."""
    if not isinstance(publication, dict):
        raise StrategyPromotionGateError(
            f"{strategy_name}: publication metadata is required for system strategies"
        )

    status = publication.get("status")
    if status != "system_locked":
        raise StrategyPromotionGateError(
            f"{strategy_name}: publication.status must be system_locked"
        )

    workflow = publication.get("workflow")
    if workflow == "legacy_manual_seed":
        _validate_legacy_exemption(publication, strategy_name=strategy_name)
        return
    if workflow == "strategy_agent_v1":
        _validate_strategy_agent_gates(publication, strategy_name=strategy_name)
        return

    raise StrategyPromotionGateError(
        f"{strategy_name}: unsupported publication.workflow {workflow!r}"
    )


def _validate_legacy_exemption(publication: dict[str, Any], *, strategy_name: str) -> None:
    exemption = publication.get("gate_exemption")
    if not isinstance(exemption, str) or not exemption.strip():
        raise StrategyPromotionGateError(
            f"{strategy_name}: legacy_manual_seed publication requires gate_exemption"
        )


def _validate_strategy_agent_gates(publication: dict[str, Any], *, strategy_name: str) -> None:
    backtest_gate = _require_gate(publication, "backtest_gate", strategy_name=strategy_name)
    live_gate = _require_gate(publication, "live_gate", strategy_name=strategy_name)

    backtest_runs = _int_field(backtest_gate, "successful_runs")
    if backtest_runs < MIN_BACKTEST_SUCCESSFUL_RUNS:
        raise StrategyPromotionGateError(
            f"{strategy_name}: strategy_agent_v1 publication requires at least "
            f"{MIN_BACKTEST_SUCCESSFUL_RUNS} successful backtests"
        )

    distinct_ranges = _int_field(backtest_gate, "distinct_data_ranges")
    if distinct_ranges < MIN_BACKTEST_DISTINCT_RANGES:
        raise StrategyPromotionGateError(
            f"{strategy_name}: strategy_agent_v1 publication requires at least "
            f"{MIN_BACKTEST_DISTINCT_RANGES} distinct data ranges"
        )

    live_runs = _int_field(live_gate, "successful_runs")
    if live_runs < MIN_LIVE_SUCCESSFUL_RUNS:
        raise StrategyPromotionGateError(
            f"{strategy_name}: strategy_agent_v1 publication requires at least "
            f"{MIN_LIVE_SUCCESSFUL_RUNS} successful live runs"
        )

    modes = live_gate.get("modes")
    if not isinstance(modes, list) or not modes:
        raise StrategyPromotionGateError(
            f"{strategy_name}: live_gate.modes must list paper/sandbox modes used"
        )
    unsupported_modes = sorted({str(mode) for mode in modes} - ALLOWED_LIVE_MODES)
    if unsupported_modes:
        raise StrategyPromotionGateError(
            f"{strategy_name}: unsupported live modes in publication evidence: {unsupported_modes}"
        )

    _require_evidence_ref(backtest_gate, "backtest_gate", strategy_name=strategy_name)
    _require_evidence_ref(live_gate, "live_gate", strategy_name=strategy_name)


def _require_gate(publication: dict[str, Any], key: str, *, strategy_name: str) -> dict[str, Any]:
    gate = publication.get(key)
    if not isinstance(gate, dict):
        raise StrategyPromotionGateError(f"{strategy_name}: publication.{key} is required")
    return gate


def _int_field(gate: dict[str, Any], key: str) -> int:
    value = gate.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrategyPromotionGateError(f"publication gate field {key!r} must be an integer")
    return value


def _require_evidence_ref(gate: dict[str, Any], gate_name: str, *, strategy_name: str) -> None:
    evidence_ref = gate.get("evidence_ref")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise StrategyPromotionGateError(f"{strategy_name}: {gate_name}.evidence_ref is required")
