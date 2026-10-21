"""Contract transitions from deterministic host evidence, never acknowledgements alone."""

from dataclasses import replace
from decimal import Decimal

import pytest

from koval.engine.spot_contract import SpotFee
from tests.engine.spot_fixtures import ScriptedSpotBroker, entry, execution, protection, report


def runtime(broker=None, sink=None):
    from koval.engine.spot_execution import SpotExecution

    broker = broker or ScriptedSpotBroker()
    records = []
    engine = SpotExecution(
        broker,
        session_id="session",
        capital="1000",
        binding={"strategy": "hash", "timeframe": "1h"},
        emit=sink or (lambda k, p: records.append((k, p))),
    )
    engine.reconcile()
    return engine, broker, records


def enter(engine, broker, *, fees=()):
    engine.submit(entry())
    quantity = "1.99" if fees else "2"
    broker.events = [execution(fees=fees)]
    broker.reports = [report("800", quantity)]
    engine.poll()
    return engine.protection


def test_entry_requires_preflight_and_never_creates_a_fill_from_ack():
    engine, broker, _ = runtime()
    engine.submit(entry())
    assert engine.ledger.quantity == 0
    assert engine.pending is True
    assert not any(c[0] == "protect" for c in broker.calls)


def test_full_entry_protects_net_inventory_and_emits_confirmed_snapshot():
    engine, broker, records = runtime()
    protected = enter(engine, broker, fees=(SpotFee("BTC", "0.01"),))
    assert Decimal(protected.quantity) == Decimal("1.99")
    assert engine.ledger.account.snapshot().free_margin == pytest.approx(800)
    assert engine.ledger.account.snapshot().margin_used == pytest.approx(199)
    assert engine.pending is False
    assert records[-1][0] == "spot_state"


def test_partial_entry_cancels_reconciles_race_and_protects_final_net_inventory():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution(quantity="0.5", status="partial")]
    broker.reports = [
        report("950", "0.5", pending=("entry",), orders=("entry",)),
        report("900", "1", fills=(execution("race", quantity="0.5"),)),
    ]
    engine.poll()
    kinds = [c[0] for c in broker.calls]
    assert kinds.index("cancel_entry") < kinds.index("protect")
    assert Decimal(engine.protection.quantity) == 1
    assert engine.halted is True
    assert not any(c[0] == "exit" for c in broker.calls)


def test_unconfirmed_partial_cancellation_halts_without_resubmission():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution(quantity="1", status="partial")]
    broker.reports = [report("900", "1", pending=("entry",), orders=("entry",))]
    with pytest.raises(RuntimeError, match="pending"):
        engine.poll()
    assert engine.halted
    assert len([c for c in broker.calls if c[0] == "cancel_entry"]) == 1
    assert not any(c[0] in {"protect", "exit"} for c in broker.calls)


@pytest.mark.parametrize("reverse", [False, True])
def test_oco_sibling_cancellation_is_normal_after_confirmed_exit(reverse):
    engine, broker, _ = runtime()
    p = enter(engine, broker)
    sold = execution("sold", role="target", client_id=p.target_client_order_id, price="110")
    canceled = execution(
        "cancel",
        role="stop",
        client_id=p.stop_client_order_id,
        quantity="0",
        status="canceled",
        price=None,
    )
    broker.events = [sold, canceled] if not reverse else [canceled, sold]
    broker.reports = [report("1020", "0")]
    engine.poll()
    broker.events = [sold, canceled]
    engine.poll()
    assert engine.ledger.cash == 1020
    assert engine.ledger.quantity == 0
    assert engine.flat_confirmed
    assert not any(c[0] == "exit" for c in broker.calls)


def test_partial_exit_preserves_exact_residual_protection():
    engine, broker, _ = runtime()
    p = enter(engine, broker)
    broker.events = [
        execution(
            "partial",
            quantity="0.5",
            role="target",
            status="partial",
            client_id=p.target_client_order_id,
            price="110",
        )
    ]
    remaining = replace(p, quantity="1.5")
    broker.reports = [
        report(
            "855",
            "1.5",
            protected=remaining,
            orders=(p.stop_client_order_id, p.target_client_order_id),
        )
    ]
    engine.poll()
    assert engine.ledger.cost == 150
    assert engine.protection.quantity == "1.5"


def test_dust_stays_visible_and_never_means_flat():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution(fees=(SpotFee("BTC", "0.001"),))]
    broker.reports = [report("800", "1.999")]
    # Broker rounds the protection, never the engine's inventory ledger.
    broker.protection_response = lambda intent: protection(intent, quantity="1.99", dust="0.009")
    engine.poll()
    assert engine.ledger.quantity == Decimal("1.999")
    assert engine.protection.dust_quantity == "0.009"
    assert engine.halted
    assert not engine.flat_confirmed


