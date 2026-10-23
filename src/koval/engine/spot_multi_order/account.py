"""Pure attributed execution accounting and per-order reservations."""

from decimal import Decimal

from koval.engine.spot_numbers import decimal_text, exact

from .models import (
    TERMINAL,
    ZERO,
    SpotAccountingViolation,
    SpotExecutionEventV1,
    SpotFeeValuationV1,
    SpotLotV1,
    SpotOrderIntentV1,
    SpotOrderStateV1,
    SpotReconciliationV1,
    SpotReplaceIntentV1,
)


def reserved_quote(state):
    return sum((o.reserved_quote for o in state.orders.values()), ZERO)


def reserved_base(state):
    return sum((o.reserved_base for o in state.orders.values()), ZERO)


@exact
def free_quote(state):
    return (
        state.account.allocation_cash
        - reserved_quote(state)
        - state.account.cleanup_reserve
        - state.account.cycle_reserve
    )


@exact
def free_base(state):
    return state.account.net_base - reserved_base(state)


def attributed(state, value):
    if (value.account_id, value.session_id) != (state.binding.account_id, state.binding.session_id):
        raise ValueError("Spot account/session mismatch")
    if hasattr(value, "symbol") and value.symbol != state.binding.symbol:
        raise ValueError("Spot symbol mismatch")


def available_lots(state, intent):
    lots = sorted(state.account.lots.values(), key=lambda lot: (lot.creation_sequence, lot.lot_id))
    if state.recipe.kind == "grid" and intent.purpose == "grid_sell":
        lots = [lot for lot in lots if lot.slot_id == intent.slot_id]
        if intent.parent_order_id is not None:
            lots = [lot for lot in lots if lot.lot_id == intent.parent_order_id]
    return lots


@exact
def submit_spot_order(state, intent):
    """Admit/reserve a local intent only; this function performs no dispatch."""
    intent = SpotOrderIntentV1.model_validate(
        intent.model_dump() if hasattr(intent, "model_dump") else intent
    )
    attributed(state, intent)
    if state.reconciliation_required or state.quarantine:
        raise ValueError("Spot reconciliation required before another write")
    if intent.side == "buy" and (
        state.fee_reconciliation_required
        or any(
            fee["quote_value"] is None and Decimal(fee["quantity"])
            for fee in state.account.fee_records.values()
        )
    ):
        raise ValueError("unvalued fee requires valuation and reconciliation before BUY")
    previous = state.orders.get(intent.client_order_id)
    if previous:
        if previous.intent == intent:
            return state.model_copy(deep=True)
        raise ValueError("conflicting Spot order identity")
    if state.controller.terminal_action and intent.purpose != "flatten":
        raise ValueError("terminal barrier freezes new orders")
    if (
        sum(not o.terminal_reconciled for o in state.orders.values())
        >= state.instrument.maximum_open_orders
    ):
        raise ValueError("owned open order capacity exhausted")
    for old in state.orders.values():
        if (old.intent.cycle_id, old.intent.slot_id, old.intent.purpose) == (
            intent.cycle_id,
            intent.slot_id,
            intent.purpose,
        ):
            if not old.terminal_reconciled or old.intent.generation >= intent.generation:
                raise ValueError("stale or overlapping order generation")
    candidate = state.model_copy(deep=True)
    rules = candidate.instrument
    step = rules.market_quantity_step if intent.order_type == "market" else rules.quantity_step
    if intent.quantity is not None:
        if (
            intent.quantity % step
            or intent.quantity
            < (
                rules.market_minimum_quantity or rules.minimum_quantity
                if intent.order_type == "market"
                else rules.minimum_quantity
            )
            or intent.quantity
            > (
                rules.market_maximum_quantity or rules.maximum_quantity
                if intent.order_type == "market"
                else rules.maximum_quantity
            )
        ):
            raise ValueError("order violates quantity evidence")
    if intent.limit_price is not None:
        if (
            intent.limit_price % rules.price_tick
            or not rules.minimum_price <= intent.limit_price <= rules.maximum_price
        ):
            raise ValueError("order violates price evidence")
        if (
            rules.price_band_low is not None
            and not rules.price_band_low <= intent.limit_price <= rules.price_band_high
        ):
            raise ValueError("order violates price-band evidence")
        notional = (
            intent.quantity * intent.limit_price
            if intent.quantity is not None
            else intent.quote_notional_cap
        )
        if not rules.minimum_notional <= notional <= rules.maximum_notional:
            raise ValueError("order violates notional evidence")
    record = SpotOrderStateV1(intent=intent, accepted_sequence=len(candidate.orders) + 1)
    if intent.side == "buy":
        notional = (
            intent.quote_notional_cap
            if intent.quote_notional_cap is not None
            else intent.quantity * intent.limit_price
            if intent.limit_price
            else None
        )
        if notional is None:
            raise ValueError("market BUY requires a bounded quote cap")
        record.reserved_quote = notional * (1 + candidate.policy.buy_quote_fee_cap_bps / 10000)
        transfer = (
            min(candidate.account.cycle_reserve, record.reserved_quote)
            if intent.purpose in {"base", "addition"}
            else ZERO
        )
        candidate.account.cycle_reserve -= transfer
        if record.reserved_quote > free_quote(candidate):
            raise ValueError("insufficient reserved quote cash")
    else:
        record.reserved_base = intent.quantity * (
            1 + candidate.policy.sell_base_fee_cap_bps / 10000
        )
        if record.reserved_base > free_base(candidate):
            raise ValueError("SELL exceeds free base inventory")
        remaining = record.reserved_base
        for lot in available_lots(candidate, intent):
            used = sum(
                (o.allocations.get(lot.lot_id, ZERO) for o in candidate.orders.values()), ZERO
            )
            take = min(lot.quantity - used, remaining)
            if take > 0:
                record.allocations[lot.lot_id] = take
                remaining -= take
        if remaining:
            raise ValueError("SELL exceeds attributed lot inventory")
    candidate.orders[intent.client_order_id] = record
    candidate.trace.append(
        {
            "kind": "submit",
            "intent": intent.model_dump(mode="json"),
            "reserved_quote": decimal_text(record.reserved_quote),
            "reserved_base": decimal_text(record.reserved_base),
        }
    )
    return candidate


