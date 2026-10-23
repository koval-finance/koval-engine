"""Pure bounded DCA/Grid decisions. Hosts reduce their typed intents identically."""

from decimal import Decimal

from koval.engine.spot_numbers import ceil_step, decimal_text, exact, floor_step
from koval.strategy.schemas import SpotDcaParams, SpotGridParams

from .account import free_base, free_quote
from .models import (
    TERMINAL,
    ZERO,
    SpotControllerStateV1,
    SpotDecisionV1,
    SpotOrderIntentV1,
    spot_content_hash,
)
from .state import grid_points


def owned(state, purpose=None):
    return [
        o
        for o in state.orders.values()
        if (purpose is None or o.intent.purpose == purpose)
        and o.intent.cycle_id == f"c{state.controller.cycle}"
    ]


def new_intent(
    state, purpose, slot, *, price=None, quantity=None, quote=None, parent=None, activation=None
):
    generation = (
        max(
            (o.intent.generation for o in owned(state, purpose) if o.intent.slot_id == slot),
            default=-1,
        )
        + 1
    )
    identity = spot_content_hash(
        [
            state.binding.account_id,
            state.binding.session_id,
            state.controller.cycle,
            slot,
            purpose,
            generation,
        ]
    )[:32]
    key = f"sm1-{identity}"
    return SpotOrderIntentV1(
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol=state.binding.symbol,
        intent_id=key,
        client_order_id=key,
        cycle_id=f"c{state.controller.cycle}",
        slot_id=slot,
        generation=generation,
        purpose=purpose,
        side="buy" if purpose in {"base", "addition", "grid_buy"} else "sell",
        order_type="limit" if price is not None else "market",
        quantity=quantity,
        quote_notional_cap=quote,
        limit_price=price,
        requested_quantity=Decimal(state.controller.slots[slot]["requested_quantity"])
        if purpose == "grid_buy"
        else quantity,
        requested_price=price,
        parent_order_id=parent,
        terminal_command_id=state.controller.terminal_command_id if purpose == "flatten" else None,
        activation_bar=state.bar_index + 1 if activation is None else activation,
        expires_after_bars=state.policy.expires_after_bars,
    )


def submit(decisions, intent):
    decisions.append({"kind": "submit", "intent": intent.model_dump(mode="json")})


def cancel(decisions, order, reason):
    if (
        not order.terminal_reconciled
        and order.status not in TERMINAL
        and not order.cancel_command_id
    ):
        decisions.append(
            {
                "kind": "cancel",
                "client_order_id": order.intent.client_order_id,
                "generation": order.intent.generation,
                "command_id": f"cancel-{spot_content_hash([order.intent.client_order_id, reason])[:32]}",
            }
        )


@exact
def sell_quantity(state, quantity=None, market=False):
    available = free_base(state) if quantity is None else quantity
    step = state.instrument.market_quantity_step if market else state.instrument.quantity_step
    quantity = available / (1 + state.policy.sell_base_fee_cap_bps / 10000)
    if market:
        quantity = min(
            quantity, state.instrument.market_maximum_quantity or state.instrument.maximum_quantity
        )
    return floor_step(quantity, step)


def trigger_terminal(state, action, reason, command_id=None):
    c = state.controller
    if reason == "target" and c.terminal_action is not None and c.terminal_reason != "target":
        return
    if c.terminal_action == "stop_and_flatten" and action == "stop_and_hold":
        return
    c.terminal_action = action
    c.terminal_reason = reason
    c.terminal_command_id = command_id or f"terminal-{c.cycle}-{reason}"
    c.phase = "stopping"


