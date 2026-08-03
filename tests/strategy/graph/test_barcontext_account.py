def test_barcontext_has_account_and_symbol_defaults():
    from koval.strategy.graph.node import BarContext

    ctx = BarContext(
        close=1.0,
        high=1.0,
        low=1.0,
        open=1.0,
        volume=1.0,
        bar_index=0,
        timestamp_ms=0,
    )
    assert ctx.account is None
    assert ctx.symbol == ""