def test_lost_protection_reconciles_cancellation_before_emergency_sell():
    engine, broker, _ = runtime()
    p = enter(engine, broker)
    broker.events = [
        execution(
            "lost",
            quantity="0",
            price=None,
            role="stop",
            status="canceled",
            client_id=p.stop_client_order_id,
        )
    ]
    broker.reports = [report("800", "2"), report("800", "2"), report("800", "2")]
    with pytest.raises(RuntimeError, match="closure unconfirmed"):
        engine.poll()
    kinds = [c[0] for c in broker.calls]
    assert kinds.index("cancel_all") < kinds.index("exit")
    assert engine.ledger.quantity == 2
    assert engine.flat_confirmed is False
    with pytest.raises(RuntimeError):
        engine.close()
    assert len([c for c in broker.calls if c[0] == "exit"]) == 1


def test_unknown_entry_submission_is_never_blindly_retried():
    engine, broker, _ = runtime()
    broker.entry_error = TimeoutError("uncertain")
    with pytest.raises(TimeoutError):
        engine.submit(entry())
    with pytest.raises(RuntimeError, match="halted"):
        engine.submit(entry())
    assert engine.halted and engine.uncertain
    assert len([c for c in broker.calls if c[0] == "entry"]) == 1


def test_persistence_failure_before_entry_has_no_broker_mutation():
    def sink(kind, payload):
        if kind == "order_intent":
            raise OSError("disk")

    engine, broker, _ = runtime(sink=sink)
    with pytest.raises(OSError):
        engine.submit(entry())
    assert engine.halted
    assert not any(c[0] in {"entry", "exit"} for c in broker.calls)


def test_restart_uses_public_state_and_venue_proof_without_new_entry():
    engine, broker, _ = runtime()
    p = enter(engine, broker)
    state = engine.export_state()
    restored, new_broker, _ = runtime()
    new_broker.reports = [
        report("800", "2", protected=p, orders=(p.stop_client_order_id, p.target_client_order_id))
    ]
    restored.restore(state)
    restored.reconcile()
    assert restored.ledger.quantity == 2
    assert restored.protection == p
    assert not any(c[0] in {"entry", "protect", "exit"} for c in new_broker.calls)
    with pytest.raises(ValueError, match="binding"):
        restored.restore({**state, "binding": {"strategy": "different"}})


def test_protection_timeout_never_repeats_submission_and_can_confirm_emergency_exit():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution()]
    broker.protection_response = TimeoutError("unknown protection")
    broker.reports = [report("800", "2"), report("800", "2")]
    original_exit = broker.submit_exit

    def sell(intent):
        ack = original_exit(intent)
        broker.events = [
            execution(
                "emergency",
                quantity="2",
                price="99",
                role="flatten",
                client_id=intent.client_order_id,
            )
        ]
        broker.reports = [report("998", "0")]
        return ack

    broker.submit_exit = sell
    with pytest.raises(TimeoutError):
        engine.poll()
    assert engine.ledger.cash == 998
    engine.poll()
    assert len([c for c in broker.calls if c[0] == "protect"]) == 1
    assert len([c for c in broker.calls if c[0] == "exit"]) == 1
    assert engine.flat_confirmed


def test_close_cancellation_fill_race_does_not_submit_an_extra_sell():
    engine, broker, _ = runtime()
    p = enter(engine, broker)
    sold = execution("race", role="stop", client_id=p.stop_client_order_id, price="90")
    broker.reports = [report("980", "0", fills=(sold,))]
    engine.close()
    assert engine.flat_confirmed
    assert not any(c[0] == "exit" for c in broker.calls)


def test_partial_emergency_exit_never_reports_flat_or_retries_market_sell():
    engine, broker, _ = runtime()
    enter(engine, broker)
    broker.reports = [report("800", "2")]
    original_exit = broker.submit_exit

    def sell(intent):
        ack = original_exit(intent)
        broker.events = [
            execution(
                "partial-close",
                quantity="1",
                price="99",
                role="flatten",
                status="partial",
                client_id=intent.client_order_id,
            )
        ]
        broker.reports = [report("899", "1", orders=(intent.client_order_id,))]
        return ack

    broker.submit_exit = sell
    with pytest.raises(RuntimeError, match="closure unconfirmed"):
        engine.close()
    assert engine.ledger.quantity == 1
    assert not engine.flat_confirmed
    with pytest.raises(RuntimeError):
        engine.close()
    assert len([c for c in broker.calls if c[0] == "exit"]) == 1