@exact
def terminal_decisions(state, decisions):
    c = state.controller
    if state.reconciliation_required or state.quarantine:
        c.phase = "reconciling"
        return
    predecessors = [o for o in state.orders.values() if o.intent.purpose != "flatten"]
    for order in predecessors:
        cancel(decisions, order, c.terminal_command_id)
    if any(not o.terminal_reconciled for o in predecessors):
        return
    state.account.cycle_reserve = ZERO
    state.account.cleanup_reserve = ZERO
    if c.terminal_action == "stop_and_hold":
        c.phase = "stopped_holding"
        return
    closes = owned(state, "flatten")
    if any(not o.terminal_reconciled for o in closes):
        return
    if not state.account.net_base:
        was_complete = c.phase == "complete"
        c.phase = "complete" if c.terminal_reason in {"target", "empty_base"} else "stopped_flat"
        if not was_complete:
            c.last_cycle_bar = state.bar_index
        return
    if (
        closes
        and closes[-1].status != "filled"
        and closes[-1].intent.terminal_command_id == c.terminal_command_id
    ):
        c.phase = "containment"
        return
    quantity = sell_quantity(state, market=True)
    minimum = state.instrument.market_minimum_quantity or state.instrument.minimum_quantity
    if (
        quantity < minimum
        or state.mark is None
        or state.instrument.apply_notional_to_market
        and quantity * state.mark < state.instrument.minimum_notional
    ):
        c.phase = "stopped_with_dust"
        return
    # Only the first protective close can use the unconsumed trigger reference.
    activation = (
        state.bar_index if c.terminal_reason == "stop" and not closes else state.bar_index + 1
    )
    if closes and c.last_close_observation >= state.observation_sequence:
        return
    submit(
        decisions, new_intent(state, "flatten", "basket", quantity=quantity, activation=activation)
    )
    c.last_close_observation = state.observation_sequence
    c.close_generation += 1


def common_safety(state, kind, payload, decisions):
    c = state.controller
    if (
        kind == "price"
        and c.stop is not None
        and state.account.net_base
        and state.mark <= c.stop
        and c.phase != "stopped_holding"
    ):
        trigger_terminal(state, "stop_and_flatten", "stop")
    if kind == "executions":
        if any(
            e.get("purpose") == "target" and Decimal(e.get("quantity", "0")) > 0
            for e in payload["events"]
        ):
            trigger_terminal(state, "stop_and_flatten", "target")
    if kind == "price" and not c.terminal_action:
        for order in owned(state, "target"):
            if (
                order.status not in TERMINAL
                and order.intent.activation_bar <= state.bar_index
                and order.status != "pending"
                and state.mark >= order.intent.limit_price
            ):
                trigger_terminal(state, "stop_and_flatten", "target")
                break
    if c.terminal_action:
        terminal_decisions(state, decisions)
        return True
    return bool(state.reconciliation_required or state.quarantine)


@exact
def _target(state, decisions):
    c = state.controller
    if not state.account.net_base:
        return
    quantity = sell_quantity(state, state.account.net_base)
    rules = state.instrument
    invalid = (
        c.desired_target is None
        or not rules.minimum_price <= c.desired_target <= rules.maximum_price
        or quantity > rules.maximum_quantity
        or quantity * c.desired_target > rules.maximum_notional
        or rules.price_band_low is not None
        and not rules.price_band_low <= c.desired_target <= rules.price_band_high
    )
    if invalid:
        c.refusal_reason = "target_constraints"
        decisions[:] = [d for d in decisions if d["kind"] != "submit"]
        trigger_terminal(state, "stop_and_flatten", "target_constraints")
        terminal_decisions(state, decisions)
        return
    if (
        quantity < state.instrument.minimum_quantity
        or quantity * c.desired_target < state.instrument.minimum_notional
    ):
        c.refusal_reason = "target_dust"
        return
    targets = owned(state, "target")
    working = [o for o in targets if not o.terminal_reconciled]
    for order in working:
        remaining = order.intent.quantity - order.filled_quantity
        if order.intent.limit_price != c.desired_target or remaining != quantity:
            cancel(decisions, order, "target_replace")
    if working:
        return
    submit(
        decisions, new_intent(state, "target", "basket", price=c.desired_target, quantity=quantity)
    )


