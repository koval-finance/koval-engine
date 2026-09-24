"""Append-only cash ledger and reconciliation equations for simulated accounts."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Literal

from koval.engine.run_identity import content_sha256

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

    def checkpoint(self) -> dict:
        """Return an immutable-content identity for restart-safe restoration."""
        value = {
            "version": "koval_account_ledger_checkpoint_v1",
            "starting_balance": self._starting_balance,
            "entries": [asdict(entry) for entry in self._entries],
        }
        return {**value, "sha256": content_sha256(value)}

    @classmethod
    def from_checkpoint(cls, checkpoint: dict) -> AccountLedger:
        """Restore only a complete, untampered ledger prefix."""
        value = {key: item for key, item in checkpoint.items() if key != "sha256"}
        if checkpoint.get("sha256") != content_sha256(value):
            raise ValueError("ledger checkpoint hash mismatch")
        if value.get("version") != "koval_account_ledger_checkpoint_v1":
            raise ValueError("unsupported ledger checkpoint version")
        ledger = cls(value["starting_balance"])
        for expected, item in enumerate(value.get("entries", ()), 1):
            if item.get("sequence") != expected:
                raise ValueError("ledger checkpoint sequence is not contiguous")
            recorded = ledger.record(
                timestamp_ms=item["timestamp_ms"],
                kind=item["kind"],
                amount=item["amount"],
                reference_id=item.get("reference_id", ""),
                currency=item.get("currency", "quote"),
                metadata=item.get("metadata") or {},
            )
            if asdict(recorded) != item:
                raise ValueError("ledger checkpoint entry is not canonical")
        return ledger

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
