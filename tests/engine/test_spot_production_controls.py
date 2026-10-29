"""Residual disposal and terminal custody use exact host-authorized evidence."""

from copy import deepcopy
from dataclasses import replace

import pytest

from koval.engine.run_identity import content_sha256
from tests.engine.spot_fixtures import entry, execution
from tests.engine.test_spot_production import PRODUCTION, live

MUTATIONS = {"entry", "exit", "protect", "cancel_all", "cancel_entry"}


def event(broker, *args, **kwargs):
    return replace(execution(*args, **kwargs), metadata={"spot_context": broker.context})


def submit(engine, broker, quantity="2"):
    engine._spot.submit(
        replace(
            entry(), target=PRODUCTION, quantity=quantity, metadata={"spot_context": broker.context}
        )
    )


def acquired(host=None):
    engine, broker, records = live(host=host)
    submit(engine, broker)
    broker.events = [event(broker)]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="800",
        base_quantity="2",
        base_free="2",
        fees_complete=True,
        terminal_order_ids=("entry",),
    )
    engine.observe_spot()
    return engine, broker, records


def protected(host=None):
    engine, broker, records = acquired(host)
    from tests.engine.spot_fixtures import protection

    broker.protection_response = lambda intent: replace(protection(intent), context=broker.context)
    engine.supervise_spot()
    return engine, broker, records


class Host:
    def __init__(self):
        self.calls = []
        self.change = {}

    def authorize_spot_control(self, request):
        from koval.engine.spot_control import SpotControlReceipt

        self.calls.append(deepcopy(request))
        return SpotControlReceipt(
            request_sha256=content_sha256(request),
            receipt_id="receipt-" + request["action"],
            grant_id="grant-" + request["action"],
            quantity="1",
            max_quantity="1",
            max_notional="100",
            reference_price="99",
            protection_disposition="retain",
            retained_order_ids=tuple(request["open_order_ids"]),
            **self.change,
        )

    def verify_spot_restore(self, bundle):
        return content_sha256(bundle)


def test_supervision_while_halted_retains_confirmed_oco_and_frozen_prices():
    engine, broker, _ = protected()
    engine.halt_spot_entries("pause")
    before = len(broker.calls)
    engine.supervise_spot()
    assert all(c[0] not in MUTATIONS for c in broker.calls[before:])
    assert engine.spot_account["protection"]["stop_price"] == "90"
    assert engine.spot_account["protection"]["target_price"] == "120"


def test_finish_retains_oco_and_blocks_supervision_close_and_cleanup():
    engine, broker, records = protected(Host())
    engine.halt_spot_entries("custody")
    result = engine.finish_spot(
        disposition="custody_transferred", evidence_hash="synthetic-evidence"
    )
    assert result["execution"]["terminal"]["disposition"] == "custody_transferred"
    assert not engine.spot_account["flat_confirmed"]
    assert engine.spot_account["base_quantity"] == "2"
    before = len(broker.calls)
    engine.supervise_spot()
    engine.close_spot()
    engine._finalize()
    assert all(c[0] not in MUTATIONS for c in broker.calls[before:])
    assert engine.spot_account["protection"] is not None
    assert any(r["kind"] == "spot_finish" for r in records)


def test_finish_persistence_failure_cannot_acknowledge_custody():
    engine, broker, _ = protected(Host())

    def sink(record):
        if record["kind"] == "spot_finish":
            raise OSError("disk")

    engine._journal.sink = sink
    with pytest.raises(OSError):
        engine.finish_spot(disposition="custody_transferred", evidence_hash="evidence")
    assert engine.spot_state["execution"]["terminal"] is None
    assert engine.spot_account["uncertain"]


def test_flat_finish_requires_exact_zero_and_no_orders():
    engine, broker, _ = acquired(Host())
    with pytest.raises(RuntimeError, match="flat"):
        engine.finish_spot(disposition="closed_flat", evidence_hash="evidence")
    assert not [c for c in broker.calls if c[0] in {"exit", "cancel_all"}]


def test_finish_requires_host_verified_receipt_and_matching_request():
    engine, broker, _ = acquired()
    with pytest.raises(RuntimeError, match="host"):
        engine.finish_spot(disposition="custody_transferred", evidence_hash="evidence")
    assert engine.spot_state["execution"]["terminal"] is None


