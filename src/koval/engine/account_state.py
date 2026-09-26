"""Platform Account State — per-run runtime service.

Source of truth for balance/equity/margin/drawdown surfaced read-only on
``BarContext.account``. Equity is fed from the broker each bar;
daily PnL, drawdown and margin are derived here. Holds only plain data so it
survives the process-pool job queue. Runtimes update it from execution events;
standalone GraphStrategy instances maintain a fallback through position hooks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from koval.engine.account_ledger import AccountLedger, LedgerReconciliation

_MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class OpenPosition:
    side: str  # "buy" | "sell"
    entry_price: float
    quantity: float
    current_stop: float
    margin: float


@dataclass(frozen=True)
class AccountSnapshot:
    balance: float
    equity: float
    free_margin: float
    margin_used: float
    unrealized_pnl: float
    realized_pnl: float
    daily_pnl: float
    peak_equity: float
    drawdown_pct: float
    open_positions: int
    open_position: OpenPosition | None
    daily_loss_pct: float = 0.0
    trade_realized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0


class PlatformAccountState:
    def __init__(
        self,
        starting_balance: float = 0.0,
        *,
        daily_baseline_equity: float | None = None,
        peak_equity: float | None = None,
        ledger: AccountLedger | None = None,
    ) -> None:
        bal = float(starting_balance)
        self._starting = bal
        if ledger is not None and ledger.starting_balance != bal:
            raise ValueError("account and broker ledger starting balances must match")
        self._ledger = ledger or AccountLedger(bal)
        self._equity = bal
        self._peak_equity = float(peak_equity) if peak_equity is not None else bal
        self._margin_used = 0.0
        self._position: OpenPosition | None = None
        self._current_day: int | None = None
        self._day_start_equity = (
            float(daily_baseline_equity) if daily_baseline_equity is not None else bal
        )

    @property
    def ledger(self) -> AccountLedger:
        return self._ledger

    def on_bar(self, *, equity: float, timestamp_ms: int) -> None:
        equity = float(equity)
        day = int(timestamp_ms) // _MS_PER_DAY
        if self._current_day is None:
            self._current_day = day
        elif day != self._current_day:
            self._current_day = day
            self._day_start_equity = self._equity
        self._equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

    def on_open(
        self,
        *,
        side: str,
        entry_price: float,
        quantity: float,
        current_stop: float,
        margin: float,
    ) -> None:
        self._position = OpenPosition(
            side=side,
            entry_price=float(entry_price),
            quantity=float(quantity),
            current_stop=float(current_stop),
            margin=float(margin),
        )
        self._margin_used += float(margin)

    def on_entry_fill(self, *, entry_price: float, quantity: float, margin: float) -> None:
        """Apply an additional entry-fill delta to the currently open position."""
        if self._position is None:
            raise ValueError("cannot resize an account position before it is open")
        delta_quantity = float(quantity)
        delta_margin = float(margin)
        if delta_quantity <= 0 or delta_margin < 0:
            raise ValueError("entry fill quantity must be positive and margin non-negative")
        current = self._position
        total_quantity = current.quantity + delta_quantity
        average_price = (
            current.entry_price * current.quantity + float(entry_price) * delta_quantity
        ) / total_quantity
        self._position = OpenPosition(
            side=current.side,
            entry_price=average_price,
            quantity=total_quantity,
            current_stop=current.current_stop,
            margin=current.margin + delta_margin,
        )
        self._margin_used += delta_margin

    def on_stop_update(self, new_stop: float) -> None:
        """Mirror a confirmed protective-stop update in the account snapshot."""
        if self._position is None:
            raise ValueError("cannot update the stop of a missing account position")
        stop = float(new_stop)
        if not math.isfinite(stop) or stop <= 0:
            raise ValueError("account stop update must be positive and finite")
        current = self._position
        increases_risk = (current.side == "buy" and stop < current.current_stop) or (
            current.side == "sell" and stop > current.current_stop
        )
        if increases_risk:
            raise ValueError("account stop update must not increase risk")
        self._position = replace(current, current_stop=stop)

    def on_partial_close(
        self,
        *,
        quantity: float,
        realized_pnl: float = 0.0,
        timestamp_ms: int = 0,
        reference_id: str = "",
        record_ledger: bool = True,
    ) -> None:
        """Release margin for an exit-fill delta while leaving the rest protected."""
        if self._position is None:
            raise ValueError("cannot partially close a missing account position")
        delta = float(quantity)
        current = self._position
        if delta <= 0 or delta >= current.quantity:
            raise ValueError("partial close quantity must be between zero and position quantity")
        if record_ledger:
            self._ledger.record(
                timestamp_ms=timestamp_ms,
                kind="trade_pnl",
                amount=float(realized_pnl),
                reference_id=reference_id,
            )
        remaining = current.quantity - delta
        remaining_margin = current.margin * (remaining / current.quantity)
        self._margin_used = max(0.0, self._margin_used - (current.margin - remaining_margin))
        self._position = OpenPosition(
            side=current.side,
            entry_price=current.entry_price,
            quantity=remaining,
            current_stop=current.current_stop,
            margin=remaining_margin,
        )

    def on_fee(self, amount: float, *, timestamp_ms: int = 0, reference_id: str = "") -> None:
        """Debit a venue or simulated commission from the wallet."""
        entry = self._ledger.record(
            timestamp_ms=timestamp_ms,
            kind="commission",
            amount=-float(amount),
            reference_id=reference_id,
        )
        self._apply_cash_movement(entry.amount)

    def on_funding(self, amount: float, *, timestamp_ms: int, reference_id: str = "") -> None:
        entry = self._ledger.record(
            timestamp_ms=timestamp_ms,
            kind="funding",
            amount=float(amount),
            reference_id=reference_id,
        )
        self._apply_cash_movement(entry.amount)

    def on_liquidation_fee(
        self, amount: float, *, timestamp_ms: int, reference_id: str = ""
    ) -> None:
        entry = self._ledger.record(
            timestamp_ms=timestamp_ms,
            kind="liquidation_fee",
            amount=-abs(float(amount)),
            reference_id=reference_id,
        )
        self._apply_cash_movement(entry.amount)

    def on_close(
        self,
        *,
        realized_pnl: float,
        timestamp_ms: int = 0,
        reference_id: str = "",
        record_ledger: bool = True,
    ) -> None:
        if record_ledger:
            self._ledger.record(
                timestamp_ms=timestamp_ms,
                kind="trade_pnl",
                amount=float(realized_pnl),
                reference_id=reference_id,
            )
        if self._position is not None:
            self._margin_used = max(0.0, self._margin_used - self._position.margin)
        self._position = None
        self._equity = self._ledger.balance
        self._peak_equity = max(self._peak_equity, self._equity)

    def _apply_cash_movement(self, amount: float) -> None:
        self._equity += float(amount)
        self._peak_equity = max(self._peak_equity, self._equity)

    def snapshot(self) -> AccountSnapshot:
        balance = self._ledger.balance
        peak = self._peak_equity if self._peak_equity > 0 else self._equity
        drawdown = (peak - self._equity) / peak * 100.0 if peak > 0 else 0.0
        daily_pnl = self._equity - self._day_start_equity
        daily_loss = (
            max(0.0, -daily_pnl / self._day_start_equity * 100.0)
            if self._day_start_equity > 0
            else 0.0
        )
        return AccountSnapshot(
            balance=balance,
            equity=self._equity,
            free_margin=self._equity - self._margin_used,
            margin_used=self._margin_used,
            unrealized_pnl=self._equity - balance,
            realized_pnl=balance - self._starting,
            daily_pnl=daily_pnl,
            daily_loss_pct=daily_loss,
            peak_equity=peak,
            drawdown_pct=max(0.0, drawdown),
            trade_realized_pnl=self._ledger.trade_realized_pnl,
            fees=self._ledger.fees,
            funding=self._ledger.funding,
            open_positions=1 if self._position is not None else 0,
            open_position=self._position,
        )

    def reconcile(self) -> LedgerReconciliation:
        snapshot = self.snapshot()
        return self._ledger.reconcile(
            unrealized_pnl=snapshot.unrealized_pnl,
            equity=snapshot.equity,
            margin_used=snapshot.margin_used,
        )