@pytest.mark.parametrize(
    "failed_kind",
    ["order_ack", "fill", "spot_protection_intent", "spot_protection_ack", "spot_state"],
)
def test_callback_failures_halt_without_blind_mutations(failed_kind):
    enabled = False

    def sink(kind, payload):
        if enabled and kind == failed_kind:
            raise OSError("storage unavailable")

    engine, broker, _ = runtime(sink=sink)
    if failed_kind in {"order_ack", "spot_state"}:
        enabled = True
        with pytest.raises(OSError):
            engine.submit(entry())
    else:
        engine.submit(entry())
        enabled = True
        broker.events = [execution()]
        broker.reports = [report("800", "2")]
        with pytest.raises(OSError):
            engine.poll()
    assert engine.halted and engine.persistence_failed
    before = list(broker.calls)
    with pytest.raises(RuntimeError):
        engine.submit(entry())
    with pytest.raises(RuntimeError):
        engine.close()
    assert broker.calls == before


@pytest.mark.parametrize(
    "bad",
    [
        {"session_id": "other"},
        {"symbol": "ETHUSDT"},
        {"client_order_id": "unowned"},
        {"side": "sell"},
        {"event_id": ""},
        {"exchange_order_id": None},
    ],
)
def test_unattributed_fill_is_rejected_before_accounting(bad):
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [replace(execution(), **bad)]
    with pytest.raises(ValueError):
        engine.poll()
    assert engine.ledger.quantity == 0
    assert engine.halted


def test_restart_with_uncertain_entry_cannot_resubmit_or_claim_flat():
    engine, broker, _ = runtime()
    broker.entry_error = TimeoutError()
    with pytest.raises(TimeoutError):
        engine.submit(entry())
    state = engine.export_state()
    recovered, new_broker, _ = runtime()
    recovered.restore(state)
    new_broker.reports = [report(pending=("entry",), orders=("entry",))]
    with pytest.raises(RuntimeError, match="pending"):
        recovered.reconcile()
    with pytest.raises(RuntimeError, match="halted"):
        recovered.submit(entry())
    assert not recovered.flat_confirmed
    assert not any(c[0] == "entry" for c in new_broker.calls)


def test_zero_fill_terminal_entry_is_idempotent_and_account_neutral():
    engine, broker, _ = runtime()
    engine.submit(entry())
    canceled = execution("cancel", quantity="0", price=None, status="canceled")
    broker.events = [canceled, canceled]
    broker.reports = [report()]
    engine.poll()
    assert engine.flat_confirmed
    assert engine.ledger.cash == 1000
    assert len(engine.ledger.events) == 1


def test_reconciliation_mismatch_and_incomplete_report_halt():
    for response in [report("999", "0"), report(complete=False)]:
        engine, broker, _ = runtime()
        broker.reports = [response]
        with pytest.raises(RuntimeError):
            engine.reconcile()
        assert engine.halted


def test_fill_after_failed_durable_callback_can_only_recover_in_fresh_instance():
    enabled = False

    def sink(kind, payload):
        if enabled and kind == "fill":
            raise OSError("disk")

    engine, broker, _ = runtime(sink=sink)
    engine.submit(entry())
    enabled = True
    broker.events = [execution()]
    with pytest.raises(OSError):
        engine.poll()
    enabled = False
    broker.events = [execution()]
    before = list(broker.calls)
    with pytest.raises(RuntimeError, match="storage"):
        engine.poll()
    assert broker.calls == before


@pytest.mark.parametrize("partial", [False, True])
def test_protection_placement_fill_race_uses_newer_reconciled_state(partial):
    engine, broker, records = runtime()
    engine.submit(entry())
    broker.events = [execution()]
    broker.reports = [report("800", "2")]
    original = broker.place_spot_protection

    def place(intent):
        p = original(intent)
        sold = execution(
            "during-placement",
            quantity="0.5" if partial else "2",
            status="partial" if partial else "filled",
            role="target",
            client_id=p.target_client_order_id,
            price="110",
        )
        broker.reports = [
            report(
                "855" if partial else "1020",
                "1.5" if partial else "0",
                fills=(sold,),
                protected=replace(p, quantity="1.5") if partial else None,
                orders=(p.stop_client_order_id, p.target_client_order_id) if partial else (),
            )
        ]
        return p

    broker.place_spot_protection = place
    engine.poll()
    assert not any(c[0] in {"exit", "cancel_all"} for c in broker.calls)
    assert engine.ledger.quantity == (Decimal("1.5") if partial else 0)
    assert engine.flat_confirmed is (not partial)
    assert len([r for r in records if r[0] == "trade"]) == (0 if partial else 1)