def cancel_spot_order(state, client_order_id, *, command_id, generation):
    if state.reconciliation_required or state.quarantine:
        raise ValueError("Spot reconciliation required before cancellation")
    candidate = state.model_copy(deep=True)
    order = candidate.orders[client_order_id]
    if order.intent.generation != generation:
        raise ValueError("stale cancellation generation")
    if order.cancel_command_id and order.cancel_command_id != command_id:
        raise ValueError("conflicting pending cancellation")
    if order.terminal_reconciled or order.cancel_command_id:
        return candidate
    order.cancel_command_id = command_id
    if order.status not in TERMINAL:
        order.status = "cancel_pending"
    candidate.trace.append(
        {
            "kind": "cancel",
            "client_order_id": client_order_id,
            "generation": generation,
            "command_id": command_id,
        }
    )
    return candidate


def replace_spot_order(state, request):
    request = SpotReplaceIntentV1.model_validate(request)
    attributed(state, request)
    if state.reconciliation_required or state.quarantine:
        raise ValueError("Spot reconciliation required before replacement")
    old = state.orders[request.client_order_id]
    new = request.replacement
    attributed(state, new)
    if old.intent.generation != request.generation or new.generation != request.generation + 1:
        raise ValueError("stale replacement generation")
    if (new.cycle_id, new.slot_id, new.purpose, new.side) != (
        old.intent.cycle_id,
        old.intent.slot_id,
        old.intent.purpose,
        old.intent.side,
    ) or new.client_order_id == request.client_order_id:
        raise ValueError("replacement order lineage mismatch")
    prior = state.replacements.get(request.client_order_id)
    if prior and prior != request:
        raise ValueError("conflicting pending replacement")
    candidate = state.model_copy(deep=True)
    candidate.replacements[request.client_order_id] = request
    if old.terminal_reconciled:
        return submit_spot_order(candidate, new)
    return cancel_spot_order(
        candidate,
        request.client_order_id,
        command_id=request.command_id,
        generation=request.generation,
    )


