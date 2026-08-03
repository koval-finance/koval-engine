from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def _safe_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def build_closed_trade_metrics(
    *,
    initial_capital: float,
    final_capital: float,
    closed_trades: Iterable[dict[str, Any]],
) -> dict[str, float | int | None]:
    trades = list(closed_trades or [])
    pnl_values = [_safe_float(trade.get("realized_pnl", 0.0)) for trade in trades]
    wins = [pnl for pnl in pnl_values if pnl > 0]
    losses = [pnl for pnl in pnl_values if pnl < 0]

    win_count = len(wins)
    loss_count = len(losses)
    total_trades = len(pnl_values)
    win_rate = (win_count / total_trades) * 100.0 if total_trades else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    if gross_losses == 0.0:
        profit_factor = 0.0 if gross_wins == 0.0 else None
    else:
        profit_factor = gross_wins / gross_losses

    return {
        "initial_capital": initial_capital,
        "final_capital": final_capital,
        "total_pnl": final_capital - initial_capital,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "win_count": win_count,
        "loss_count": loss_count,
        "avg_win": (gross_wins / win_count) if win_count else 0.0,
        "avg_loss": (sum(losses) / loss_count) if loss_count else 0.0,
    }