@exact
def _dca(state, kind, payload, decisions):
    c = state.controller
    p = SpotDcaParams.model_validate(state.recipe.params)
    if (
        c.phase in {"idle", "complete"}
        and kind == "closed_bar"
        and payload.get("start")
        and state.bar_index > c.last_cycle_bar
    ):
        maximum = (p.base_quote + p.max_additions * p.addition_quote) * (
            1 + state.policy.buy_quote_fee_cap_bps / 10000
        )
        if maximum + p.fee_reserve_quote > state.account.allocation_cash:
            c.refusal_reason = "insufficient_cycle_cash"
            return
        cycle = c.cycle + 1
        state.controller = c = SpotControllerStateV1(
            cycle=cycle,
            phase="base_pending",
            last_cycle_bar=c.last_cycle_bar,
            cycle_base_start=state.account.bought_base,
            cycle_quote_start=state.account.bought_quote,
        )
        state.account.cleanup_reserve = p.fee_reserve_quote
        state.account.cycle_reserve = maximum
        submit(decisions, new_intent(state, "base", "base", quote=p.base_quote))
        return
    bases = owned(state, "base")
    if bases and bases[0].filled_quantity:
        base = bases[0]
        first = next(
            e
            for e in state.events.values()
            if e.client_order_id == base.intent.client_order_id and e.quantity
        )
        vwap = base.filled_quote / base.filled_quantity
        stop = ceil_step(
            max(first.price, vwap) * (1 - p.stop_loss_pct / 100), state.instrument.price_tick
        )
        if c.stop is None or stop > c.stop:
            c.stop = stop
            decisions.append(
                {
                    "kind": "protection",
                    "stop_price": decimal_text(stop),
                    "reason": "actual_base_fill",
                }
            )
        gross_base = state.account.bought_base - c.cycle_base_start
        gross_quote = state.account.bought_quote - c.cycle_quote_start
        c.desired_target = ceil_step(
            gross_quote / gross_base * (1 + p.take_profit_pct / 100), state.instrument.price_tick
        )
        if state.mark is not None and state.mark <= c.stop:
            trigger_terminal(state, "stop_and_flatten", "stop")
            terminal_decisions(state, decisions)
            return
        if base.terminal_reconciled and c.anchor is None:
            c.anchor = vwap
            levels = [
                floor_step(vwap * (1 - i * p.deviation_pct / 100), state.instrument.price_tick)
                for i in range(1, p.max_additions + 1)
            ]
            if len(set(levels)) != len(levels) or any(level <= c.stop for level in levels):
                c.refusal_reason = "normalized_additions_invalid"
            else:
                for i, level in enumerate(levels, 1):
                    c.slots[str(i)] = {"price": decimal_text(level)}
                c.phase = "active"
        if not c.refusal_reason and not owned(state, "addition") and c.slots:
            for slot, details in c.slots.items():
                price = Decimal(details["price"])
                quantity = floor_step(p.addition_quote / price, state.instrument.quantity_step)
                submit(
                    decisions, new_intent(state, "addition", slot, price=price, quantity=quantity)
                )
        _target(state, decisions)
    elif bases and bases[0].terminal_reconciled:
        trigger_terminal(state, "stop_and_flatten", "empty_base")
        terminal_decisions(state, decisions)


