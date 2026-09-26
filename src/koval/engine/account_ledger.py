"""Append-only cash ledger and reconciliation equations for simulated accounts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

LedgerKind = Literal["commission", "funding", "trade_pnl", "liquidation_fee", "adjustment"]


@dataclass(frozen=True)
class LedgerEntry:
    sequence: int
    timestamp_ms: int
    kind: LedgerKind
    amount: float
    reference_id: str = ""
    currency: str = "quote"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerReconciliation:
    starting_balance: float
    cash_movements: float
    balance: float
    unrealized_pnl: float
    equity: float
    margin_used: float
    free_margin: float
    balanced: bool


class AccountLedger:
    """Records every balance mutation once and derives account totals from it."""

    def __init__(self, starting_balance: float) -> None:
        starting = float(starting_balance)
        if not math.isfinite(starting) or starting < 0:
            raise ValueError("ledger starting balance must be non-negative and finite")
        self._starting_balance = starting
        self._entries: list[LedgerEntry] = []

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    @property
    def starting_balance(self) -> float:
        return self._starting_balance

    @property
    def balance(self) -> float:
        return self._starting_balance + sum(entry.amount for entry in self._entries)

    @property
    def fees(self) -> float:
        return -sum(
            entry.amount
            for entry in self._entries
            if entry.kind in {"commission", "liquidation_fee"}
        )

    @property
    def funding(self) -> float:
        return sum(entry.amount for entry in self._entries if entry.kind == "funding")

    @property
    def trade_realized_pnl(self) -> float:
        return sum(entry.amount for entry in self._entries if entry.kind == "trade_pnl")

    def record(
        self,
        *,
        timestamp_ms: int,
        kind: LedgerKind,
        amount: float,
        reference_id: str = "",
        currency: str = "quote",
        metadata: dict[str, object] | None = None,
    ) -> LedgerEntry:
        parsed = float(amount)
        if not math.isfinite(parsed):
            raise ValueError("ledger amount must be finite")
        if isinstance(timestamp_ms, bool) or int(timestamp_ms) != timestamp_ms or timestamp_ms < 0:
            raise ValueError("ledger timestamp_ms must be a non-negative integer")
        entry = LedgerEntry(
            sequence=len(self._entries) + 1,
            timestamp_ms=int(timestamp_ms),
            kind=kind,
            amount=parsed,
            reference_id=str(reference_id),
            currency=str(currency),
            metadata=dict(metadata or {}),
        )
        self._entries.append(entry)
        return entry

    def reconcile(
        self, *, unrealized_pnl: float, equity: float, margin_used: float
    ) -> LedgerReconciliation:
        unrealized = float(unrealized_pnl)
        observed_equity = float(equity)
        margin = float(margin_used)
        balance = self.balance
        expected_equity = balance + unrealized
        return LedgerReconciliation(
            starting_balance=self._starting_balance,
            cash_movements=balance - self._starting_balance,
            balance=balance,
            unrealized_pnl=unrealized,
            equity=observed_equity,
            margin_used=margin,
            free_margin=observed_equity - margin,
            balanced=math.isclose(expected_equity, observed_equity, rel_tol=1e-12, abs_tol=1e-9),
        )


__all__ = ["AccountLedger", "LedgerEntry", "LedgerKind", "LedgerReconciliation"]