@exact
def _consume_lots(state, order, removed, sold):
    remaining = removed
    released = ZERO
    allocations = []
    # DCA's average-cost releases consume each lot in proportion, preserving
    # exact deferred-fee weights. Grid consumes its explicitly reserved lots.
    if state.recipe.kind == "dca":
        total = state.account.net_base
        basket_cost = state.account.remaining_cost
        lots = [lot for lot in available_lots(state, order.intent) if lot.quantity]
        shares = [
            (lot, lot.quantity if removed == total else removed * lot.quantity / total)
            for lot in lots
        ]
        if shares:
            if removed != total:
                shares[-1] = (shares[-1][0], removed - sum((q for _, q in shares[:-1]), ZERO))
    else:
        shares = []
        for lot in available_lots(state, order.intent):
            take = min(order.allocations.get(lot.lot_id, ZERO), remaining)
            if take:
                shares.append((lot, take))
                remaining -= take
        if remaining:
            raise SpotAccountingViolation("SELL exceeds its reserved lot inventory")
    for lot, take in shares:
        cost = lot.cost * take / lot.quantity
        ratio = take / lot.quantity
        for fee in state.account.fee_records.values():
            if fee["side"] == "buy" and fee["lot_id"] == lot.lot_id:
                fee["remaining_weight"] = decimal_text(
                    Decimal(fee["remaining_weight"]) * (1 - ratio)
                )
        lot.quantity -= take
        lot.cost -= cost
        released += cost
        order.allocations[lot.lot_id] = max(ZERO, order.allocations.get(lot.lot_id, ZERO) - take)
        allocations.append(
            {"lot_id": lot.lot_id, "quantity": decimal_text(take), "cost": decimal_text(cost)}
        )
    state.trace.append(
        {
            "kind": "lot_release",
            "client_order_id": order.intent.client_order_id,
            "allocations": allocations,
            "sold_base": decimal_text(sold),
        }
    )
    if state.recipe.kind == "dca" and shares:
        # The basket is authoritative. Assign division roundoff to its last lot
        # so repeated proportional releases preserve exact cash/base totals.
        released = basket_cost if removed == total else basket_cost * removed / total
        last = shares[-1][0]
        last.quantity = (total - removed) - sum((lot.quantity for lot in lots[:-1]), ZERO)
        last.cost = (basket_cost - released) - sum((lot.cost for lot in lots[:-1]), ZERO)
    return released


@exact
def _account_fill(state, event, order):
    account = state.account
    fees = {}
    for fee in event.fees:
        fees[fee.asset] = fees.get(fee.asset, ZERO) + fee.quantity
    base_fee = fees.get(state.binding.base_asset, ZERO)
    quote_fee = fees.get(state.binding.quote_asset, ZERO)
    rate_cap = max(state.policy.maker_fee_bps, state.policy.taker_fee_bps)
    if (
        event.side == "buy"
        and base_fee > event.quantity * rate_cap / 10000
        or event.side == "sell"
        and quote_fee > event.quote_notional * rate_cap / 10000
    ):
        raise SpotAccountingViolation("execution fee exceeds declared cost policy")
    if (
        event.side == "buy"
        and quote_fee > event.quote_notional * state.policy.buy_quote_fee_cap_bps / 10000
    ):
        raise SpotAccountingViolation("BUY quote fee exceeds policy cap")
    if (
        event.side == "sell"
        and base_fee > event.quantity * state.policy.sell_base_fee_cap_bps / 10000
    ):
        raise SpotAccountingViolation("SELL base fee exceeds policy cap")
    if event.side == "buy":
        debit = event.quote_notional + quote_fee
        net = event.quantity - base_fee
        if net <= 0 or debit > order.reserved_quote or debit > account.allocation_cash:
            raise SpotAccountingViolation("BUY exceeds reserved cash or acquired base")
        account.quote_cash -= debit
        account.allocation_cash -= debit
        account.net_base += net
        account.remaining_cost += debit
        account.bought_base += event.quantity
        account.bought_quote += event.quote_notional
        lot = account.lots.setdefault(
            event.client_order_id,
            SpotLotV1(
                lot_id=event.client_order_id,
                slot_id=order.intent.slot_id,
                creation_sequence=event.source_sequence,
            ),
        )
        lot.quantity += net
        lot.cost += debit
        lot.bought_base += event.quantity
        lot.bought_quote += event.quote_notional
        order.reserved_quote -= debit
    else:
        removed = event.quantity + base_fee
        if removed > order.reserved_base or removed > account.net_base:
            raise SpotAccountingViolation("SELL exceeds reserved base inventory")
        released = _consume_lots(state, order, removed, event.quantity)
        proceeds = event.quote_notional - quote_fee
        if proceeds < 0:
            raise SpotAccountingViolation("SELL quote fee exceeds proceeds")
        account.quote_cash += proceeds
        account.allocation_cash += proceeds
        account.net_base -= removed
        account.remaining_cost -= released
        account.realized_profit += proceeds - released
        order.reserved_base -= removed
    for index, fee in enumerate(event.fees):
        account.fees_by_asset[fee.asset] = account.fees_by_asset.get(fee.asset, ZERO) + fee.quantity
        fee_id = f"{event.event_id}:{index}"
        third = fee.asset not in {state.binding.base_asset, state.binding.quote_asset}
        account.fee_records[fee_id] = {
            "asset": fee.asset,
            "quantity": decimal_text(fee.quantity),
            "side": event.side,
            "lot_id": event.client_order_id if event.side == "buy" else None,
            "remaining_weight": "1" if event.side == "buy" else "0",
            "quote_value": None
            if third
            else decimal_text(
                fee.quantity * (event.price if fee.asset == state.binding.base_asset else 1)
            ),
        }
        if third and fee.quantity:
            if fee.quote_value is None:
                state.controller.refusal_reason = "unvalued_fee"
                state.fee_reconciliation_required = True
            else:
                if not fee.source:
                    raise ValueError("valued third-asset fee requires provenance")
                _value_fee(state, fee_id, fee.quote_value)
    if free_quote(state) < 0 or free_base(state) < 0:
        raise SpotAccountingViolation("fill violates another order's reservation")


