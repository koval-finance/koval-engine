"""Deterministic, venue-safe client order identifiers."""

from __future__ import annotations

import hashlib


def make_client_order_id(
    session_id: str,
    intent_id: str,
    venue: str,
    role: str,
    *,
    max_len: int = 32,
) -> str:
    """Return a stable idempotency key safe for Binance and WhiteBIT."""

    if max_len < 16:
        raise ValueError("max_len must be at least 16")
    raw = "\x1f".join((session_id, intent_id, venue, role)).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return f"kv-{digest}"[:max_len]
