"""Backtest-engine plugin contract for independently distributed engines.

Concrete engines are discovered through Python entry points or loaded from an
explicit module path. This MIT package does not import an implementation.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Protocol

import numpy as np

from koval.engine.run_boundaries import (
    RUNTIME_BOUNDARIES_CAPABILITY,
    resolve_runtime_boundaries,
)

_ENTRY_POINT_GROUP = "koval.backtest_engines"
_DEFAULT_ENGINE_NAME = "backtrader"

# Bump when EngineRunSpec / BacktestResult change in a breaking way so external
# engines can negotiate compatibility.
ENGINE_PROTOCOL_VERSION = 2


@dataclass
class EngineRunSpec:
    """Everything the engine needs for one run — pure, picklable data.

    ``execution_config`` is an optional dict describing the venue (``exchange``,
    ``exchange_type``) so the engine can mark realistic fees on the broker. When
    omitted the run is fee-free — kept that way for unit tests that assert on
    raw price action.
    """

    graph: dict[str, Any]
    feeds: dict[str, np.ndarray]
    initial_capital: float
    execution_config: dict[str, Any] | None = None
    protocol_version: int = ENGINE_PROTOCOL_VERSION
    execution_contract_version: int = 1
    required_execution_capabilities: tuple[str, ...] = ()
    runtime_contract: dict[str, Any] | None = None


@dataclass
class BacktestResult:
    """Engine output — plain Python only, no Backtrader objects."""

    metrics: dict[str, Any] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionCapabilities:
    """Versioned evidence/execution features advertised by a runtime plugin."""

    execution_contract_versions: tuple[int, ...]
    features: tuple[str, ...]


@dataclass(frozen=True)
class NegotiatedExecutionCapabilities:
    execution_contract_version: int
    features: tuple[str, ...]


class BacktestEngineProtocol(Protocol):
    """The contract every backtest engine plugin implements."""

    def run(
        self,
        spec: EngineRunSpec,
        on_event: Callable[[dict], None] | None = None,
    ) -> BacktestResult: ...


class ProtocolVersionError(RuntimeError):
    """Raised when a spec's protocol version is not supported by the engine."""


def check_protocol_version(spec: EngineRunSpec) -> None:
    if spec.runtime_contract is not None and spec.protocol_version < 2:
        raise ProtocolVersionError("runtime boundaries require protocol version 2")
    if spec.protocol_version not in {1, ENGINE_PROTOCOL_VERSION}:
        raise ProtocolVersionError(
            f"spec protocol_version={spec.protocol_version} unsupported; "
            f"engine speaks {ENGINE_PROTOCOL_VERSION}"
        )


def negotiate_execution_capabilities(
    spec: EngineRunSpec,
    offered: ExecutionCapabilities,
) -> NegotiatedExecutionCapabilities:
    """Fail closed when a plugin cannot honor requested execution evidence."""
    check_protocol_version(spec)
    boundaries = resolve_runtime_boundaries(spec.runtime_contract)
    if boundaries is not None and boundaries.initial_balance != spec.initial_capital:
        raise ValueError("runtime initial_balance must match initial_capital")
    requested_version = int(spec.execution_contract_version)
    if requested_version not in offered.execution_contract_versions:
        raise ProtocolVersionError(
            f"execution contract v{requested_version} is not offered by the engine"
        )
    required = tuple(dict.fromkeys(spec.required_execution_capabilities))
    if boundaries is not None:
        required = tuple(dict.fromkeys((*required, RUNTIME_BOUNDARIES_CAPABILITY)))
    missing = sorted(set(required) - set(offered.features))
    if missing:
        raise ProtocolVersionError(
            "engine is missing required execution capabilities: " + ", ".join(missing)
        )
    return NegotiatedExecutionCapabilities(
        execution_contract_version=requested_version,
        features=required,
    )


class NoBacktestEngineError(RuntimeError):
    """Raised when no backtest engine can be resolved."""


def _discover_entry_points() -> dict[str, metadata.EntryPoint]:
    return {ep.name: ep for ep in metadata.entry_points(group=_ENTRY_POINT_GROUP)}


def _resolve_engine_target(name: str | None) -> Callable[[], BacktestEngineProtocol]:
    """Resolve the engine factory: an explicit module override wins, then the
    requested entry point."""
    override = os.getenv("KOVAL_BACKTEST_ENGINE")
    if override:
        return importlib.import_module(override).create_engine
    requested = name or _DEFAULT_ENGINE_NAME
    entry_points = _discover_entry_points()
    chosen = entry_points.get(requested)
    if chosen is not None:
        return chosen.load()
    available = ", ".join(sorted(entry_points)) or "none"
    if requested != _DEFAULT_ENGINE_NAME:
        raise NoBacktestEngineError(
            f"No backtest engine named '{requested}' is registered "
            f"(available: {available}). Install/register a compatible plugin "
            "or set KOVAL_BACKTEST_ENGINE to an engine module path."
        )
    raise NoBacktestEngineError(
        f"No backtest engine named '{requested}' is registered "
        f"(available: {available}) — install or register a compatible backtest "
        "engine plugin through the 'koval.backtest_engines' entry-point group, "
        "or set KOVAL_BACKTEST_ENGINE to an engine module path."
    )


def load_backtest_engine(name: str | None = None) -> BacktestEngineProtocol:
    """Resolve and instantiate a backtest engine via the
    ``koval.backtest_engines`` entry-point group. ``KOVAL_BACKTEST_ENGINE``
    (a module path) overrides discovery; ``name`` selects a registered engine
    (default ``backtrader``). The env is read on every call so callers can
    switch engines without clearing a cache."""
    return _resolve_engine_target(name)()
