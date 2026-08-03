import numpy as np
import pytest

from koval.engine.backtest_engine import (
    ENGINE_PROTOCOL_VERSION,
    EngineRunSpec,
    ProtocolVersionError,
    check_protocol_version,
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
