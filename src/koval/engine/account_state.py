"""Platform Account State — per-run runtime service.

Source of truth for balance/equity/margin/drawdown surfaced read-only on
``BarContext.account``. Equity is fed from the broker each bar;
daily PnL, drawdown and margin are derived here. Holds only plain data so it
survives the process-pool job queue. Not a graph node — updated via GraphStrategy
hooks (``on_bar`` / ``on_open`` / ``on_close``).
"""

from __future__ import annotations

from dataclasses import dataclass

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


class PlatformAccountState:
    def __init__(self, starting_balance: float = 0.0) -> None:
        bal = float(starting_balance)
        self._starting = bal
        self._realized = 0.0
        self._equity = bal
        self._peak_equity = bal
        self._margin_used = 0.0
        self._position: OpenPosition | None = None
        self._current_day: int | None = None
        self._day_start_equity = bal

    def on_bar(self, *, equity: float, timestamp_ms: int) -> None:
        equity = float(equity)
        day = int(timestamp_ms) // _MS_PER_DAY
        if self._current_day is None or day != self._current_day:
            # daily_pnl baseline is the FIRST bar's equity of the new UTC day, so
            # any overnight gap between days is excluded from both days' daily_pnl.
            self._current_day = day
            self._day_start_equity = equity
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

    def on_close(self, *, realized_pnl: float) -> None:
        self._realized += float(realized_pnl)
        if self._position is not None:
            self._margin_used = max(0.0, self._margin_used - self._position.margin)
        self._position = None

    def snapshot(self) -> AccountSnapshot:
        balance = self._starting + self._realized
        peak = self._peak_equity if self._peak_equity > 0 else self._equity
        drawdown = (peak - self._equity) / peak * 100.0 if peak > 0 else 0.0
        return AccountSnapshot(
            balance=balance,
            equity=self._equity,
            free_margin=self._equity - self._margin_used,
            margin_used=self._margin_used,
            unrealized_pnl=self._equity - balance,
            realized_pnl=self._realized,
            daily_pnl=self._equity - self._day_start_equity,
            peak_equity=peak,
            drawdown_pct=max(0.0, drawdown),
            open_positions=1 if self._position is not None else 0,
            open_position=self._position,
        )
