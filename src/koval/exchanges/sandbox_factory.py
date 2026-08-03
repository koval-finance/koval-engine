"""Allowlisted sandbox broker factory."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from koval.engine.broker import Broker
from koval.engine.paper_broker import PaperBroker
from koval.exchanges.binance_sandbox import BinanceSandboxBroker

_ALLOWED_MODES = {"paper", "binance_sandbox"}


@dataclass(frozen=True)
class SandboxBrokerConfig:
    initial_capital: float = 10_000.0
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)


def build_broker(mode: str, config: SandboxBrokerConfig) -> Broker:
    if mode not in _ALLOWED_MODES:
        raise ValueError(f"unsupported sandbox broker mode: {mode}")
    if mode == "paper":
        return PaperBroker(config.initial_capital)
    if mode == "binance_sandbox":
        return BinanceSandboxBroker(
            api_key=_required(config.env, "BINANCE_SANDBOX_API_KEY"),
            api_secret=_required(config.env, "BINANCE_SANDBOX_API_SECRET"),
        )
    raise AssertionError(f"unreachable broker mode: {mode}")


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ValueError(f"missing required sandbox credential: {key}")
    return value
