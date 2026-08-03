"""Run the bundled example strategy over the bundled sample candles.

The same run as the README's `koval backtest` command, through the Python API
instead of the CLI.

Requires a compatible backtest engine plugin registered through the
``koval.backtest_engines`` entry-point group.
"""

from __future__ import annotations

import json

from koval.cli.data import load_csv_candles
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import example_path


def main() -> None:
    graph = json.loads(example_path("graphs", "ema_cross_trend.json").read_text(encoding="utf-8"))
    candles = load_csv_candles(example_path("data", "sample-1h.csv"))
    spec = EngineRunSpec(graph=graph["graph"], feeds={"1h": candles}, initial_capital=10_000.0)
    result = load_backtest_engine().run(spec)
    for key in sorted(result.metrics):
        print(f"{key:<24} {result.metrics[key]}")


if __name__ == "__main__":
    main()
