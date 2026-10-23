"""DCA decisions use actual executions and protect the first partial fill."""

from decimal import Decimal

from tests.engine.test_spot_multi_contract import api, event, session


def observe(state, kind, payload):
    return (
        api()
        .advance_spot(
            state,
            api().SpotObservationV1(
                observation_id=f"o{state.observation_sequence + 1}",
                sequence=state.observation_sequence + 1,
                timestamp_ms=state.observation_sequence + 1,
                kind=kind,
                payload=payload,
            ),
        )
        .state
    )


def closed(state, price, index=0, start=True):
    return observe(state, "closed_bar", {"bar_index": index, "close": price, "start": start})


def consume(state, client, qty, price, status="filled"):
    fill = event(state, client, qty=qty, price=price, status=status, seq=state.event_sequence + 1)
    state = observe(state, "executions", {"events": [fill.model_dump(mode="json")]})
    return state


def reconcile(state):
    return observe(
        state, "reconciliation", api().paper_reconciliation(state).model_dump(mode="json")
    )


def find(state, purpose):
    return [o for o in state.orders.values() if o.intent.purpose == purpose]


def test_first_partial_base_fill_has_stop_before_base_completion():
    state = closed(session("dca"), "100")
    base = find(state, "base")[0]
    state = consume(state, base.intent.client_order_id, "0.4", "100", "partial")
    assert state.controller.stop == 70
    assert state.account.net_base == Decimal("0.4")
    assert find(state, "addition") == []
    state = observe(state, "price", {"price": "69", "bar_index": 1, "phase": "descending"})
    assert state.controller.terminal_action == "stop_and_flatten"
    assert find(state, "base")[0].status == "cancel_pending"
    assert find(state, "flatten") == []
    assert find(state, "addition") == []


def test_dca_anchor_and_ladder_wait_for_actual_terminal_vwap():
    state = closed(session("dca"), "99")
    base = find(state, "base")[0]
    state = consume(state, base.intent.client_order_id, "1", "100")
    assert state.controller.stop == 70
    assert find(state, "addition") == []
    state = reconcile(state)
    assert state.controller.anchor == 100
    addition = find(state, "addition")[0]
    assert addition.intent.limit_price == 80
    assert addition.intent.quantity == Decimal("1.25")
    assert state.controller.desired_target == 110


def test_target_replacement_waits_for_old_reserved_base_cancellation():
    state = closed(session("dca"), "100")
    state = consume(state, find(state, "base")[0].intent.client_order_id, "1", "100")
    state = reconcile(state)
    old = find(state, "target")[0]
    state = consume(state, find(state, "addition")[0].intent.client_order_id, "1.25", "80")
    assert state.controller.desired_target < 100
    assert state.orders[old.intent.client_order_id].status == "cancel_pending"
    assert len(find(state, "target")) == 1
    assert state.account.net_base == Decimal("2.25")
    canceled = event(
        state, old.intent.client_order_id, status="canceled", seq=state.event_sequence + 1
    )
    state = observe(state, "executions", {"events": [canceled.model_dump(mode="json")]})
    assert len(find(state, "target")) == 1
    state = reconcile(state)
    assert len(find(state, "target")) == 2
    new = find(state, "target")[-1]
    assert new.intent.quantity == Decimal("2.25")
    assert new.intent.generation == 1


def test_desired_target_cannot_trigger_before_its_order_activates():
    state = closed(session("dca"), "100")
    state = consume(state, find(state, "base")[0].intent.client_order_id, "1", "100")
    state = observe(state, "price", {"price": "120", "bar_index": 1, "phase": "ascending"})
    assert state.controller.terminal_action is None
