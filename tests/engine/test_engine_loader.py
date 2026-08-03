"""Backtest-engine loader: entry-point discovery, env override, actionable error."""

from types import SimpleNamespace

import pytest

import koval.engine.backtest_engine as bte
from koval.engine.backtest_engine import NoBacktestEngineError, load_backtest_engine


def test_loads_default_registered_engine(monkeypatch):
    expected = object()
    entry_point = SimpleNamespace(load=lambda: lambda: expected)
    monkeypatch.delenv("KOVAL_BACKTEST_ENGINE", raising=False)
    monkeypatch.setattr(bte, "_discover_entry_points", lambda: {"backtrader": entry_point})

    engine = load_backtest_engine()
    assert engine is expected


def test_env_override_still_works(monkeypatch):
    expected = object()
    module = SimpleNamespace(create_engine=lambda: expected)
    monkeypatch.setenv("KOVAL_BACKTEST_ENGINE", "custom.engine")
    monkeypatch.setattr(bte.importlib, "import_module", lambda name: module)

    engine = load_backtest_engine()
    assert engine is expected


def test_actionable_error_when_no_engine(monkeypatch):
    monkeypatch.delenv("KOVAL_BACKTEST_ENGINE", raising=False)
    monkeypatch.setattr(bte, "_discover_entry_points", lambda: {})

    def fail_on_module_import(name):
        raise AssertionError(f"loader must not import an in-tree adapter fallback: {name}")

    monkeypatch.setattr(bte.importlib, "import_module", fail_on_module_import)
    with pytest.raises(NoBacktestEngineError) as exc_info:
        load_backtest_engine()

    message = str(exc_info.value)
    assert "install or register a compatible backtest engine plugin" in message
    assert "koval-backtrader" not in message


def test_missing_named_engine_reports_requested_and_available_names(monkeypatch):
    monkeypatch.delenv("KOVAL_BACKTEST_ENGINE", raising=False)
    monkeypatch.setattr(
        bte,
        "_discover_entry_points",
        lambda: {"backtrader": SimpleNamespace()},
    )

    with pytest.raises(NoBacktestEngineError) as exc_info:
        load_backtest_engine("custom")

    message = str(exc_info.value)
    assert "'custom'" in message
    assert "backtrader" in message
