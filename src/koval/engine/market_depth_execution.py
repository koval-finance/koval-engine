"""Deterministic depth walking and explicit limit-queue approximation.

Depth fills consume only displayed quantities from one archived snapshot. The
queue model fills only from archived trades after its versioned queue-ahead
estimate is consumed; snapshots alone never create a resting-order fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def _positive(value: Decimal | str | int, *, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return parsed


def _non_negative(value: Decimal | str | int, *, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        _positive(self.price, name="book price")
        _positive(self.quantity, name="book quantity")


@dataclass(frozen=True)
class BookLevelChange:
    """One absolute Binance depth quantity; zero removes the level."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        _positive(self.price, name="book change price")
        _non_negative(self.quantity, name="book change quantity")


@dataclass(frozen=True)
class OrderBookSnapshot:
    timestamp_ms: int
    source: str
    source_sequence: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("book timestamp must be a non-negative integer")
        if (
            isinstance(self.source_sequence, bool)
            or not isinstance(self.source_sequence, int)
            or self.source_sequence < 0
        ):
            raise ValueError("book source sequence must be a non-negative integer")
        if not self.source or not self.bids or not self.asks:
            raise ValueError("book source and both sides are required")
        if list(self.bids) != sorted(self.bids, key=lambda level: level.price, reverse=True):
            raise ValueError("book bids must be strictly price-descending")
        if list(self.asks) != sorted(self.asks, key=lambda level: level.price):
            raise ValueError("book asks must be strictly price-ascending")
        if len({level.price for level in self.bids}) != len(self.bids):
            raise ValueError("book contains duplicate bid prices")
        if len({level.price for level in self.asks}) != len(self.asks):
            raise ValueError("book contains duplicate ask prices")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("book snapshot is crossed")


@dataclass(frozen=True)
class BookDelta:
    """A native Binance diff-depth event with its complete sequence envelope."""

    timestamp_ms: int
    source: str
    first_sequence: int
    final_sequence: int
    previous_final_sequence: int
    bids: tuple[BookLevelChange, ...]
    asks: tuple[BookLevelChange, ...]

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("book delta source is required")
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("book delta timestamp must be a non-negative integer")
        sequences = (
            self.first_sequence,
            self.final_sequence,
            self.previous_final_sequence,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in sequences
        ):
            raise ValueError("book delta sequences must be non-negative integers")
        if self.first_sequence > self.final_sequence:
            raise ValueError("book delta sequence range is inverted")
        if not self.bids and not self.asks:
            raise ValueError("book delta must change at least one level")


class OrderBookState:
    """Strict snapshot-plus-delta state; any uncertain sequence fails closed."""

    def __init__(self, snapshot: OrderBookSnapshot) -> None:
        self._snapshot = snapshot
        self._bridged = False

    @property
    def snapshot(self) -> OrderBookSnapshot:
        return self._snapshot

    def apply_delta(self, delta: BookDelta) -> OrderBookSnapshot:
        current = self._snapshot
        if delta.timestamp_ms < current.timestamp_ms:
            raise ValueError("book delta timestamp regression")
        if delta.final_sequence <= current.source_sequence:
            raise ValueError("duplicate or stale book delta")
        expected = current.source_sequence + 1
        if not self._bridged:
            if not delta.first_sequence <= expected <= delta.final_sequence:
                raise ValueError("first book delta does not bridge the snapshot sequence")
        elif delta.previous_final_sequence != current.source_sequence:
            raise ValueError("book delta sequence gap")
        next_snapshot = OrderBookSnapshot(
            timestamp_ms=delta.timestamp_ms,
            source=delta.source,
            source_sequence=delta.final_sequence,
            bids=_apply_changes(current.bids, delta.bids, reverse=True),
            asks=_apply_changes(current.asks, delta.asks, reverse=False),
        )
        self._snapshot = next_snapshot
        self._bridged = True
        return next_snapshot


def _apply_changes(
    levels: tuple[BookLevel, ...],
    changes: tuple[BookLevelChange, ...],
    *,
    reverse: bool,
) -> tuple[BookLevel, ...]:
    values = {level.price: level.quantity for level in levels}
    for change in changes:
        if change.quantity == 0:
            values.pop(change.price, None)
        else:
            values[change.price] = change.quantity
    return tuple(
        BookLevel(price=price, quantity=values[price]) for price in sorted(values, reverse=reverse)
    )


