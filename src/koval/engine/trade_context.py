"""Map raw analyzer trade data to persisted per-trade context."""

from __future__ import annotations

from typing import Any

_EXIT_CANON = {
    "Take Profit": "tp",
    "Take Profit (Approx)": "tp",
    "Stop Loss": "sl",
    "Stop Loss (Approx)": "sl",
    "Trailing Stop": "trailing",
    "Breakeven": "breakeven",
    "Manual": "manual",
    "End of Data": "eod",
    "End-of-Data": "eod",
}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def context_from_raw(raw: dict[str, Any], trade_id: int) -> dict[str, Any]:
    """Project one raw analyzer dict to the persisted trade-context shape."""
    why_entry = _as_list(raw.get("why_entry"))
    exit_text = str(raw.get("exit_reason", "")) or "Unknown"
    return {
        "trade_id": trade_id,
        "pattern": (
            str(raw.get("reason"))
            if raw.get("reason")
            else (str(why_entry[0]) if why_entry else "")
        ),
        "stop_loss": float(raw.get("stop_loss", 0.0) or 0.0),
        "take_profit": float(raw.get("take_profit", 0.0) or 0.0),
        "sl_calculation": str(raw.get("sl_calculation", "") or ""),
        "tp_calculation": str(raw.get("tp_calculation", "") or ""),
        "exit_reason": _EXIT_CANON.get(exit_text, "other"),
        "exit_reason_text": exit_text,
        "sl_history": _as_list(raw.get("sl_history")),
        "why_entry": why_entry,
        "why_exit": _as_list(raw.get("why_exit")),
        "indicators_at_entry": dict(raw.get("indicators_at_entry") or {}),
        "metadata": dict(raw.get("metadata") or {}),
    }
