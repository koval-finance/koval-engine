_MS_PER_DAY = 86_400_000


def test_initial_snapshot_is_flat():
    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=10_000.0)
    snap = acc.snapshot()
    assert snap.equity == 10_000.0
    assert snap.balance == 10_000.0
    assert snap.open_positions == 0
    assert snap.open_position is None
    assert snap.drawdown_pct == 0.0
    assert snap.daily_pnl == 0.0


def test_drawdown_tracks_peak():
    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=10_000.0)
    acc.on_bar(equity=11_000.0, timestamp_ms=0)  # new peak
    acc.on_bar(equity=9_900.0, timestamp_ms=1_000)  # 10% off peak
    snap = acc.snapshot()
    assert snap.peak_equity == 11_000.0
    assert round(snap.drawdown_pct, 2) == 10.0


def test_daily_pnl_resets_on_utc_day_boundary():
    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=10_000.0)
    acc.on_bar(equity=10_000.0, timestamp_ms=0)  # day 0 start
    acc.on_bar(equity=10_500.0, timestamp_ms=1_000)  # +500 same day
    assert acc.snapshot().daily_pnl == 500.0
    acc.on_bar(equity=10_500.0, timestamp_ms=_MS_PER_DAY)  # new day, reset baseline
    assert acc.snapshot().daily_pnl == 0.0


def test_open_records_position_and_margin():
    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=10_000.0)
    acc.on_bar(equity=10_000.0, timestamp_ms=0)
    acc.on_open(side="buy", entry_price=100.0, quantity=2.0, current_stop=98.0, margin=100.0)
    snap = acc.snapshot()
    assert snap.open_positions == 1
    assert snap.open_position.side == "buy"
    assert snap.margin_used == 100.0
    assert snap.free_margin == 10_000.0 - 100.0


def test_close_realizes_pnl_and_frees_margin():
    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=10_000.0)
    acc.on_open(side="buy", entry_price=100.0, quantity=2.0, current_stop=98.0, margin=100.0)
    acc.on_close(realized_pnl=250.0)
    snap = acc.snapshot()
    assert snap.open_positions == 0
    assert snap.realized_pnl == 250.0
    assert snap.balance == 10_250.0
    assert snap.equity == 10_250.0
    assert snap.margin_used == 0.0
    assert acc.reconcile().balanced


def test_pickle_round_trip():
    import pickle

    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=5_000.0)
    acc.on_bar(equity=5_200.0, timestamp_ms=0)
    acc.on_open(side="sell", entry_price=50.0, quantity=1.0, current_stop=51.0, margin=25.0)
    restored = pickle.loads(pickle.dumps(acc))
    assert restored.snapshot().equity == 5_200.0
    assert restored.snapshot().open_position.side == "sell"


def test_on_fee_debits_the_wallet():
    import pytest

    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=100.0)
    account.on_fee(0.5)
    assert account.snapshot().balance == pytest.approx(99.5)
    assert account.snapshot().equity == pytest.approx(99.5)

    account.on_fee(-0.2)
    assert account.snapshot().balance == pytest.approx(99.7)
    assert account.snapshot().equity == pytest.approx(99.7)
    assert account.snapshot().fees == pytest.approx(0.3)
    assert account.reconcile().balanced


def test_funding_updates_balance_and_equity_immediately():
    import pytest

    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=100.0)
    account.on_funding(-1.5, timestamp_ms=1)

    assert account.snapshot().balance == pytest.approx(98.5)
    assert account.snapshot().equity == pytest.approx(98.5)
    assert account.reconcile().balanced


def test_first_bar_and_overnight_gap_use_prior_equity_as_daily_baseline():
    import pytest

    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=10_000.0, daily_baseline_equity=10_200.0)
    account.on_bar(equity=10_000.0, timestamp_ms=0)
    assert account.snapshot().daily_pnl == pytest.approx(-200.0)
    assert account.snapshot().daily_loss_pct == pytest.approx(200 / 10_200 * 100)

    account.on_bar(equity=9_700.0, timestamp_ms=_MS_PER_DAY)
    assert account.snapshot().daily_pnl == pytest.approx(-300.0)
    assert account.snapshot().daily_loss_pct == pytest.approx(3.0)


def test_daily_loss_and_all_time_drawdown_are_independent():
    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=10_000.0)
    account.on_bar(equity=12_000.0, timestamp_ms=0)
    account.on_bar(equity=11_000.0, timestamp_ms=_MS_PER_DAY)
    snapshot = account.snapshot()

    assert snapshot.daily_loss_pct == 1000 / 12_000 * 100
    assert snapshot.drawdown_pct == 1000 / 12_000 * 100
    account.on_bar(equity=11_500.0, timestamp_ms=_MS_PER_DAY + 1)
    snapshot = account.snapshot()
    assert snapshot.daily_loss_pct == 500 / 12_000 * 100
    assert snapshot.drawdown_pct == 500 / 12_000 * 100


def test_account_ledger_reconciles_trade_fees_funding_and_equity():
    import pytest

    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=10_000.0)
    account.on_fee(4.0, timestamp_ms=1, reference_id="entry-1")
    account.on_funding(-2.0, timestamp_ms=2, reference_id="funding-1")
    account.on_close(realized_pnl=100.0, timestamp_ms=3, reference_id="exit-1")
    account.on_bar(equity=10_144.0, timestamp_ms=4)
    snapshot = account.snapshot()

    assert snapshot.balance == pytest.approx(10_094.0)
    assert snapshot.unrealized_pnl == pytest.approx(50.0)
    assert snapshot.trade_realized_pnl == pytest.approx(100.0)
    assert snapshot.fees == pytest.approx(4.0)
    assert snapshot.funding == pytest.approx(-2.0)
    assert [entry.kind for entry in account.ledger.entries] == [
        "commission",
        "funding",
        "trade_pnl",
    ]
    assert account.reconcile().balanced


def test_partial_entry_and_exit_resize_position_and_margin():
    import pytest

    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=10_000.0)
    account.on_open(
        side="buy",
        entry_price=100.0,
        quantity=1.0,
        current_stop=90.0,
        margin=100.0,
    )

    account.on_entry_fill(entry_price=102.0, quantity=1.0, margin=102.0)
    position = account.snapshot().open_position
    assert position.quantity == 2.0
    assert position.entry_price == 101.0
    assert account.snapshot().margin_used == 202.0

    account.on_partial_close(quantity=0.5)
    position = account.snapshot().open_position
    assert position.quantity == 1.5
    assert account.snapshot().margin_used == pytest.approx(151.5)


def test_confirmed_stop_update_is_reflected_in_account_position():
    import pytest

    from koval.engine.account_state import PlatformAccountState

    account = PlatformAccountState(starting_balance=10_000.0)
    account.on_open(
        side="buy",
        entry_price=100.0,
        quantity=1.0,
        current_stop=90.0,
        margin=100.0,
    )

    account.on_stop_update(95.0)

    assert account.snapshot().open_position.current_stop == 95.0
    with pytest.raises(ValueError, match="increase risk"):
        account.on_stop_update(94.0)
