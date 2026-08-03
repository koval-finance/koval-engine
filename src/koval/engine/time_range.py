"""Convert ``YYYY-MM-DD`` date strings to UTC epoch milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime


def date_to_ms(date_str: str) -> int:
    """Return UTC-midnight epoch milliseconds for a ``YYYY-MM-DD`` string."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
