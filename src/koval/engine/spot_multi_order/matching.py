"""Pure price/quantity/cost arithmetic for the versioned causal OHLC policy."""

from koval.engine.spot_numbers import ceil_step, exact, floor_step

from .models import TERMINAL, SpotExecutionEventV1, SpotFeeV1


def eligible(state, order):
    return (
        order.status not in TERMINAL
        and order.status != "unknown"
        and order.intent.activation_bar <= state.bar_index
    )


@exact
def execution_price(state, order, reference):
    fraction = (state.policy.spread_bps / 2 + state.policy.slippage_bps) / 10000
    price = reference * (1 + fraction if order.intent.side == "buy" else 1 - fraction)
    price = (
        ceil_step(price, state.instrument.price_tick)
        if order.intent.side == "buy"
        else floor_step(price, state.instrument.price_tick)
    )
    limit = order.intent.limit_price
    if limit is not None:
        price = min(price, limit) if order.intent.side == "buy" else max(price, limit)
    return price


@exact
def modeled_event(state, order, reference, *, at_open, timestamp_ms):
    price = execution_price(state, order, reference)
    rules = state.instrument
    if (
        not rules.minimum_price <= price <= rules.maximum_price
        or rules.price_band_low is not None
        and not rules.price_band_low <= price <= rules.price_band_high
    ):
        return status_event(state, order, "rejected", timestamp_ms)
    step = (
        rules.market_quantity_step if order.intent.order_type == "market" else rules.quantity_step
    )
    requested = (
        (order.intent.quote_notional_cap - order.filled_quote) / price
        if order.intent.quote_notional_cap is not None
        else order.intent.quantity - order.filled_quantity
    )
    requested = floor_step(requested, step)
    if (
        order.intent.order_type == "market"
        and not order.filled_quantity
        and (
            requested < (rules.market_minimum_quantity or rules.minimum_quantity)
            or requested > (rules.market_maximum_quantity or rules.maximum_quantity)
            or rules.apply_notional_to_market
            and not rules.minimum_notional <= requested * price <= rules.maximum_notional
        )
    ):
        return status_event(state, order, "rejected", timestamp_ms)
    quantity = floor_step(min(requested, state.liquidity_allowance - state.liquidity_used), step)
    if quantity <= 0:
        return None
    notional = quantity * price
    taker = order.intent.order_type == "market" or at_open
    rate = state.policy.taker_fee_bps if taker else state.policy.maker_fee_bps
    fee_notional = notional * rate / 10000
    asset = state.policy.fee_asset
    fee_qty = (
        fee_notional / price
        if asset == state.binding.base_asset
        else fee_notional
        if asset == state.binding.quote_asset
        else fee_notional / state.policy.third_asset_quote_price
    )
    fees = (
        ()
        if not fee_qty
        else (
            SpotFeeV1(
                asset=asset,
                quantity=fee_qty,
                quote_value=fee_notional
                if asset not in {state.binding.base_asset, state.binding.quote_asset}
                else None,
                source=state.policy.fee_source,
            ),
        )
    )
    full = quantity == requested
    if order.intent.quote_notional_cap is not None:
        full = (
            floor_step(
                (order.intent.quote_notional_cap - order.filled_quote - notional) / price, step
            )
            == 0
        )
    return SpotExecutionEventV1(
        event_id=f"paper-e{state.event_sequence + 1}",
        trade_id=f"paper-t{state.event_sequence + 1}",
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol=state.binding.symbol,
        client_order_id=order.intent.client_order_id,
        exchange_order_id=f"paper-{order.intent.client_order_id}",
        generation=order.intent.generation,
        source_sequence=state.event_sequence + 1,
        timestamp_ms=timestamp_ms,
        side=order.intent.side,
        purpose=order.intent.purpose,
        status="filled" if full else "partial",
        quantity=quantity,
        price=price,
        quote_notional=notional,
        cumulative_quantity=order.filled_quantity + quantity,
        cumulative_quote=order.filled_quote + notional,
        fees=fees,
    )


def status_event(state, order, status, timestamp_ms):
    return SpotExecutionEventV1(
        event_id=f"paper-e{state.event_sequence + 1}",
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol=state.binding.symbol,
        client_order_id=order.intent.client_order_id,
        exchange_order_id=f"paper-{order.intent.client_order_id}",
        generation=order.intent.generation,
        source_sequence=state.event_sequence + 1,
        timestamp_ms=timestamp_ms,
        side=order.intent.side,
        purpose=order.intent.purpose,
        status=status,
        cumulative_quantity=order.filled_quantity,
        cumulative_quote=order.filled_quote,
    )