def residual(host=None):
    engine, broker, records = acquired(host or Host())
    original = broker.submit_exit

    def sell(intent):
        ack = original(intent)
        broker.events = [
            event(
                broker,
                "partial-close",
                quantity="1",
                price="99",
                role="flatten",
                status="canceled",
                client_id=intent.client_order_id,
            )
        ]
        broker.last_report = replace(
            broker.last_report,
            quote_balance="899",
            base_quantity="1",
            base_free="1",
            terminal_order_ids=("entry", intent.client_order_id),
        )
        return ack

    broker.submit_exit = sell
    with pytest.raises(RuntimeError, match="closure unconfirmed"):
        engine.close_spot()
    broker.submit_exit = original
    observed = engine.observe_spot()
    return engine, broker, records, observed["execution"]["observation_hash"]


def test_residual_disposal_sends_once_preserves_original_close_id_and_replays():
    engine, broker, records, state_hash = residual()
    old = engine.spot_state["execution"]["close_id"]
    first = engine.retry_spot_close(attempt_id="human-attempt-2", expected_state_hash=state_hash)
    second = engine.retry_spot_close(attempt_id="human-attempt-2", expected_state_hash=state_hash)
    assert first == second
    assert engine.spot_state["execution"]["close_id"] == old
    assert len([c for c in broker.calls if c[0] == "exit"]) == 2
    assert records[-1]["kind"] == "spot_state"
    with pytest.raises(ValueError, match="payload"):
        engine.retry_spot_close(attempt_id="human-attempt-2", expected_state_hash="changed")


@pytest.mark.parametrize(
    "change",
    [
        {"unknown_request_ids": ("unknown",)},
        {"terminal_order_ids": ("entry",)},
        {"open_order_ids": ("entry",), "pending_entry_ids": ("entry",)},
        {"fees_complete": False},
        {"base_free": "0"},
    ],
)
def test_residual_refuses_unknown_working_or_incomplete_predecessors(change):
    engine, broker, _, state_hash = residual()
    broker.last_report = replace(broker.last_report, **change)
    before = len([c for c in broker.calls if c[0] == "exit"])
    with pytest.raises((ValueError, RuntimeError)):
        engine.retry_spot_close(attempt_id="next", expected_state_hash=state_hash)
    assert len([c for c in broker.calls if c[0] == "exit"]) == before


def test_residual_refuses_stale_hash():
    engine, broker, _, _ = residual()
    with pytest.raises(ValueError, match="stale"):
        engine.retry_spot_close(attempt_id="next", expected_state_hash="stale")
    assert len([c for c in broker.calls if c[0] == "exit"]) == 1


def test_reentrant_finish_in_intent_callback_cannot_allow_send_after_finish():
    engine, broker, _ = live(host=Host())

    def callback(payload):
        engine.finish_spot(disposition="closed_flat", evidence_hash="evidence")

    engine._on_order_intent = engine._record_callback("order_intent", callback)
    # Exercise the public serialized bar path rather than the coordinator directly.
    engine._strategy.should_long = lambda: True
    from koval.strategy.base.trade_setup import TradeSetup

    engine._strategy.go_long = lambda: TradeSetup(
        direction="long",
        entry_price=100,
        stop_loss=90,
        take_profit=120,
        size=1,
        entry_type="market",
    )
    with pytest.raises(RuntimeError, match="reentrant"):
        engine._process_bar([0, 100, 101, 99, 100, 10])
    assert not [c for c in broker.calls if c[0] in MUTATIONS]


def test_disposal_unknown_send_returns_saved_receipt_on_replay_without_send():
    engine, broker, _, state_hash = residual()
    broker.exit_error = TimeoutError("unknown additional disposal")
    with pytest.raises(TimeoutError):
        engine.retry_spot_close(attempt_id="ambiguous", expected_state_hash=state_hash)
    result = engine.retry_spot_close(attempt_id="ambiguous", expected_state_hash=state_hash)
    assert result["status"] == "unknown"
    assert result["ack"] is None
    assert len([c for c in broker.calls if c[0] == "exit"]) == 2


