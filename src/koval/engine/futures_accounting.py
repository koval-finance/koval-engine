"""Decimal-safe single-asset cross and isolated Futures accounting.

This module calculates an auditable snapshot from explicit cash movements,
position terms, one mark event, and one applicable instrument/risk snapshot.
The liquidation price is an estimate under the selected maintenance tier; an
exchange-reported threshold, when available, must remain a separate value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from koval.engine.instrument_risk import (
    InstrumentSpecEvidence,
    MarkPriceRecord,
    evaluate_liquidation,
    validate_initial_leverage,
)


def _finite(value: Decimal | str | int, *, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _positive(value: Decimal | str | int, *, name: str) -> Decimal:
    parsed = _finite(value, name=name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _non_negative(value: Decimal | str | int, *, name: str) -> Decimal:
    parsed = _finite(value, name=name)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


@dataclass(frozen=True)
class FuturesCashLedger:
    initial_wallet_balance: Decimal
    realized_pnl: Decimal = Decimal("0")
    trading_fees: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    liquidation_fees: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _non_negative(self.initial_wallet_balance, name="initial wallet balance")
        _finite(self.realized_pnl, name="realized PnL")
        _non_negative(self.trading_fees, name="trading fees")
        _finite(self.funding, name="funding")
        _non_negative(self.liquidation_fees, name="liquidation fees")

    @property
    def wallet_balance(self) -> Decimal:
        return (
            Decimal(str(self.initial_wallet_balance))
            + Decimal(str(self.realized_pnl))
            + Decimal(str(self.funding))
            - Decimal(str(self.trading_fees))
            - Decimal(str(self.liquidation_fees))
        )


@dataclass(frozen=True)
class FuturesPosition:
    side: str
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal
    margin_mode: str = "cross"
    isolated_margin: Decimal | None = None

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError("Futures position side must be buy or sell")
        _positive(self.quantity, name="position quantity")
        _positive(self.entry_price, name="position entry price")
        _positive(self.leverage, name="position leverage")
        if self.margin_mode not in {"cross", "isolated"}:
            raise ValueError("Futures margin mode must be cross or isolated")
        if self.margin_mode == "cross" and self.isolated_margin is not None:
            raise ValueError("cross position cannot carry isolated margin")
        if self.margin_mode == "isolated":
            if self.isolated_margin is None:
                raise ValueError("isolated position requires isolated margin")
            _positive(self.isolated_margin, name="isolated margin")


@dataclass(frozen=True)
class FuturesAccountSnapshot:
    margin_mode: str
    wallet_balance: Decimal
    available_balance: Decimal
    equity: Decimal
    margin_used: Decimal
    maintenance_margin: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    trading_fees: Decimal
    funding: Decimal
    liquidation_fees: Decimal
    estimated_liquidation_price: Decimal | None
    liquidation_threshold_status: str
    liquidated: bool
    mark_price: Decimal
    mark_timestamp_ms: int
    instrument_evidence_id: str
    position_margin_equity: Decimal | None
    reconciled: bool


def futures_account_snapshot(
    *,
    ledger: FuturesCashLedger,
    position: FuturesPosition | None,
    mark: MarkPriceRecord,
    instrument: InstrumentSpecEvidence,
    evaluated_at_ms: int,
    maximum_mark_age_ms: int,
) -> FuturesAccountSnapshot:
    """Calculate one fail-closed single-position cross-margin snapshot."""
    if maximum_mark_age_ms < 0:
        raise ValueError("maximum mark age must be non-negative")
    if (
        evaluated_at_ms < mark.timestamp_ms
        or evaluated_at_ms - mark.timestamp_ms > maximum_mark_age_ms
    ):
        raise ValueError("mark-price evidence is stale")
    if not (
        instrument.effective_from_ms <= evaluated_at_ms
        and (instrument.effective_to_ms is None or evaluated_at_ms <= instrument.effective_to_ms)
    ):
        raise ValueError("instrument evidence does not cover the evaluation timestamp")

    wallet = ledger.wallet_balance
    mark_price = Decimal(str(mark.price))
    if position is None:
        return FuturesAccountSnapshot(
            margin_mode="cross",
            wallet_balance=wallet,
            available_balance=wallet,
            equity=wallet,
            margin_used=Decimal("0"),
            maintenance_margin=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal(str(ledger.realized_pnl)),
            trading_fees=Decimal(str(ledger.trading_fees)),
            funding=Decimal(str(ledger.funding)),
            liquidation_fees=Decimal(str(ledger.liquidation_fees)),
            estimated_liquidation_price=None,
            liquidation_threshold_status="not_applicable",
            liquidated=False,
            mark_price=mark_price,
            mark_timestamp_ms=mark.timestamp_ms,
            instrument_evidence_id=instrument.evidence_id,
            position_margin_equity=None,
            reconciled=True,
        )

    quantity = Decimal(str(position.quantity))
    contract_size = Decimal(str(instrument.contract_size))
    mark_notional = quantity * mark_price * contract_size
    validate_initial_leverage(
        instrument,
        notional=mark_notional,
        leverage=Decimal(str(position.leverage)),
    )
    margin_mode = position.margin_mode
    initial_margin = (
        quantity
        * Decimal(str(position.entry_price))
        * contract_size
        / Decimal(str(position.leverage))
    )
    if margin_mode == "isolated":
        assert position.isolated_margin is not None
        isolated_margin = Decimal(str(position.isolated_margin))
        if isolated_margin < initial_margin:
            raise ValueError("isolated margin must cover initial margin")
        if isolated_margin > wallet:
            raise ValueError("isolated margin exceeds wallet balance")
        risk_cash = isolated_margin
    else:
        isolated_margin = None
        risk_cash = wallet
    risk = evaluate_liquidation(
        instrument,
        side=position.side,
        quantity=quantity,
        entry_price=Decimal(str(position.entry_price)),
        cash_balance=risk_cash,
        mark_price=mark_price,
    )
    margin_used = (
        Decimal(str(isolated_margin))
        if isolated_margin is not None
        else mark_notional / Decimal(str(position.leverage))
    )
    estimated_liquidation = _estimated_liquidation_price(
        wallet_balance=risk_cash,
        position=position,
        contract_size=contract_size,
        maintenance_rate=Decimal(str(risk.margin_tier.maintenance_margin_rate)),
        maintenance_amount=Decimal(str(risk.margin_tier.maintenance_amount)),
    )
    total_equity = wallet + risk.unrealized_pnl
    available_balance = (
        wallet - isolated_margin if isolated_margin is not None else total_equity - margin_used
    )
    return FuturesAccountSnapshot(
        margin_mode=margin_mode,
        wallet_balance=wallet,
        available_balance=available_balance,
        equity=total_equity,
        margin_used=margin_used,
        maintenance_margin=risk.maintenance_margin,
        unrealized_pnl=risk.unrealized_pnl,
        realized_pnl=Decimal(str(ledger.realized_pnl)),
        trading_fees=Decimal(str(ledger.trading_fees)),
        funding=Decimal(str(ledger.funding)),
        liquidation_fees=Decimal(str(ledger.liquidation_fees)),
        estimated_liquidation_price=estimated_liquidation,
        liquidation_threshold_status="estimated_from_current_tier",
        liquidated=risk.liquidated,
        mark_price=mark_price,
        mark_timestamp_ms=mark.timestamp_ms,
        instrument_evidence_id=instrument.evidence_id,
        position_margin_equity=risk.equity,
        reconciled=(
            total_equity == wallet + risk.unrealized_pnl
            and risk.equity == risk_cash + risk.unrealized_pnl
        ),
    )


def _estimated_liquidation_price(
    *,
    wallet_balance: Decimal,
    position: FuturesPosition,
    contract_size: Decimal,
    maintenance_rate: Decimal,
    maintenance_amount: Decimal,
) -> Decimal:
    quantity = Decimal(str(position.quantity)) * contract_size
    entry_notional = Decimal(str(position.entry_price)) * quantity
    if position.side == "buy":
        numerator = entry_notional - wallet_balance - maintenance_amount
        denominator = quantity * (Decimal("1") - maintenance_rate)
    else:
        numerator = wallet_balance + entry_notional + maintenance_amount
        denominator = quantity * (Decimal("1") + maintenance_rate)
    return max(Decimal("0"), numerator / denominator)


__all__ = [
    "FuturesAccountSnapshot",
    "FuturesCashLedger",
    "FuturesPosition",
    "futures_account_snapshot",
]
