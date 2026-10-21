"""Exact, session-attributed Spot cash and inventory from confirmed fill deltas."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Context, Decimal, InvalidOperation, localcontext
from functools import wraps

from koval.engine.account_state import OpenPosition, PlatformAccountState
from koval.engine.spot_contract import SpotFill, SpotIdentity

ZERO = Decimal(0)


def spot_decimal_context(method):
    """Keep host decimal settings out of deterministic execution accounting."""

    @wraps(method)
    def call(*args, **kwargs):
        # Accepted inputs span 18 powers above zero and 72 decimal places below.
        # Preserve small residuals alongside large inventory/notional products.
        with localcontext(Context(prec=256)):
            return method(*args, **kwargs)

    return call


def decimal_value(value, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid Spot decimal") from exc
    if (
        not number.is_finite()
        or number < 0
        or (positive and number == 0)
        or (number and (number.adjusted() > 18 or number.as_tuple().exponent < -72))
    ):
        raise ValueError("invalid Spot decimal")
    return number


class SpotLedger:
    """Base fees are expensed at fill price; unknown assets remain unvalued.

    The platform account is a float projection for the existing evaluator.
    Decimal cash/inventory remains the reconciliation authority.
    """

    def __init__(self, identity: SpotIdentity, capital: str, account=None):
        self.identity = identity
        self.capital = decimal_value(capital, positive=True)
        self.cash = self.capital
        self.quantity = ZERO
        self.cost = ZERO
        self.realized = ZERO
        self.valued_fees = ZERO
        self.fees: dict[str, Decimal] = defaultdict(Decimal)
        self.events: dict[str, SpotFill] = {}
        self.account = account or PlatformAccountState(float(self.capital), market="spot")

    @spot_decimal_context
    def apply(self, fill: SpotFill, *, stop_price: str = "0") -> bool:
        if not isinstance(fill, SpotFill) or not fill.event_id:
            raise ValueError("Spot fill requires an immutable event_id")
        if (
            isinstance(fill.timestamp_ms, bool)
            or not isinstance(fill.timestamp_ms, int)
            or fill.timestamp_ms < 0
        ):
            raise ValueError("Spot timestamp_ms must be a non-negative integer")
        if any(key.startswith("commission") for key in fill.metadata):
            raise ValueError("Spot commissions must use explicit SpotFee records")
        if fill.symbol != self.identity.symbol:
            raise ValueError("Spot fill symbol mismatch")
        if fill.event_id in self.events:
            if self.events[fill.event_id] != fill:
                raise ValueError("conflicting Spot event_id")
            return False
        if fill.status not in {"partial", "filled", "canceled", "expired"}:
            raise ValueError("invalid Spot event status")
        if fill.role not in {"entry", "stop", "target", "flatten"}:
            raise ValueError("invalid Spot fill role")
        if fill.side != ("buy" if fill.role == "entry" else "sell"):
            raise ValueError("invalid Spot execution side")
        quantity = decimal_value(fill.quantity or "0")
        if quantity == 0:
            if fill.status not in {"canceled", "expired"} or fill.fees:
                raise ValueError("zero quantity is only valid for fee-free terminal events")
            self.events[fill.event_id] = deepcopy(fill)
            return True
        price = decimal_value(fill.price, positive=True)
        fees: dict[str, Decimal] = defaultdict(Decimal)
        for fee in fill.fees:
            if not fee.asset or fee.asset != fee.asset.strip().upper():
                raise ValueError("invalid Spot fee asset")
            fees[fee.asset] += decimal_value(fee.quantity)
        base_fee = fees[self.identity.base_asset]
        quote_fee = fees[self.identity.quote_asset]
        valued = base_fee * price + quote_fee
        realized = ZERO
        if fill.role == "entry":
            delta = quantity - base_fee
            if delta <= 0:
                raise ValueError("base fee consumes acquired inventory")
            cash = self.cash - quantity * price - quote_fee
            new_quantity = self.quantity + delta
            new_cost = self.cost + delta * price
        else:
            delta = quantity + base_fee
            if delta > self.quantity:
                raise ValueError("Spot sell exceeds confirmed inventory")
            released = self.cost * delta / self.quantity
            realized = delta * price - released
            cash = self.cash + quantity * price - quote_fee
            new_quantity = self.quantity - delta
            new_cost = self.cost - released
        if cash < 0:
            raise ValueError("Spot fill exceeds allocated quote cash")
        # Validate everything before mutating either the exact or projected ledger.
        stop = float(decimal_value(stop_price))
        current = self.account.snapshot().open_position
        if fill.role != "entry":
            if new_quantity:
                self.account.ledger.record(
                    timestamp_ms=fill.timestamp_ms,
                    kind="trade_pnl",
                    amount=float(realized),
                    reference_id=fill.event_id,
                )
            else:
                self.account.on_close(
                    realized_pnl=float(realized),
                    timestamp_ms=fill.timestamp_ms,
                    reference_id=fill.event_id,
                )
        if new_quantity:
            self.account.set_position_projection(
                OpenPosition(
                    side="buy",
                    entry_price=float(new_cost / new_quantity),
                    quantity=float(new_quantity),
                    margin=float(new_cost),
                    current_stop=current.current_stop if current is not None else stop,
                )
            )
        if valued:
            self.account.on_fee(
                float(valued), timestamp_ms=fill.timestamp_ms, reference_id=fill.event_id
            )
        self.cash, self.quantity, self.cost = cash, new_quantity, new_cost
        self.realized += realized
        self.valued_fees += valued
        for asset, amount in fees.items():
            if amount:
                self.fees[asset] += amount
        self.events[fill.event_id] = deepcopy(fill)
        self.mark(str(price), fill.timestamp_ms)
        return True

    @spot_decimal_context
    def mark(self, price: str, timestamp_ms: int) -> None:
        mark = decimal_value(price, positive=True)
        self.account.on_bar(
            equity=float(self.cash + self.quantity * mark), timestamp_ms=timestamp_ms
        )

    @spot_decimal_context
    def snapshot(self, mark_price: str) -> dict:
        mark = decimal_value(mark_price, positive=True)
        unknown = sorted(set(self.fees) - {self.identity.base_asset, self.identity.quote_asset})
        return {
            "quote_cash": str(self.cash),
            "base_quantity": str(self.quantity),
            "inventory_cost": str(self.cost),
            "equity": str(self.cash + self.quantity * mark),
            "gross_realized_pnl": str(self.realized),
            "valued_fees": str(self.valued_fees),
            "net_realized_pnl": None if unknown else str(self.realized - self.valued_fees),
            "fees_by_asset": {asset: str(value) for asset, value in sorted(self.fees.items())},
            "valuation_complete": not unknown,
            "unvalued_fee_assets": unknown,
        }
