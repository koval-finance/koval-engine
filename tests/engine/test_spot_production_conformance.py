"""Independent cash, race, identity and persistence oracles, without sockets."""

from dataclasses import replace
from decimal import Decimal

import pytest

from koval.engine.live_engine import LiveEngine
from koval.engine.spot_contract import SpotFee
from tests.engine.test_spot_production import IDENTITY, ProductionBroker, config, live, spot_graph
from tests.engine.test_spot_production_controls import acquired, event, protected, submit


@pytest.mark.parametrize("split", [False, True])
def test_independent_decimal_oracle_base_buy_fee_and_quote_sale_fee(split):
    engine, broker, _ = live(capital=10)
    submit(engine, broker, quantity="0.05")
    fills = (
        [
            event(
                broker, "b1", quantity="0.02", status="partial", fees=(SpotFee("BTC", "0.00002"),)
            ),
            event(broker, "b2", quantity="0.03", fees=(SpotFee("BTC", "0.00003"),)),
        ]
        if split
        else [event(broker, "b1", quantity="0.05", fees=(SpotFee("BTC", "0.00005"),))]
    )
    broker.events = fills + fills
    broker.last_report = replace(
        broker.last_report,
        quote_balance="5",
        base_quantity="0.04995",
        terminal_order_ids=("entry",),
        fees_complete=True,
        base_free="0.04995",
    )
    engine.observe_spot()
    snap = engine.spot_account
    for name, expected in {
        "quote_cash": "5",
        "base_quantity": "0.04995",
        "inventory_cost": "4.995",
        "valued_fees": "0.005",
    }.items():
        assert Decimal(snap[name]) == Decimal(expected)
    original = broker.submit_exit

    def sell(intent):
        ack = original(intent)
        sold = event(
            broker,
            "sale",
            quantity="0.04995",
            price="110",
            role="flatten",
            client_id=intent.client_order_id,
            fees=(SpotFee("USDT", "0.0054945"),),
        )
        broker.events = [sold, sold]
        broker.last_report = replace(
            broker.last_report,
            quote_balance="10.4890055",
            base_quantity="0",
            base_free="0",
            terminal_order_ids=("entry", intent.client_order_id),
        )
        return ack

    broker.submit_exit = sell
    engine.close_spot()
    snap = engine.spot_account
    assert Decimal(snap["quote_cash"]) == Decimal("10.4890055")
    assert Decimal(snap["base_quantity"]) == 0
    assert Decimal(snap["net_realized_pnl"]) == Decimal("0.4890055")
    assert snap["flat_confirmed"]


@pytest.mark.parametrize(
    "asset,fee,cash,base,complete",
    [
        ("USDT", "0.005", "4.995", "0.05", True),
        ("BNB", "0.001", "5", "0.05", False),
    ],
)
def test_independent_quote_and_unvalued_fee_oracles(asset, fee, cash, base, complete):
    engine, broker, _ = live(capital=10)
    submit(engine, broker, quantity="0.05")
    broker.events = [event(broker, quantity="0.05", fees=(SpotFee(asset, fee),))]
    broker.last_report = replace(
        broker.last_report,
        quote_balance=cash,
        base_quantity=base,
        terminal_order_ids=("entry",),
        fees_complete=True,
        base_free=base,
    )
    engine.observe_spot()
    snapshot = engine.spot_account
    assert Decimal(snapshot["quote_cash"]) == Decimal(cash)
    assert Decimal(snapshot["base_quantity"]) == Decimal(base)
    assert snapshot["valuation_complete"] is complete
    assert Decimal(snapshot["fees_by_asset"][asset]) == Decimal(fee)
    if not complete:
        assert snapshot["net_realized_pnl"] is None
        assert snapshot["entries_halted"]


def test_production_generated_ids_are_bound_to_account_and_replay_stable():
    def intent_for(identity):
        broker = ProductionBroker()
        broker.spot_identity = identity
        engine = LiveEngine(
            spot_graph(),
            replace(config(), spot_identity=identity),
            broker=broker,
            session_id="same-session",
            on_record=lambda r: None,
        )
        # A real evaluator decision through the engine generates its entry identity.
        broker.context = engine.spot_state["execution"]["context"]
        broker.last_report = replace(
            broker.last_report, identity=identity, session_id="same-session", context=broker.context
        )
        engine.reconcile_spot()
        engine._strategy.should_long = lambda: True
        from koval.strategy.base.trade_setup import TradeSetup

        engine._strategy.go_long = lambda: TradeSetup("long", 100, 90, 120, 1, "market")
        broker.last_report = replace(
            broker.last_report, pending_entry_ids=(), terminal_order_ids=()
        )
        original = broker.submit_entry

        def send(intent):
            result = original(intent)
            broker.last_report = replace(
                broker.last_report,
                open_order_ids=(intent.client_order_id,),
                pending_entry_ids=(intent.client_order_id,),
            )
            return result

        broker.submit_entry = send
        engine._process_bar([0, 100, 101, 99, 100, 10])
        return next(c[1] for c in broker.calls if c[0] == "entry")

    first, replay = intent_for(IDENTITY), intent_for(IDENTITY)
    foreign = intent_for(replace(IDENTITY, account_id="other"))
    assert first.client_order_id == replay.client_order_id
    assert first.client_order_id != foreign.client_order_id


