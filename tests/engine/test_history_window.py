import pytest

from koval.engine.history_window import DEFAULT_HISTORY_BARS, resolve_history_bars
from koval.engine.live_engine import LiveEngineConfig


def test_default_window_is_one_thousand_bars():
    assert DEFAULT_HISTORY_BARS == 1000
    assert (
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1h", initial_capital=1.0).max_window == 1000
    )


@pytest.mark.parametrize("value", [2, 300, 1000, 100_000])
def test_resolve_history_bars_accepts_ints(value):
    assert resolve_history_bars(value) == value


@pytest.mark.parametrize("value", [1, 0, -5, True, 2.5, "300", 100_001, None])
def test_resolve_history_bars_rejects_invalid(value):
    with pytest.raises(ValueError, match="history_bars"):
        resolve_history_bars(value)


def test_history_tail_reports_the_last_preloaded_bar():
    import numpy as np

    from koval.engine.live_engine import LiveEngine
    from koval.examples import parity_fixtures

    graph = parity_fixtures()[0]["graph"]
    rows = np.array([[0, 100, 101, 99, 100, 1], [3_600_000, 100, 101, 99, 100, 1]], dtype=float)
    engine = LiveEngine(
        graph,
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1h", initial_capital=10_000.0, history=rows),
    )
    assert engine.history_tail_ms == 3_600_000
    bare = LiveEngine(
        graph,
        LiveEngineConfig(symbol="BTCUSDT", timeframe="1h", initial_capital=10_000.0),
    )
    assert bare.history_tail_ms is None
