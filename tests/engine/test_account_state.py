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
    assert snap.margin_used == 0.0


def test_pickle_round_trip():
    import pickle

    from koval.engine.account_state import PlatformAccountState

    acc = PlatformAccountState(starting_balance=5_000.0)
    acc.on_bar(equity=5_200.0, timestamp_ms=0)
    acc.on_open(side="sell", entry_price=50.0, quantity=1.0, current_stop=51.0, margin=25.0)
    restored = pickle.loads(pickle.dumps(acc))
    assert restored.snapshot().equity == 5_200.0
    assert restored.snapshot().open_position.side == "sell"