@pytest.mark.parametrize(
    "changed",
    [
        {"quantity": "2"},
        {"max_notional": "98"},
        {"request_sha256": "other"},
        {"grant_id": ""},
        {"max_quantity": "0"},
    ],
)
def test_disposal_refuses_invalid_authority_or_ceilings(changed):
    class BadHost(Host):
        def authorize_spot_control(self, request):
            valid = super().authorize_spot_control(request)
            return replace(valid, **changed)

    engine, broker, _, state_hash = residual(BadHost())
    with pytest.raises(ValueError):
        engine.retry_spot_close(attempt_id="bounded", expected_state_hash=state_hash)
    assert len([c for c in broker.calls if c[0] == "exit"]) == 1
    assert not engine.spot_state["execution"]["disposal_attempts"]


def test_terminal_observation_journals_late_fills_without_mutation_or_lost_custody():
    engine, broker, records = protected(Host())
    engine.finish_spot(disposition="custody_transferred", evidence_hash="evidence")
    terminal = deepcopy(engine.spot_state["execution"]["terminal"])
    p = engine._spot.protection
    broker.events = [
        event(
            broker,
            "late-sale",
            role="target",
            client_id=p.target_client_order_id,
            quantity="2",
            price="110",
        )
    ]
    list_id = engine._spot.protection_intents[-1].metadata["list_client_order_id"]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="1020",
        base_quantity="0",
        base_free="0",
        protection=None,
        open_order_ids=(),
        terminal_order_ids=("entry", p.stop_client_order_id, p.target_client_order_id),
        terminal_list_ids=(list_id,),
    )
    before = len(broker.calls)
    engine.observe_spot()
    engine.supervise_spot()
    engine._finalize()
    assert engine.spot_account["base_quantity"] == "0"
    assert engine.spot_state["execution"]["terminal"] == terminal
    assert any(r["kind"] == "fill" and r["payload"]["event_id"] == "late-sale" for r in records)
    assert all(c[0] not in MUTATIONS for c in broker.calls[before:])


def test_closed_flat_finish_and_replay_do_not_dispatch():
    engine, broker, _ = live(host=Host())
    broker.last_report = replace(broker.last_report, fees_complete=True)
    result = engine.finish_spot(disposition="closed_flat", evidence_hash="flat-evidence")
    engine.finish_spot(disposition="closed_flat", evidence_hash="flat-evidence")
    engine.close_spot()
    assert result["execution"]["terminal"]["inventory"] == "0"
    assert not [c for c in broker.calls if c[0] in MUTATIONS]


def test_residual_grant_cannot_use_an_exit_from_a_previous_completed_cycle():
    from tests.engine.spot_fixtures import protection

    engine, broker, _ = live(host=Host())
    submit(engine, broker)
    broker.events = [event(broker)]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="800",
        base_quantity="2",
        terminal_order_ids=("entry",),
        fees_complete=True,
    )
    broker.protection_response = lambda intent: replace(protection(intent), context=broker.context)
    engine.supervise_spot()
    p = engine._spot.protection
    list_id = engine._spot.protection_intents[-1].metadata["list_client_order_id"]
    broker.events = [
        event(
            broker,
            "first-cycle-sale",
            quantity="2",
            role="target",
            client_id=p.target_client_order_id,
            price="110",
        )
    ]
    terminal_ids = ("entry", p.stop_client_order_id, p.target_client_order_id)
    broker.last_report = replace(
        broker.last_report,
        quote_balance="1020",
        base_quantity="0",
        open_order_ids=(),
        protection=None,
        terminal_order_ids=terminal_ids,
        terminal_list_ids=(list_id,),
    )
    engine.supervise_spot()
    assert not engine.spot_account["entries_halted"]
    engine._spot.submit(
        replace(
            entry(),
            target=PRODUCTION,
            client_order_id="entry2",
            intent_id="next",
            metadata={"spot_context": broker.context},
        )
    )
    broker.events = [event(broker, "second-cycle-buy", client_id="entry2")]
    broker.last_report = replace(
        broker.last_report,
        quote_balance="820",
        base_quantity="2",
        base_free="2",
        terminal_order_ids=(*terminal_ids, "entry2"),
    )
    observed = engine.observe_spot()
    with pytest.raises(RuntimeError, match="partial-exit"):
        engine.retry_spot_close(
            attempt_id="wrong-cycle", expected_state_hash=observed["execution"]["observation_hash"]
        )
    assert not [c for c in broker.calls if c[0] == "exit"]