def test_unknown_protection_request_cannot_cancel_or_sell_until_original_ids_resolved():
    engine, broker, _ = acquired()
    broker.protection_response = TimeoutError("lost OCO response")
    before = len(broker.calls)
    with pytest.raises((RuntimeError, TimeoutError)):
        engine.supervise_spot()
    assert len([c for c in broker.calls[before:] if c[0] == "protect"]) == 1
    assert not [c for c in broker.calls[before:] if c[0] in {"cancel_all", "exit"}]


def test_observation_cannot_confirm_flat_with_unresolved_entry_or_missing_fees():
    engine, broker, _ = live()
    submit(engine, broker)
    # Omitted order with no terminal proof is not established flatness.
    engine.observe_spot()
    assert not engine.spot_account["flat_confirmed"]
    assert engine.spot_account["entries_halted"]


def test_partial_buy_terminal_cancel_and_late_fill_protects_once():
    engine, broker, _ = live()
    submit(engine, broker)
    broker.events = [event(broker, "p", quantity="0.5", status="partial")]
    partial_report = replace(
        broker.last_report,
        quote_balance="950",
        base_quantity="0.5",
        open_order_ids=("entry",),
        pending_entry_ids=("entry",),
        fees_complete=True,
    )
    final = replace(
        broker.last_report,
        quote_balance="900",
        base_quantity="1",
        base_free="1",
        terminal_order_ids=("entry",),
        fees_complete=True,
        fills=(event(broker, "race", quantity="0.5", status="canceled"),),
    )
    broker.reports = [partial_report, final]
    from tests.engine.spot_fixtures import protection

    broker.protection_response = lambda intent: replace(protection(intent), context=broker.context)
    engine.supervise_spot()
    assert engine.spot_account["base_quantity"] == "1.0"
    assert engine.spot_account["entries_halted"]
    assert len([c for c in broker.calls if c[0] == "entry"]) == 1
    assert len([c for c in broker.calls if c[0] == "cancel_entry"]) == 1
    assert len([c for c in broker.calls if c[0] == "protect"]) == 1


def test_fill_callback_failure_precedes_account_mutation_and_latches_storage():
    engine, broker, records = live()
    submit(engine, broker)
    engine._on_fill = engine._record_callback(
        "fill", lambda payload: (_ for _ in ()).throw(OSError("callback"))
    )
    broker.events = [event(broker)]
    with pytest.raises(OSError):
        engine.observe_spot()
    assert engine.spot_account["base_quantity"] == "0"
    assert any(r["kind"] == "fill" for r in records)
    before = list(broker.calls)
    with pytest.raises(RuntimeError, match="storage"):
        engine.supervise_spot()
    assert broker.calls == before


@pytest.mark.parametrize(
    "node", ["value.constant", "condition.hold", "state.position", "recipe.grid"]
)
def test_production_numeric_state_recipe_graphs_fail_without_broker_calls(node):
    from koval.engine.spot_contract import validate_spot_selection

    graph = spot_graph()
    graph["blocks"].append({"id": "extra", "type": node, "params": {}})
    broker = ProductionBroker()
    with pytest.raises(ValueError, match="supports"):
        validate_spot_selection(broker, config(), graph, archive_enabled=True)
    assert not broker.calls


def test_reconcile_cannot_rearm_an_unknown_dispatched_entry():
    engine, broker, _ = live()
    submit(engine, broker)
    with pytest.raises(RuntimeError, match="unknown"):
        engine.reconcile_spot()
    assert engine.spot_account["entries_halted"]
    assert not engine.spot_account["flat_confirmed"]


@pytest.mark.parametrize(
    "node", ["source.value", "fact.condition_edge", "fact.compare", "policy.from_condition"]
)
def test_public_selection_rejects_real_historical_numeric_nodes(node):
    from koval.engine.spot_contract import validate_spot_selection
    from koval.strategy.graph.compat import compile_legacy

    graph = compile_legacy(spot_graph())
    graph["blocks"].append({"id": "extra", "type": node, "params": {}})
    with pytest.raises(ValueError, match="supports"):
        validate_spot_selection(ProductionBroker(), config(), graph, archive_enabled=True)


