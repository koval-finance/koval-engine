"""The bundled sample must actually exercise the bundled strategy.

A quickstart that prints `total_trades 0` teaches a newcomer that the engine is
broken. Counting entry signals needs only the pure helpers, so this guard runs
in the default suite without the GPL backtest adapter.
"""

from __future__ import annotations

import json

from koval.cli.data import load_csv_candles
from koval.examples import example_path
from koval.strategy.helpers.filters.trend import ema_trend_filter
from koval.strategy.helpers.signals.technical import ema_cross

SAMPLE = example_path("data", "sample-1h.csv")
GRAPH = example_path("graphs", "ema_cross_trend.json")


def _params() -> dict[str, dict]:
    graph = json.loads(GRAPH.read_text(encoding="utf-8"))["graph"]
    return {block["id"]: block["params"] for block in graph["blocks"]}


def _entry_signal_bars() -> list[int]:
    params = _params()
    fast, slow = params["sig"]["fast"], params["sig"]["slow"]
    period, direction = params["trend"]["period"], params["trend"]["direction"]

    closes = load_csv_candles(SAMPLE)[:, 4]
    bars = []
    for index in range(period, len(closes)):
        window = closes[: index + 1]
        if ema_cross(window, fast, slow) == direction and ema_trend_filter(
            window, period, direction
        ):
            bars.append(index)
    return bars


def test_sample_data_triggers_several_entry_signals():
    signals = _entry_signal_bars()
    assert len(signals) >= 5, (
        f"the bundled sample produces only {len(signals)} entry signals; "
        "the quickstart would print an all-zero result. "
        "Retune examples/generate_sample.py."
    )


def test_sample_data_is_long_enough_for_the_trend_filter_warmup():
    closes = load_csv_candles(SAMPLE)[:, 4]
    period = _params()["trend"]["period"]

    assert len(closes) > period * 2, (
        "the sample must leave room after the trend filter's warm-up, "
        "otherwise almost every bar is spent warming up"
    )
