"""Framework-free trade narrative formatting."""

from __future__ import annotations


def build_narrative(context: dict, trade: dict) -> str:
    """Build a plain-language narrative from persisted context and trade data."""
    direction = "Long" if str(trade.get("direction")) == "long" else "Short"
    entry = float(trade.get("entry_price", 0.0) or 0.0)
    exit_price = float(trade.get("exit_price", 0.0) or 0.0)
    pnl = float(trade.get("pnl", 0.0) or 0.0)
    pct = float(trade.get("pnl_pct", 0.0) or 0.0)
    pattern = context.get("pattern") or "signal"
    exit_text = context.get("exit_reason_text") or "exit"

    lines = [f'Entry: {direction} position opened on "{pattern}" at ${entry:,.2f}.']
    stop_loss = float(context.get("stop_loss", 0.0) or 0.0)
    take_profit = float(context.get("take_profit", 0.0) or 0.0)
    if stop_loss and take_profit:
        lines.append(f"Risk Setup: SL at ${stop_loss:,.2f}, TP at ${take_profit:,.2f}.")
    outcome = "in favor" if pnl >= 0 else "against the position"
    lines.append(
        f"Outcome: Price moved {abs(pct):.2f}% {outcome}; "
        f"closed via {exit_text} at ${exit_price:,.2f}."
    )
    lines.append(f"P&L: ${pnl:+,.2f} net.")
    return " ".join(lines)
