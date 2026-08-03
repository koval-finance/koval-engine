from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EventType(StrEnum):
    SESSION_START = "SESSION_START"
    SESSION_END = "SESSION_END"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    FILTER_PASSED = "FILTER_PASSED"
    FILTER_REJECTED = "FILTER_REJECTED"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    TRADE_OPENED = "TRADE_OPENED"
    TRADE_CLOSED = "TRADE_CLOSED"
    DRAWDOWN_LIMIT_HIT = "DRAWDOWN_LIMIT_HIT"


@dataclass
class EngineEvent:
    event_type: EventType
    bar_index: int
    timestamp_ms: int
    payload: dict = field(default_factory=dict)
