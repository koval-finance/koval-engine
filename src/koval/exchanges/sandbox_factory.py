"""Allowlisted sandbox broker factory."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import requests

from koval.engine.broker import Broker
from koval.engine.execution_proxy import ExecutionProxyConfig
from koval.engine.fee_evidence import FeeScheduleEvidence
from koval.engine.funding import FundingSeries
from koval.engine.instrument_risk import InstrumentSpecEvidence, MarkPriceSeries
from koval.engine.paper_broker import PaperBroker
from koval.engine.paper_profile import resolve_paper_profile
from koval.exchanges.binance_sandbox import BinanceSandboxBroker, ClockSkewExceeded
from koval.exchanges.execution_compatibility import compatibility_for

_ALLOWED_MODES = {"paper", "binance_sandbox"}


@dataclass(frozen=True)
class SandboxBrokerConfig:
    initial_capital: float = 10_000.0
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    exchange: str = "binance"
    exchange_type: str = "future"
    # The paper execution profile, resolved here so the one place execution
    # modes are decided is also the one place their cost model is decided.
    paper_profile: Mapping[str, object] | None = None
    funding: FundingSeries | None = None
    fee_schedule: FeeScheduleEvidence | None = None
    instrument_specs: tuple[InstrumentSpecEvidence, ...] = ()
    mark_prices: MarkPriceSeries | None = None
    execution_proxy: ExecutionProxyConfig | None = None


def build_broker(mode: str, config: SandboxBrokerConfig) -> Broker:
    if mode not in _ALLOWED_MODES:
        raise ValueError(f"unsupported sandbox broker mode: {mode}")
    compatibility = compatibility_for(config.exchange, config.exchange_type, mode)
    profile = resolve_paper_profile(config.paper_profile)
    if compatibility.market == "spot" and profile.leverage != 1.0:
        raise ValueError("spot leverage must be exactly one")
    if mode == "paper":
        return PaperBroker(
            config.initial_capital,
            profile=profile,
            market=compatibility.market,
            exchange=compatibility.exchange,
            funding=config.funding,
            fee_schedule=config.fee_schedule,
            instrument_specs=config.instrument_specs,
            mark_prices=config.mark_prices,
            execution_proxy=config.execution_proxy,
        )
    if mode == "binance_sandbox":
        broker = BinanceSandboxBroker(
            api_key=_required(config.env, "BINANCE_SANDBOX_API_KEY"),
            api_secret=_required(config.env, "BINANCE_SANDBOX_API_SECRET"),
        )
        _synchronize_venue_clock(broker)
        return broker
    raise AssertionError(f"unreachable broker mode: {mode}")


def _synchronize_venue_clock(broker: BinanceSandboxBroker) -> None:
    """Measure venue time once, at the boundary where a session's broker is built.

    Binance rejects a signed request whose timestamp falls outside its
    recvWindow, so a drifting host clock would fail every order. A skew too
    large to sign safely fails the session closed here; a time endpoint that is
    merely unreachable does not, because the venue validates the timestamp
    anyway and a second failure mode would only cost the session more.
    """
    try:
        broker.synchronize_clock()
    except ClockSkewExceeded:
        raise
    except (requests.RequestException, RuntimeError, ValueError, KeyError):
        return


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ValueError(f"missing required sandbox credential: {key}")
    return value