@exact
def _value_fee(state, fee_id, value):
    record = state.account.fee_records[fee_id]
    if state.account.valued_third_fees + value > state.policy.third_asset_allowance_quote:
        raise SpotAccountingViolation("third-asset economic fee allowance exceeded")
    record["quote_value"] = decimal_text(value)
    state.account.valued_third_fees += value
    weight = Decimal(record["remaining_weight"])
    if record["side"] == "buy":
        remaining = value * weight
        state.account.remaining_cost += remaining
        state.account.lots[record["lot_id"]].cost += remaining
        state.account.realized_profit -= value - remaining
    else:
        state.account.realized_profit -= value


@exact
def apply_spot_fee_valuation(state, valuation):
    valuation = SpotFeeValuationV1.model_validate(valuation)
    attributed(state, valuation)
    payload = valuation.model_dump(mode="json")
    if valuation.valuation_id in state.valuations:
        if state.valuations[valuation.valuation_id] != payload:
            raise ValueError("conflicting fee valuation ID")
        return state.model_copy(deep=True)
    record = state.account.fee_records.get(valuation.fee_id)
    if (
        not record
        or record["quote_value"] is not None
        or (record["asset"], Decimal(record["quantity"])) != (valuation.asset, valuation.quantity)
    ):
        raise ValueError("unattributed or already valued fee")
    candidate = state.model_copy(deep=True)
    _value_fee(candidate, valuation.fee_id, valuation.quote_value)
    candidate.valuations[valuation.valuation_id] = payload
    candidate.trace.append({"kind": "fee_valuation", "valuation": payload})
    return candidate


@exact
def apply_spot_event(state, event):
    event = SpotExecutionEventV1.model_validate(
        event.model_dump() if hasattr(event, "model_dump") else event
    )
    attributed(state, event)
    previous = state.events.get(event.event_id) or state.buffered_events.get(event.event_id)
    if previous is None:
        previous = next(
            (
                SpotExecutionEventV1.model_validate(record["event"])
                for record in state.quarantine
                if record["event"]["event_id"] == event.event_id
            ),
            None,
        )
    if previous:
        if previous != event:
            raise ValueError("conflicting Spot event_id")
        return state.model_copy(deep=True)
    order = state.orders.get(event.client_order_id)
    if order is None or (event.generation, event.side, event.purpose) != (
        order.intent.generation,
        order.intent.side,
        order.intent.purpose,
    ):
        raise ValueError("Spot account/order/generation mismatch")
    if order.exchange_order_id and event.exchange_order_id != order.exchange_order_id:
        raise ValueError("Spot exchange order mismatch")
    if event.trade_id and event.trade_id in state.trade_ids:
        raise ValueError("conflicting/reused trade ID")
    candidate = state.model_copy(deep=True)
    if event.source_sequence != state.event_sequence + 1 or order.terminal_reconciled:
        if len(candidate.buffered_events) >= 1024:
            raise ValueError("execution reorder buffer exhausted")
        if any(
            e.source_sequence == event.source_sequence for e in candidate.events.values()
        ) or any(
            e.source_sequence == event.source_sequence for e in candidate.buffered_events.values()
        ):
            raise ValueError("conflicting execution sequence")
        candidate.buffered_events[event.event_id] = event
        candidate.reconciliation_required = True
        return candidate
    record = candidate.orders[event.client_order_id]
    if (
        event.cumulative_quantity != record.filled_quantity + event.quantity
        or event.cumulative_quote != record.filled_quote + event.quote_notional
    ):
        raise ValueError("execution cumulative totals mismatch")
    overfill = (
        record.intent.quantity is not None
        and event.cumulative_quantity > record.intent.quantity
        or record.intent.quote_notional_cap is not None
        and event.cumulative_quote > record.intent.quote_notional_cap
    )
    if record.status in TERMINAL:
        raise ValueError("new execution after terminal watermark")
    try:
        if overfill:
            raise SpotAccountingViolation("attributed order overfill exceeds reservation")
        if event.quantity:
            _account_fill(candidate, event, record)
    except SpotAccountingViolation as exc:
        quarantined = state.model_copy(deep=True)
        quarantined.quarantine.append({"event": event.model_dump(mode="json"), "reason": str(exc)})
        quarantined.reconciliation_required = True
        return quarantined
    record.filled_quantity = event.cumulative_quantity
    record.filled_quote = event.cumulative_quote
    record.exchange_order_id = event.exchange_order_id
    if event.status == "unknown":
        candidate.reconciliation_required = True
        record.unknown_outcome = "cancel" if record.cancel_command_id else "submit"
    elif event.status in TERMINAL or record.unknown_outcome == "submit":
        record.unknown_outcome = None
    # A partial execution during cancel does not clear the pending cancellation.
    record.status = (
        "unknown"
        if record.unknown_outcome
        else "cancel_pending"
        if record.cancel_command_id and event.status not in TERMINAL
        else event.status
    )
    if event.status in TERMINAL:
        record.terminal_bar = candidate.bar_index
    candidate.events[event.event_id] = event
    if event.trade_id:
        candidate.trade_ids[event.trade_id] = event.event_id
    candidate.event_sequence = event.source_sequence
    candidate.trace.append({"kind": "execution", "event": event.model_dump(mode="json")})
    return candidate