def test_partial_triggered_exit_halts_additions_and_preserves_exact_oco():
    engine, broker, _ = protected()
    p = engine._spot.protection
    # This coordinator-level setup bypasses observation's conservative halt, to
    # prove the exit delta itself latches the entry gate.
    engine._spot.halted = False
    engine._spot.ready = True
    broker.events = [
        event(
            broker,
            "partial-target",
            quantity="0.5",
            status="partial",
            role="target",
            client_id=p.target_client_order_id,
            price="110",
        )
    ]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="855",
        base_quantity="1.5",
        protection=replace(p, quantity="1.5"),
    )
    engine.supervise_spot()
    assert engine.spot_account["entries_halted"]
    assert Decimal(engine.spot_account["base_quantity"]) == Decimal("1.5")
    assert engine.spot_account["protection"]["quantity"] == "1.5"


def test_close_requires_terminal_proof_after_cancellation_before_sell():
    engine, broker, _ = protected()
    original = broker.cancel_all

    def cancel(symbol, session_id):
        result = original(symbol, session_id)
        broker.last_report = replace(
            broker.last_report,
            open_order_ids=(),
            protection=None,
            unknown_request_ids=("unresolved-cancel",),
            terminal_order_ids=("entry",),
        )
        return result

    broker.cancel_all = cancel
    with pytest.raises(RuntimeError, match="unconfirmed"):
        engine.close_spot()
    assert not [c for c in broker.calls if c[0] == "exit"]


def test_partial_cancel_unknown_cannot_place_protection():
    engine, broker, _ = live()
    submit(engine, broker)
    broker.events = [event(broker, "p", quantity="0.5", status="partial")]
    first = replace(
        broker.last_report,
        quote_balance="950",
        base_quantity="0.5",
        open_order_ids=("entry",),
        pending_entry_ids=("entry",),
        fees_complete=True,
    )
    second = replace(
        first,
        open_order_ids=(),
        pending_entry_ids=(),
        terminal_order_ids=("entry",),
        unknown_request_ids=("cancel-outcome-unknown",),
    )
    broker.reports = [first, second]
    with pytest.raises(RuntimeError, match="unknown"):
        engine.supervise_spot()
    assert not [c for c in broker.calls if c[0] in {"protect", "exit"}]
    assert engine.spot_account["uncertain"]


@pytest.mark.parametrize("partial", [False, True])
def test_production_oco_placement_race_consumes_actual_fills_without_extra_sell(partial):
    from tests.engine.spot_fixtures import protection

    engine, broker, _ = acquired()
    original = broker.place_spot_protection
    broker.protection_response = lambda intent: replace(protection(intent), context=broker.context)

    def place(intent):
        ack = original(intent)
        sold = event(
            broker,
            "during-placement",
            quantity="0.5" if partial else "2",
            status="partial" if partial else "filled",
            role="target",
            client_id=ack.target_client_order_id,
            price="110",
        )
        broker.last_report = replace(
            broker.last_report,
            quote_balance="855" if partial else "1020",
            base_quantity="1.5" if partial else "0",
            fills=(sold,),
            protection=replace(ack, quantity="1.5") if partial else None,
            open_order_ids=(ack.stop_client_order_id, ack.target_client_order_id)
            if partial
            else (),
            terminal_order_ids=("entry",)
            if partial
            else ("entry", ack.stop_client_order_id, ack.target_client_order_id),
            terminal_list_ids=() if partial else (intent.metadata["list_client_order_id"],),
        )
        return ack

    broker.place_spot_protection = place
    engine.supervise_spot()
    assert Decimal(engine.spot_account["base_quantity"]) == Decimal("1.5" if partial else "0")
    assert engine.spot_account["flat_confirmed"] is (not partial)
    assert not [c for c in broker.calls if c[0] in {"exit", "cancel_all"}]


def test_duplicate_conflict_and_terminal_before_late_buy_never_erase_inventory():
    engine, broker, _ = live()
    submit(engine, broker)
    terminal = event(broker, "terminal", quantity="0", price=None, status="canceled")
    filled = event(broker, "actual", quantity="1", status="canceled")
    broker.events = [terminal, filled, terminal, filled]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="900",
        base_quantity="1",
        terminal_order_ids=("entry",),
        fees_complete=True,
    )
    engine.observe_spot()
    before = engine.spot_account
    broker.events = [replace(filled, price="99")]
    with pytest.raises(ValueError, match="conflicting"):
        engine.observe_spot()
    assert engine.spot_account["quote_cash"] == before["quote_cash"]
    assert engine.spot_account["base_quantity"] == before["base_quantity"]


def test_production_preserves_absolute_bracket_when_actual_fill_moves():
    from tests.engine.spot_fixtures import protection

    engine, broker, _ = live()
    submit(engine, broker)
    broker.events = [event(broker, price="105")]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="790",
        base_quantity="2",
        terminal_order_ids=("entry",),
        fees_complete=True,
    )
    broker.protection_response = lambda intent: replace(protection(intent), context=broker.context)
    engine.supervise_spot()
    assert engine.spot_account["protection"]["stop_price"] == "90"
    assert engine.spot_account["protection"]["target_price"] == "120"
