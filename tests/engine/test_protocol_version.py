import numpy as np
import pytest

from koval.engine.backtest_engine import (
    ENGINE_PROTOCOL_VERSION,
    EngineRunSpec,
    ExecutionCapabilities,
    ProtocolVersionError,
    check_protocol_version,
    negotiate_execution_capabilities,
)


def _spec(**kw):
    return EngineRunSpec(graph={}, feeds={"1h": np.zeros((1, 6))}, initial_capital=1000.0, **kw)


def test_spec_defaults_to_current_protocol_version():
    assert _spec().protocol_version == ENGINE_PROTOCOL_VERSION


def test_check_passes_for_current_version():
    check_protocol_version(_spec())  # no raise


def test_check_rejects_unknown_future_version():
    with pytest.raises(ProtocolVersionError):
        check_protocol_version(_spec(protocol_version=ENGINE_PROTOCOL_VERSION + 1))


def test_execution_evidence_capabilities_are_versioned_and_negotiated():
    spec = _spec(
        execution_contract_version=2,
        required_execution_capabilities=("fee_evidence_v1", "funding_cashflows_v1"),
    )
    offered = ExecutionCapabilities(
        execution_contract_versions=(1, 2),
        features=("fee_evidence_v1", "funding_cashflows_v1", "partial_fills_v1"),
    )
    negotiated = negotiate_execution_capabilities(spec, offered)
    assert negotiated.execution_contract_version == 2
    assert negotiated.features == ("fee_evidence_v1", "funding_cashflows_v1")


def test_missing_execution_capability_fails_closed():
    spec = _spec(
        execution_contract_version=2,
        required_execution_capabilities=("fee_evidence_v1",),
    )
    with pytest.raises(ProtocolVersionError, match="fee_evidence_v1"):
        negotiate_execution_capabilities(
            spec,
            ExecutionCapabilities(execution_contract_versions=(1, 2), features=()),
        )
