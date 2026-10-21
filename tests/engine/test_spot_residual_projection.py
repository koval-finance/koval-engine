"""Keep authoritative Spot residuals when float subtraction loses them."""

from dataclasses import replace
from decimal import Decimal

import pytest

from koval.engine.account_state import OpenPosition, PlatformAccountState
from koval.engine.spot_account import SpotLedger
from tests.engine.spot_fixtures import ScriptedSpotBroker, entry, execution, report
from tests.engine.test_spot_lifecycle import runtime


def test_exact_residual_keeps_inventory_margin_and_reconciliation():
    ledger = SpotLedger(ScriptedSpotBroker.spot_identity, "1000")
    ledger.apply(execution(quantity="1000000000", price="0.000001"))

    ledger.apply(
        execution(
            "partial-exit",
            quantity="999999999.99999999",
            price="0.000001",
            role="target",
            status="partial",
        )
    )

    assert ledger.quantity == Decimal("0.00000001")
    assert ledger.cost == Decimal("0.00000000000001")
    assert ledger.cash == Decimal("999.99999999999999")
    snapshot = ledger.account.snapshot()
    assert snapshot.open_position.quantity == 0.00000001
    assert snapshot.open_position.margin == pytest.approx(1e-14, rel=1e-15, abs=0)
    assert snapshot.margin_used == pytest.approx(1e-14, rel=1e-15, abs=0)
    assert snapshot.equity == 1000
    assert ledger.account.reconcile().balanced


def test_accepted_decimal_bounds_do_not_round_a_partial_sale_flat():
    # Both values are inside the public bounds: magnitude up to 10**18 and
    # 72 decimal places. Construct the delta independently of the ledger context.
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 200
        initial = Decimal("1000000000000000000")
        residual = Decimal("1e-72")
        sold = initial - residual
    ledger = SpotLedger(ScriptedSpotBroker.spot_identity, str(initial))
    ledger.apply(execution(quantity=str(initial), price="1"))

    ledger.apply(
        execution("partial-exit", quantity=str(sold), price="1", role="target", status="partial")
    )

    assert ledger.quantity == residual
    assert ledger.cost == residual
    assert ledger.cash == sold
    assert ledger.account.snapshot().open_position.quantity == 1e-72


def test_tiny_residual_retains_protection_and_survives_restore():
    engine, broker, _ = runtime()
    engine.submit(
        replace(
            entry(),
            quantity="1000000000",
            price="0.000001",
            stop_price="0.0000009",
            target_price="0.0000012",
        )
    )
    broker.events = [execution(quantity="1000000000", price="0.000001")]
    broker.reports = [report("0", "1000000000")]
    engine.poll()
    protected = replace(engine.protection, quantity="0.00000001")
    broker.events = [
        execution(
            "partial-exit",
            quantity="999999999.99999999",
            price="0.000001",
            role="target",
            status="partial",
            client_id=protected.target_client_order_id,
        )
    ]
    residual_report = report(
        "999.99999999999999",
        "0.00000001",
        protected=protected,
        orders=(protected.stop_client_order_id, protected.target_client_order_id),
    )
    broker.reports = [residual_report]

    engine.poll()
    recovered, new_broker, _ = runtime()
    recovered.restore(engine.export_state())
    new_broker.reports = [residual_report]
    recovered.reconcile()

    for coordinator in (engine, recovered):
        assert coordinator.ledger.quantity == Decimal("0.00000001")
        assert coordinator.ledger.cost == Decimal("0.00000000000001")
        assert coordinator.protection == protected
        assert not coordinator.flat_confirmed
        snapshot = coordinator.ledger.account.snapshot()
        assert snapshot.open_position.quantity == 0.00000001
        assert snapshot.margin_used == pytest.approx(1e-14, rel=1e-15, abs=0)
        assert coordinator.ledger.account.reconcile().balanced
    assert not any(kind in {"entry", "protect", "exit"} for kind, _ in new_broker.calls)


def test_many_entry_deltas_do_not_make_an_exact_partial_exit_invalid():
    ledger = SpotLedger(ScriptedSpotBroker.spot_identity, "1000")
    for index in range(1000):
        ledger.apply(execution(str(index), quantity="0.01", price="1", status="partial"))

    ledger.apply(
        execution("partial-exit", quantity="0.1", price="1", role="target", status="partial")
    )

    assert ledger.quantity == Decimal("9.90")
    assert ledger.cost == Decimal("9.90")
    assert ledger.account.snapshot().open_position.quantity == 9.9
    assert ledger.account.snapshot().margin_used == 9.9
    assert ledger.account.reconcile().balanced


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": float("nan")},
        {"quantity": float("inf")},
        {"quantity": True},
        {"entry_price": 0},
        {"entry_price": float("inf")},
        {"margin": -1},
        {"margin": float("nan")},
        {"current_stop": -1},
        {"current_stop": float("inf")},
        {"side": "long"},
    ],
)
def test_invalid_position_projection_is_rejected_without_mutation(changes):
    account = PlatformAccountState(1000, market="spot")
    account.on_open(side="buy", entry_price=100, quantity=2, current_stop=90, margin=200)
    before = account.snapshot()

    with pytest.raises(ValueError):
        account.set_position_projection(replace(before.open_position, **changes))

    assert account.snapshot() == before
    assert account.ledger.entries == ()


def test_position_projection_replaces_totals_without_recording_cashflows_or_changing_equity():
    account = PlatformAccountState(1000, market="spot")
    account.on_open(side="buy", entry_price=100, quantity=2, current_stop=90, margin=200)

    account.set_position_projection(OpenPosition("buy", 0.000001, 1e-8, 0, 1e-14))

    assert account.snapshot().open_position == OpenPosition("buy", 0.000001, 1e-8, 0, 1e-14)
    assert account.snapshot().margin_used == 1e-14
    assert account.snapshot().equity == 1000
    assert account.ledger.entries == ()


def test_tiny_execution_is_recorded_even_when_projected_quantity_does_not_change():
    ledger = SpotLedger(ScriptedSpotBroker.spot_identity, "1000")
    ledger.apply(execution(quantity="1000000000", price="0.000001"))

    ledger.apply(
        execution("partial-exit", quantity="0.00000001", price="1", role="target", status="partial")
    )

    assert ledger.quantity == Decimal("999999999.99999999")
    assert ledger.account.snapshot().open_position.quantity == 1e9
    assert ledger.account.ledger.trade_realized_pnl == pytest.approx(9.99999e-9)


def test_partial_close_without_exact_residual_keeps_oversell_guard():
    account = PlatformAccountState(1000, market="spot")
    account.on_open(side="buy", entry_price=100, quantity=2, current_stop=90, margin=200)

    with pytest.raises(ValueError, match="partial close quantity"):
        account.on_partial_close(quantity=2)

    account.on_partial_close(quantity=0.5)
    assert account.snapshot().open_position.quantity == 1.5
    assert account.snapshot().margin_used == 150
