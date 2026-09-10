"""The one candle-history window every runtime injects into a strategy."""

from __future__ import annotations

DEFAULT_HISTORY_BARS = 1000
_MIN_HISTORY_BARS = 2
_MAX_HISTORY_BARS = 100_000


def resolve_history_bars(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("history_bars must be an integer number of closed bars")
    if not _MIN_HISTORY_BARS <= value <= _MAX_HISTORY_BARS:
        raise ValueError(
            f"history_bars must be between {_MIN_HISTORY_BARS} and {_MAX_HISTORY_BARS}"
        )
    return value
