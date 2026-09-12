"""Framework-free trade narrative formatting from recorded facts only."""

from __future__ import annotations

import math


def _number(value: object, *, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and (not positive or number > 0) else None


def build_narrative(context: dict, trade: dict) -> str:
    """Describe actual price movement separately from recorded net trade PnL.

    Funding is an account cashflow and is never silently added to a trade.
    Missing context, prices, direction or PnL remain explicitly unrecorded.
    """
    side = trade.get("direction")
    direction = {"long": "Long", "short": "Short"}.get(side, "Direction not recorded:")
    entry = _number(trade.get("entry_price"), positive=True)
    exit_price = _number(trade.get("exit_price"), positive=True)
    pnl = _number(trade.get("pnl"))
    entry_text = f"at ${entry:,.2f}" if entry is not None else "at a price not recorded"
    lines = [f"Entry: {direction} position opened {entry_text}."]
    pattern = context.get("pattern")
    lines.append(f'Entry reason: "{pattern}".' if pattern else "Entry reason not recorded.")
    stop_loss = _number(context.get("stop_loss"), positive=True)
    take_profit = _number(context.get("take_profit"), positive=True)
    if stop_loss is not None and take_profit is not None:
        lines.append(f"Risk Setup: SL at ${stop_loss:,.2f}, TP at ${take_profit:,.2f}.")
    if entry is not None and exit_price is not None and side in {"long", "short"}:
        pct = (exit_price - entry) / entry * 100.0
        favorable = pct * (1 if side == "long" else -1) > 0
        movement = "in favor" if favorable else "against the position"
        lines.append(
            "Outcome: Price was unchanged."
            if pct == 0
            else f"Outcome: Price moved {abs(pct):.2f}% {movement}."
        )
    else:
        lines.append("Outcome: Price movement not recorded.")
    exit_text = context.get("exit_reason_text") or "reason not recorded"
    exit_at = f"at ${exit_price:,.2f}" if exit_price is not None else "at a price not recorded"
    lines.append(f"Exit: {exit_text}, {exit_at}.")
    lines.append(f"P&L: ${pnl:+,.2f} net." if pnl is not None else "P&L: not recorded.")
    return " ".join(lines)
