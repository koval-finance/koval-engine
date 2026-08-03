"""Tests for the backtest-engine plugin contract."""

from types import SimpleNamespace

import numpy as np

import koval.engine.backtest_engine as bte
from koval.engine.backtest_engine import (
    BacktestResult,
    EngineRunSpec,
    load_backtest_engine,
)


def test_engine_run_spec_holds_data():
    spec = EngineRunSpec(
        graph={"blocks": []}, feeds={"1h": np.zeros((1, 6))}, initial_capital=10000.0
    )
    assert spec.initial_capital == 10000.0
    assert "1h" in spec.feeds


def test_backtest_result_defaults_are_empty():
    result = BacktestResult()
    assert result.metrics == {}
    assert result.trades == []
    assert result.equity_curve == []


def test_load_backtest_engine_resolves_an_override(monkeypatch):
    expected = object()
    module = SimpleNamespace(create_engine=lambda: expected)
    monkeypatch.setenv("KOVAL_BACKTEST_ENGINE", "custom.engine")
    monkeypatch.setattr(bte.importlib, "import_module", lambda name: module)

    engine = load_backtest_engine()
    assert engine is expected


def test_load_backtest_engine_resolves_the_registered_default(monkeypatch):
    expected = object()
    entry_point = SimpleNamespace(load=lambda: lambda: expected)
    monkeypatch.delenv("KOVAL_BACKTEST_ENGINE", raising=False)
    monkeypatch.setattr(bte, "_discover_entry_points", lambda: {"backtrader": entry_point})

    engine = load_backtest_engine()
    assert engine is expected
