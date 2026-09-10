from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_EXECUTION_MODE = "paper"
SUPPORTED_EXECUTION_MODES = {"paper", "binance_sandbox"}
PAPER_COMMISSION_SIDE = "taker"


@dataclass(frozen=True)
class FeeSchedule:
    maker_fee_bps: float
    taker_fee_bps: float


@dataclass(frozen=True)
class ExecutionSettings:
    exchange: str
    exchange_type: str
    execution_mode: str
    fee_source: str
    maker_fee_bps: float
    taker_fee_bps: float
    broker_commission_bps: float
    commission_rate: float
    paper_commission_side: str = PAPER_COMMISSION_SIDE

    def as_config_patch(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "exchange_type": self.exchange_type,
            "execution_mode": self.execution_mode,
            "maker_fee_bps": self.maker_fee_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "broker_commission_bps": self.broker_commission_bps,
            "commission": self.commission_rate,
            "fee_source": self.fee_source,
            "paper_commission_side": self.paper_commission_side,
        }


_DEFAULT_FEE_SCHEDULES: dict[tuple[str, str], FeeSchedule] = {
    # Spot public fee page shows 0.100% / 0.100% for Regular User.
    ("binance", "spot"): FeeSchedule(maker_fee_bps=10.0, taker_fee_bps=10.0),
    # Futures signed commission endpoint example returns 0.02% maker / 0.04% taker.
    ("binance", "future"): FeeSchedule(maker_fee_bps=2.0, taker_fee_bps=4.0),
    # WhiteBIT spot public fee schedule: 0.10% maker / 0.10% taker for a base
    # (no VIP, no WBT discount) account. See whitebit.com/fee-schedule.
    ("whitebit", "spot"): FeeSchedule(maker_fee_bps=10.0, taker_fee_bps=10.0),
    # WhiteBIT perpetual-futures fees are not added here: the public figure
    # varies by pair and could not be pinned to a primary source, so a
    # whitebit/future run must pass maker_fee_bps and taker_fee_bps explicitly
    # rather than resolve to an unverified default.
}


def normalize_exchange_name(exchange_name: Any, *, default: str = "binance") -> str:
    raw = str(exchange_name or "").strip().lower()
    return raw or default


def normalize_exchange_type(exchange_type: Any, *, default: str = "future") -> str:
    raw = str(exchange_type or "").strip().lower()
    return raw or default


def is_futures_exchange_type(exchange_type: Any) -> bool:
    return normalize_exchange_type(exchange_type) in {"future", "futures", "usdm", "usd_m"}


def normalize_execution_mode(execution_mode: Any, *, default: str = DEFAULT_EXECUTION_MODE) -> str:
    raw = str(execution_mode or "").strip().lower()
    if raw in SUPPORTED_EXECUTION_MODES:
        return raw
    return default


def _canonical_exchange_type(exchange_type: str) -> str:
    """Canonical ``spot``/``future`` for a market string.

    Delegates to the same strict canonicaliser the adapter factory and the
    OHLCV cache use, so an unrecognised market string fails here too instead of
    silently resolving to spot fees.
    """
    from koval.exchanges.markets import canonical_market

    return canonical_market(exchange_type)


def _coerce_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _default_fee_schedule(exchange: str, exchange_type: str) -> FeeSchedule | None:
    """Return the built-in schedule for a venue/market, or None when there is none.

    A venue Koval has no published schedule for must not silently resolve to
    zero fees: a free backtest is the most optimistic result the engine can
    produce, and it would be produced by accident.
    """
    key = (exchange, _canonical_exchange_type(exchange_type))
    return _DEFAULT_FEE_SCHEDULES.get(key)


def resolve_execution_settings(
    config: Mapping[str, Any] | None,
) -> ExecutionSettings:
    raw = dict(config or {})

    exchange = normalize_exchange_name(raw.get("exchange"), default="binance")
    exchange_type = normalize_exchange_type(raw.get("exchange_type"), default="future")
    execution_mode = normalize_execution_mode(
        raw.get("execution_mode"), default=DEFAULT_EXECUTION_MODE
    )

    defaults = _default_fee_schedule(exchange, exchange_type)

    stored_fee_source = str(raw.get("fee_source") or "").strip().lower()
    allow_explicit_bps = stored_fee_source in {"", "config_override", "binance_api"}
    allow_legacy_taker_fee = stored_fee_source in {"", "legacy_taker_fee"}
    allow_legacy_commission = stored_fee_source in {"", "legacy_commission"}

    explicit_maker_bps = (
        _coerce_optional_float(raw.get("maker_fee_bps")) if allow_explicit_bps else None
    )
    explicit_taker_bps = (
        _coerce_optional_float(raw.get("taker_fee_bps")) if allow_explicit_bps else None
    )
    legacy_taker_fee_pct = (
        _coerce_optional_float(raw.get("taker_fee")) if allow_legacy_taker_fee else None
    )
    legacy_commission_rate = (
        _coerce_optional_float(raw.get("commission")) if allow_legacy_commission else None
    )

    fee_source = "exchange_default"

    maker_fee_bps = explicit_maker_bps
    taker_fee_bps = explicit_taker_bps
    if maker_fee_bps is not None or taker_fee_bps is not None:
        fee_source = "config_override"

    if taker_fee_bps is None and legacy_taker_fee_pct is not None:
        taker_fee_bps = legacy_taker_fee_pct * 100.0
        fee_source = "legacy_taker_fee"

    if maker_fee_bps is None and legacy_commission_rate is not None:
        maker_fee_bps = legacy_commission_rate * 10000.0
        fee_source = "legacy_commission"
    if taker_fee_bps is None and legacy_commission_rate is not None:
        taker_fee_bps = legacy_commission_rate * 10000.0
        fee_source = "legacy_commission"

    if (maker_fee_bps is None or taker_fee_bps is None) and defaults is None:
        raise ValueError(
            f"no default fee schedule for {exchange}/{_canonical_exchange_type(exchange_type)}; "
            "pass maker_fee_bps and taker_fee_bps"
        )
    if maker_fee_bps is None:
        maker_fee_bps = defaults.maker_fee_bps
    if taker_fee_bps is None:
        taker_fee_bps = defaults.taker_fee_bps

    broker_commission_bps = taker_fee_bps
    commission_rate = broker_commission_bps / 10000.0

    return ExecutionSettings(
        exchange=exchange,
        exchange_type=exchange_type,
        execution_mode=execution_mode,
        fee_source=fee_source,
        maker_fee_bps=float(maker_fee_bps),
        taker_fee_bps=float(taker_fee_bps),
        broker_commission_bps=float(broker_commission_bps),
        commission_rate=float(commission_rate),
    )


def apply_execution_settings(
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    base = dict(config or {})
    settings = resolve_execution_settings(base)
    base.update(settings.as_config_patch())
    return base