def test_unknown_fee_asset_halts_additions_but_protects_inventory():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution(fees=(SpotFee("BNB", "0.001"),))]
    broker.reports = [report("800", "2")]
    engine.poll()
    assert engine.protection is not None
    assert engine.halted
    assert engine.ledger.snapshot("100")["net_realized_pnl"] is None


def test_retired_oco_cannot_protect_a_new_entry_cycle():
    engine, broker, _ = runtime()
    p = enter(engine, broker)
    broker.events = [
        execution("sold", role="target", client_id=p.target_client_order_id, price="110")
    ]
    broker.reports = [report("1020", "0")]
    engine.poll()
    engine.submit(replace(entry(), client_order_id="entry2", intent_id="intent2"))
    broker.events = [execution("new", client_id="entry2")]
    broker.reports = [
        report("820", "2", protected=p, orders=(p.stop_client_order_id, p.target_client_order_id))
    ]
    with pytest.raises(RuntimeError):
        engine.poll()
    assert engine.halted
    assert not any(c[0] == "exit" for c in broker.calls)


def test_host_tick_rounding_is_explicit_bounded_and_keeps_a_valid_bracket():
    engine, broker, _ = runtime()
    engine.submit(replace(entry(), stop_price="90.004", target_price="120.008"))
    broker.events = [execution()]
    broker.reports = [report("800", "2")]
    broker.protection_response = lambda intent: replace(
        protection(intent), stop_price="90.01", target_price="120.00", price_tick="0.01"
    )
    engine.poll()
    assert engine.protection.stop_price == "90.01"
    assert engine.ledger.account.snapshot().open_position.current_stop == pytest.approx(90.01)


def test_arbitrary_unprotected_inventory_cannot_be_labeled_dust():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution()]
    broker.reports = [report("800", "2")]
    broker.protection_response = lambda intent: protection(intent, quantity="1", dust="1")
    with pytest.raises(RuntimeError):
        engine.poll()
    assert engine.halted


def test_pending_quote_reservation_is_explicit_and_bounded():
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.reports = [replace(report(pending=("entry",), orders=("entry",)), quote_reserved="200")]
    engine.poll()
    assert engine.quote_reserved == Decimal("200")
    assert engine.ledger.cash - engine.quote_reserved == 800
    broker.reports = [replace(report(pending=("entry",), orders=("entry",)), quote_reserved="1001")]
    with pytest.raises(RuntimeError, match="reservation"):
        engine.poll()


def test_terminal_entry_can_carry_final_execution_and_repeated_terminal_is_neutral():
    engine, broker, _ = runtime()
    engine.submit(entry())
    terminal = execution("canceled-with-fill", quantity="1", status="canceled")
    broker.events = [terminal]
    broker.reports = [report("900", "1")]
    engine.poll()
    assert engine.ledger.quantity == 1
    assert engine.protection.quantity == "1"
    assert engine.halted
    broker.events = [terminal]
    engine.poll()
    assert engine.ledger.quantity == 1
    assert len([c for c in broker.calls if c[0] == "protect"]) == 1


@pytest.mark.parametrize("role", ["entry", "target"])
def test_retired_cycle_execution_cannot_change_current_inventory(role):
    engine, broker, _ = runtime()
    engine.submit(entry())
    broker.events = [execution(quantity="1", status="canceled")]
    broker.reports = [report("900", "1")]
    engine.poll()
    p = engine.protection
    broker.events = [
        execution(
            "sold", quantity="1", role="target", client_id=p.target_client_order_id, price="100"
        )
    ]
    broker.reports = [report()]
    engine.poll()
    # A fresh evaluator session normally follows the halted partial cycle. This
    # focused probe asserts the attribution guard even with an already-current entry.
    engine.halted = False
    engine.submit(replace(entry(), client_order_id="entry2", intent_id="intent2"))
    broker.events = [execution("new", client_id="entry2")]
    broker.reports = [report("800", "2")]
    engine.poll()
    before = engine.ledger.snapshot("100")
    broker.events = [
        execution(
            "retired",
            quantity="1",
            role=role,
            client_id="entry" if role == "entry" else p.target_client_order_id,
        )
    ]
    with pytest.raises(ValueError, match="retired"):
        engine.poll()
    assert engine.ledger.snapshot("100") == before