@dataclass(frozen=True)
class DepthOrderIntent:
    order_id: str
    side: str
    order_type: str
    quantity: Decimal
    eligible_timestamp_ms: int
    limit_price: Decimal | None = None
    time_in_force: str = "GTC"
    post_only: bool = False
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("depth order_id is required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("depth order side must be buy or sell")
        if self.order_type not in {"market", "limit"}:
            raise ValueError("depth order type must be market or limit")
        _positive(self.quantity, name="depth order quantity")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.limit_price is not None:
            _positive(self.limit_price, name="limit price")
        if self.time_in_force not in {"GTC", "IOC", "FOK"}:
            raise ValueError("unsupported time in force")
        if self.order_type == "market" and self.time_in_force != "GTC":
            raise ValueError("market depth intent does not accept limit time in force")
        if self.post_only and self.order_type != "limit":
            raise ValueError("post-only requires a limit order")
        if (
            isinstance(self.eligible_timestamp_ms, bool)
            or not isinstance(self.eligible_timestamp_ms, int)
            or self.eligible_timestamp_ms < 0
        ):
            raise ValueError("order eligibility timestamp must be non-negative")


@dataclass(frozen=True)
class DepthFill:
    price: Decimal
    quantity: Decimal
    level_index: int


@dataclass(frozen=True)
class DepthExecutionOutcome:
    order_id: str
    status: str
    fills: tuple[DepthFill, ...]
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    vwap: Decimal | None
    book_timestamp_ms: int
    book_sequence: int


def execute_against_depth(
    snapshot: OrderBookSnapshot,
    intent: DepthOrderIntent,
    *,
    position_quantity: Decimal | None = None,
    position_side: str | None = None,
) -> DepthExecutionOutcome:
    """Walk visible opposing depth without fabricating unavailable quantity."""
    _validate_reduce_only(intent, position_quantity=position_quantity, position_side=position_side)
    if snapshot.timestamp_ms < intent.eligible_timestamp_ms:
        return _empty_outcome(snapshot, intent, "not_yet_eligible")
    if intent.post_only and _would_cross(snapshot, intent):
        return _empty_outcome(snapshot, intent, "post_only_would_cross")

    levels = snapshot.asks if intent.side == "buy" else snapshot.bids
    eligible = [level for level in levels if _price_is_eligible(level.price, intent)]
    available = sum((level.quantity for level in eligible), Decimal("0"))
    requested = Decimal(str(intent.quantity))
    if intent.time_in_force == "FOK" and available < requested:
        return _empty_outcome(snapshot, intent, "fok_unfilled")

    remaining = requested
    fills: list[DepthFill] = []
    for level_index, level in enumerate(eligible):
        if remaining <= 0:
            break
        quantity = min(remaining, Decimal(str(level.quantity)))
        fills.append(
            DepthFill(price=Decimal(str(level.price)), quantity=quantity, level_index=level_index)
        )
        remaining -= quantity
    filled = requested - remaining
    notional = sum((fill.price * fill.quantity for fill in fills), Decimal("0"))
    vwap = notional / filled if filled else None
    if not remaining:
        status = "filled"
    elif intent.time_in_force == "IOC":
        status = "ioc_remainder_cancelled"
    elif intent.order_type == "market":
        status = "insufficient_liquidity"
    else:
        status = "partial"
    return DepthExecutionOutcome(
        order_id=intent.order_id,
        status=status,
        fills=tuple(fills),
        requested_quantity=requested,
        filled_quantity=filled,
        remaining_quantity=remaining,
        vwap=vwap,
        book_timestamp_ms=snapshot.timestamp_ms,
        book_sequence=snapshot.source_sequence,
    )


def _empty_outcome(
    snapshot: OrderBookSnapshot, intent: DepthOrderIntent, status: str
) -> DepthExecutionOutcome:
    requested = Decimal(str(intent.quantity))
    return DepthExecutionOutcome(
        order_id=intent.order_id,
        status=status,
        fills=(),
        requested_quantity=requested,
        filled_quantity=Decimal("0"),
        remaining_quantity=requested,
        vwap=None,
        book_timestamp_ms=snapshot.timestamp_ms,
        book_sequence=snapshot.source_sequence,
    )


def _would_cross(snapshot: OrderBookSnapshot, intent: DepthOrderIntent) -> bool:
    assert intent.limit_price is not None
    return (
        intent.limit_price >= snapshot.asks[0].price
        if intent.side == "buy"
        else intent.limit_price <= snapshot.bids[0].price
    )


def _price_is_eligible(price: Decimal, intent: DepthOrderIntent) -> bool:
    if intent.order_type == "market":
        return True
    assert intent.limit_price is not None
    return price <= intent.limit_price if intent.side == "buy" else price >= intent.limit_price


def _validate_reduce_only(
    intent: DepthOrderIntent,
    *,
    position_quantity: Decimal | None,
    position_side: str | None,
) -> None:
    if not intent.reduce_only:
        return
    if position_quantity is None or position_side not in {"buy", "sell"}:
        raise ValueError("reduce-only requires a current position")
    position = _positive(position_quantity, name="position quantity")
    closing_side = "sell" if position_side == "buy" else "buy"
    if intent.side != closing_side:
        raise ValueError("reduce-only side would increase exposure")
    if Decimal(str(intent.quantity)) > position:
        raise ValueError("reduce-only quantity exceeds current exposure")


@dataclass(frozen=True)
class QueueEstimatePolicy:
    version: str
    scenario: str
    cancellation_credit: Decimal

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("queue policy version is required")
        if self.scenario not in {"optimistic", "expected", "stressed"}:
            raise ValueError("unsupported queue scenario")
        credit = Decimal(str(self.cancellation_credit))
        if not credit.is_finite() or not Decimal("0") <= credit <= Decimal("1"):
            raise ValueError("queue cancellation credit must be in [0, 1]")


class LimitQueueModel:
    """Versioned FIFO queue-ahead approximation driven by trades/cancellations."""

    def __init__(
        self,
        *,
        order_id: str,
        side: str,
        limit_price: Decimal,
        quantity: Decimal,
        queue_ahead: Decimal,
        policy: QueueEstimatePolicy,
    ) -> None:
        if not order_id or side not in {"buy", "sell"}:
            raise ValueError("queue order identity and side are required")
        self._order_id = order_id
        self._side = side
        self._limit_price = _positive(limit_price, name="queue limit price")
        self._quantity = _positive(quantity, name="queue order quantity")
        self._queue_ahead = _non_negative(queue_ahead, name="queue-ahead quantity")
        self._filled = Decimal("0")
        self._policy = policy

    def apply_trade(self, *, price: Decimal, quantity: Decimal) -> Decimal:
        trade_price = _positive(price, name="trade price")
        available = _positive(quantity, name="trade quantity")
        if not self._trade_reaches_limit(trade_price):
            return Decimal("0")
        consumed_ahead = min(self._queue_ahead, available)
        self._queue_ahead -= consumed_ahead
        available -= consumed_ahead
        filled = min(self._quantity - self._filled, available)
        self._filled += filled
        return filled

    def apply_cancellation(self, *, price: Decimal, quantity: Decimal) -> Decimal:
        cancellation_price = _positive(price, name="cancellation price")
        cancelled = _positive(quantity, name="cancellation quantity")
        if cancellation_price != self._limit_price:
            return Decimal("0")
        credited = min(
            self._queue_ahead,
            cancelled * Decimal(str(self._policy.cancellation_credit)),
        )
        self._queue_ahead -= credited
        return credited

    def snapshot(self) -> dict[str, str]:
        return {
            "order_id": self._order_id,
            "policy_version": self._policy.version,
            "scenario": self._policy.scenario,
            "queue_ahead": str(self._queue_ahead),
            "filled_quantity": str(self._filled),
            "remaining_quantity": str(self._quantity - self._filled),
        }

    def _trade_reaches_limit(self, price: Decimal) -> bool:
        return price <= self._limit_price if self._side == "buy" else price >= self._limit_price


__all__ = [
    "BookDelta",
    "BookLevel",
    "BookLevelChange",
    "DepthExecutionOutcome",
    "DepthFill",
    "DepthOrderIntent",
    "LimitQueueModel",
    "OrderBookSnapshot",
    "OrderBookState",
    "QueueEstimatePolicy",
    "execute_against_depth",
]