@exact
def _grid(state, kind, payload, decisions):
    c = state.controller
    p = SpotGridParams.model_validate(state.recipe.params)
    if c.phase == "idle" and kind == "closed_bar" and payload.get("start"):
        points = grid_points(p, state.instrument)
        if not points[0] <= state.mark <= points[-1]:
            raise ValueError("Grid activation must lie within its range")
        selected = [i for i, price in enumerate(points[:-1]) if price < state.mark]
        if not selected:
            raise ValueError("Grid activation has no strictly lower bids")
        quantity = floor_step(p.quantity_per_slot, state.instrument.quantity_step)
        required = sum(
            (
                quantity * points[i] * (1 + state.policy.buy_quote_fee_cap_bps / 10000)
                for i in selected
            ),
            ZERO,
        )
        if required > free_quote(state):
            raise ValueError("insufficient all-in Grid cash/fee budget")
        c.cycle += 1
        c.phase = "active"
        c.stop = ceil_step(p.stop_price, state.instrument.price_tick)
        if c.stop >= points[0]:
            raise ValueError("normalized stop overlaps Grid levels")
        for i in selected:
            slot = str(i)
            c.slots[slot] = {
                "bid": decimal_text(points[i]),
                "ask": decimal_text(points[i + 1]),
                "quantity": decimal_text(quantity),
                "requested_quantity": decimal_text(p.quantity_per_slot),
                "paused": None,
            }
            submit(
                decisions, new_intent(state, "grid_buy", slot, price=points[i], quantity=quantity)
            )
        return
    if c.phase != "active":
        return
    for slot, details in c.slots.items():
        buys = [o for o in owned(state, "grid_buy") if o.intent.slot_id == slot]
        if not buys:
            continue
        buy = buys[-1]
        if not buy.terminal_reconciled:
            continue
        sells = [
            o
            for o in owned(state, "grid_sell")
            if o.intent.parent_order_id == buy.intent.client_order_id
        ]
        if any(not o.terminal_reconciled for o in sells):
            continue
        lot = state.account.lots.get(buy.intent.client_order_id)
        remaining = lot.quantity if lot else ZERO
        quantity = sell_quantity(state, remaining)
        if (
            quantity >= state.instrument.minimum_quantity
            and quantity * Decimal(details["ask"]) >= state.instrument.minimum_notional
        ):
            submit(
                decisions,
                new_intent(
                    state,
                    "grid_sell",
                    slot,
                    price=Decimal(details["ask"]),
                    quantity=quantity,
                    parent=buy.intent.client_order_id,
                ),
            )
            continue
        last_terminal = max(
            [o.terminal_bar for o in [buy, *sells] if o.terminal_bar is not None],
            default=state.bar_index,
        )
        if kind != "closed_bar" or state.bar_index <= last_terminal:
            continue
        required = (
            Decimal(details["quantity"])
            * Decimal(details["bid"])
            * (1 + state.policy.buy_quote_fee_cap_bps / 10000)
        )
        if state.mark > p.upper_price or free_quote(state) < required:
            details["paused"] = (
                "outside_upper" if state.mark > p.upper_price else "insufficient_cash"
            )
            continue
        details["paused"] = None
        submit(
            decisions,
            new_intent(
                state,
                "grid_buy",
                slot,
                price=Decimal(details["bid"]),
                quantity=Decimal(details["quantity"]),
            ),
        )


@exact
def _decide(controller_state, facts, kind):
    state = facts["state"].model_copy(deep=True)
    state.controller = controller_state.model_copy(deep=True)
    decisions = []
    observation_kind = facts["kind"]
    payload = facts["payload"]
    # A completed target cycle may restart only at a later explicit start.
    restart = (
        state.controller.phase == "complete"
        and observation_kind == "closed_bar"
        and payload.get("start")
        and state.bar_index > state.controller.last_cycle_bar
    )
    if restart:
        state.controller.terminal_action = None
    if not common_safety(state, observation_kind, payload, decisions):
        if kind == "dca":
            _dca(state, observation_kind, payload, decisions)
        else:
            _grid(state, observation_kind, payload, decisions)
    return SpotDecisionV1(
        intents=decisions,
        controller=state.controller,
        cycle_reserve=state.account.cycle_reserve,
        cleanup_reserve=state.account.cleanup_reserve,
    )


def dca_decide(controller_state, facts):
    return _decide(controller_state, facts, "dca")


def grid_decide(controller_state, facts):
    return _decide(controller_state, facts, "grid")
