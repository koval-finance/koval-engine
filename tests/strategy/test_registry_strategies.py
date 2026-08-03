from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import BaseModel

from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.registry import (
    STRATEGY_REGISTRY,
    StrategyDefinition,
    get_all_definitions,
    get_strategy,
    register_strategy,
)


class _Tmp(DeclarativeStrategy):
    @classmethod
    def metadata(cls) -> dict:
        return {"name": "_Tmp", "description": "tmp", "tags": [], "version": "0.1.0"}


class _TmpParams(BaseModel):
    foo: int = 1


def test_register_strategy_then_lookup():
    sd = StrategyDefinition(
        name="_tmp",
        display_name="Tmp",
        description="x",
        strategy_class=_Tmp,
        params_schema=_TmpParams,
        default_params={"foo": 2},
        tags=["test"],
    )
    register_strategy(sd)
    try:
        assert get_strategy("_tmp") is sd
        assert any(d.name == "_tmp" for d in get_all_definitions())
    finally:
        del STRATEGY_REGISTRY["_tmp"]


def test_register_strategy_rejects_duplicate_name():
    sd = StrategyDefinition(
        name="dup",
        display_name="dup",
        description="x",
        strategy_class=_Tmp,
        params_schema=_TmpParams,
        default_params={},
        tags=[],
    )
    register_strategy(sd)
    try:
        with pytest.raises(ValueError):
            register_strategy(sd)
    finally:
        del STRATEGY_REGISTRY["dup"]


def test_get_strategy_unknown_raises():
    with pytest.raises(KeyError):
        get_strategy("does_not_exist")


def test_strategy_registry_contains_only_explicit_registrations():
    """The registry is module-global; tests must not leak state.

    Document the contract: at import time, STRATEGY_REGISTRY only contains
    explicitly registered extensions. Bundled graph preset builders do not
    mutate it.
    """
    assert isinstance(STRATEGY_REGISTRY, dict)
    for v in STRATEGY_REGISTRY.values():
        assert isinstance(v, StrategyDefinition)


def test_importing_graph_preset_builders_does_not_register_strategy_extensions():
    code = (
        "import koval.strategy.presets.risk_pipeline_demo; "
        "import koval.strategy.presets.sweep_choch_reversal; "
        "from koval.strategy.registry import STRATEGY_REGISTRY; "
        "print(len(STRATEGY_REGISTRY))"
    )

    assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "0"


def test_get_all_definitions_returns_fresh_list():
    """get_all_definitions returns a fresh list — caller mutation must not leak."""
    sd = StrategyDefinition(
        name="_freshcheck",
        display_name="x",
        description="x",
        strategy_class=_Tmp,
        params_schema=_TmpParams,
        default_params={},
        tags=[],
    )
    register_strategy(sd)
    try:
        defs = get_all_definitions()
        defs.clear()  # mutate the returned list
        # Should still be present in the registry
        assert get_strategy("_freshcheck") is sd
    finally:
        del STRATEGY_REGISTRY["_freshcheck"]


def test_strategy_definition_is_frozen():
    """StrategyDefinition mirrors BlockSpec — frozen dataclass for immutability."""
    sd = StrategyDefinition(
        name="frozen_check",
        display_name="x",
        description="x",
        strategy_class=_Tmp,
        params_schema=_TmpParams,
        default_params={},
        tags=[],
    )
    with pytest.raises((AttributeError, TypeError)):
        sd.name = "mutated"  # type: ignore[misc]
