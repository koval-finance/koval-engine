"""Pure risk-gate checks for the trade and portfolio risk gates.

Each returns ``(allowed, reason)`` — ``reason`` is the first failing rule, else
``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from koval.engine.account_state import AccountSnapshot


def trade_risk_check(
    *,
    entry: float,
    stop: float,
    target: float | None,
    leverage: float,
    min_rr: float,
    max_stop_distance_pct: float,
    max_leverage: float,
) -> tuple[bool, str | None]:
    stop_distance = abs(entry - stop)
    if entry > 0 and (stop_distance / entry) * 100.0 > max_stop_distance_pct:
        return False, f"stop distance {stop_distance / entry * 100:.2f}% > {max_stop_distance_pct}%"
    if leverage > max_leverage:
        return False, f"leverage {leverage} > {max_leverage}"
    if target is not None and stop_distance > 0:
        rr = abs(target - entry) / stop_distance
        if rr < min_rr:
            return False, f"rr {rr:.2f} < {min_rr}"
    return True, None


def portfolio_risk_check(
    *,
    account: AccountSnapshot,
    required_margin: float,
    max_daily_drawdown_pct: float,
    max_concurrent_positions: int,
    max_total_margin_pct: float,
) -> tuple[bool, str | None]:
    if account.drawdown_pct > max_daily_drawdown_pct:
        return False, f"daily drawdown {account.drawdown_pct:.2f}% > {max_daily_drawdown_pct}%"
    if account.open_positions >= max_concurrent_positions:
        return False, (
            f"concurrent positions {account.open_positions} >= {max_concurrent_positions}"
        )
    if account.equity > 0:
        projected_pct = (account.margin_used + required_margin) / account.equity * 100.0
        if projected_pct > max_total_margin_pct:
            return False, f"total margin {projected_pct:.1f}% > {max_total_margin_pct}%"
    return True, None
