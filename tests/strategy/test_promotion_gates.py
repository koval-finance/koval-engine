"""System strategy publication gate validation."""

from __future__ import annotations

import pytest

from koval.strategy.promotion_gates import (
    StrategyPromotionGateError,
    validate_system_publication,
)


def _valid_agent_publication() -> dict:
    return {
        "workflow": "strategy_agent_v1",
        "status": "system_locked",
        "backtest_gate": {
            "successful_runs": 100,
            "distinct_data_ranges": 100,
            "evidence_ref": "docs/strategy-validation/example-backtests.md",
        },
        "live_gate": {
            "successful_runs": 10,
            "modes": ["paper", "binance_sandbox"],
            "evidence_ref": "docs/strategy-validation/example-live.md",
        },
    }


def test_strategy_agent_publication_passes_after_required_gates():
    validate_system_publication(_valid_agent_publication(), strategy_name="agent_strat")


def test_missing_publication_metadata_is_rejected():
    with pytest.raises(StrategyPromotionGateError, match="publication metadata"):
        validate_system_publication(None, strategy_name="agent_strat")


def test_strategy_agent_publication_requires_system_locked_status():
    publication = {**_valid_agent_publication(), "status": "draft"}

    with pytest.raises(StrategyPromotionGateError, match="system_locked"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_strategy_agent_publication_requires_100_successful_backtests():
    publication = _valid_agent_publication()
    publication["backtest_gate"] = {
        **publication["backtest_gate"],
        "successful_runs": 99,
    }

    with pytest.raises(StrategyPromotionGateError, match="100 successful backtests"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_strategy_agent_publication_requires_100_distinct_backtest_ranges():
    publication = _valid_agent_publication()
    publication["backtest_gate"] = {
        **publication["backtest_gate"],
        "distinct_data_ranges": 99,
    }

    with pytest.raises(StrategyPromotionGateError, match="100 distinct data ranges"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_strategy_agent_publication_requires_10_live_runs():
    publication = _valid_agent_publication()
    publication["live_gate"] = {
        **publication["live_gate"],
        "successful_runs": 9,
    }

    with pytest.raises(StrategyPromotionGateError, match="10 successful live runs"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_strategy_agent_publication_rejects_real_money_live_modes():
    publication = _valid_agent_publication()
    publication["live_gate"] = {
        **publication["live_gate"],
        "modes": ["paper", "binance_live"],
    }

    with pytest.raises(StrategyPromotionGateError, match="unsupported live modes"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_strategy_agent_publication_rejects_disabled_whitebit_execution():
    publication = _valid_agent_publication()
    publication["live_gate"] = {
        **publication["live_gate"],
        "modes": ["whitebit_sandbox"],
    }

    with pytest.raises(StrategyPromotionGateError, match="unsupported live modes"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_strategy_agent_publication_requires_gate_evidence_refs():
    publication = _valid_agent_publication()
    publication["backtest_gate"] = {
        **publication["backtest_gate"],
        "evidence_ref": "",
    }

    with pytest.raises(StrategyPromotionGateError, match="backtest_gate.evidence_ref"):
        validate_system_publication(publication, strategy_name="agent_strat")


def test_legacy_seed_requires_explicit_exemption_reason():
    publication = {
        "workflow": "legacy_manual_seed",
        "status": "system_locked",
        "gate_exemption": "Created before strategy-agent publication gates.",
    }

    validate_system_publication(publication, strategy_name="legacy_strat")


def test_legacy_seed_without_exemption_is_rejected():
    publication = {
        "workflow": "legacy_manual_seed",
        "status": "system_locked",
    }

    with pytest.raises(StrategyPromotionGateError, match="gate_exemption"):
        validate_system_publication(publication, strategy_name="legacy_strat")