def paper_reconciliation(state):
    """Complete deterministic simulator facts; never venue reconciliation proof."""
    return SpotReconciliationV1(
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol=state.binding.symbol,
        revision=state.reconciliation_revision + 1,
        complete=True,
        source_sequence=state.event_sequence,
        quote_cash=state.account.quote_cash,
        net_base=state.account.net_base,
        orders={
            key: {
                "generation": o.intent.generation,
                "status": o.status,
                "cumulative_quantity": decimal_text(o.filled_quantity),
                "cumulative_quote": decimal_text(o.filled_quote),
                "reserved_quote": decimal_text(o.reserved_quote),
                "reserved_base": decimal_text(o.reserved_base),
            }
            for key, o in state.orders.items()
        },
        working_order_ids=tuple(key for key, o in state.orders.items() if o.status not in TERMINAL),
    )


@exact
def reconcile_spot_state(state, report):
    report = SpotReconciliationV1.model_validate(report)
    attributed(state, report)
    if not report.complete or report.revision <= state.reconciliation_revision or state.quarantine:
        raise ValueError("complete newer reconciliation required; unresolved quarantine")
    candidate = state.model_copy(deep=True)
    buffered = {**candidate.buffered_events}
    candidate.buffered_events = {}
    for event in sorted((*report.events, *buffered.values()), key=lambda e: e.source_sequence):
        candidate = apply_spot_event(candidate, event)
    for valuation in report.fee_valuations:
        candidate = apply_spot_fee_valuation(candidate, valuation)
    if (
        candidate.buffered_events
        or report.source_sequence != candidate.event_sequence
        or report.quote_cash != candidate.account.quote_cash
        or report.net_base != candidate.account.net_base
        or set(report.orders) != set(candidate.orders)
    ):
        raise ValueError("Spot reconciliation account/order mismatch")
    if len(set(report.working_order_ids)) != len(report.working_order_ids):
        raise ValueError("duplicate reconciled order identity")
    expected = paper_reconciliation(candidate)
    if report.orders != expected.orders or set(report.working_order_ids) != set(
        expected.working_order_ids
    ):
        raise ValueError("Spot reconciliation order/reservation mismatch")
    for order in candidate.orders.values():
        if order.unknown_outcome is not None:
            raise ValueError("unknown submit/cancel outcome remains unreconciled")
        if order.status in TERMINAL:
            order.reserved_quote = ZERO
            order.reserved_base = ZERO
            order.allocations = {}
            order.terminal_reconciled = True
        elif order.status == "unknown":
            raise ValueError("unknown intent outcome remains unreconciled")
    candidate.reconciliation_required = False
    if not any(
        fee["quote_value"] is None and Decimal(fee["quantity"])
        for fee in candidate.account.fee_records.values()
    ):
        candidate.fee_reconciliation_required = False
        if candidate.controller.refusal_reason == "unvalued_fee":
            candidate.controller.refusal_reason = None
    candidate.reconciliation_revision = report.revision
    candidate.trace.append({"kind": "reconciliation", "revision": report.revision})
    return candidate
