# koval-engine

An open engine for building and validating trading strategies as dataflow graphs. Validate strategies, run paper simulations, and integrate a pluggable backtest engine. There is no real-money code path.

[![CI](https://github.com/koval-finance/koval-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/koval-finance/koval-engine/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/koval-engine.svg)](https://pypi.org/project/koval-engine/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/koval-finance/koval-engine/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/koval-finance/koval-engine/badge)](https://scorecard.dev/viewer/?uri=github.com/koval-finance/koval-engine)

## Status

Beta. This is the `0.9.x` series and the public API may change before 1.0.

## Quick start

```bash
python -m pip install koval-engine
```

From a source checkout, use `python -m pip install -e ".[dev]"` instead.

```bash
koval blocks
koval examples --copy .
koval validate koval-examples/graphs/ema_cross_trend.json
```

The examples ship inside the package, so this works straight from a `pip install` with no checkout. `koval examples --copy .` writes an editable copy into `./koval-examples/`; `koval examples` on its own just prints where the bundled originals live.

## Backtesting

`koval blocks` and `koval validate` are provided entirely by this MIT package. Backtesting requires a separately installed engine that implements the public plugin protocol. After installing a compatible engine, an offline run uses the bundled synthetic candles:

```bash
koval backtest koval-examples/graphs/ema_cross_trend.json --data koval-examples/data/sample-1h.csv --timeframe 1h
```

The same run through the Python API is in [`examples/run_backtest.py`](https://github.com/koval-finance/koval-engine/blob/main/examples/run_backtest.py).

## Why this engine is open source

A backtest result is a claim about money. When the engine that produced it is closed, the reader is asked to trust the number without being able to check how it was computed — what filled, at what price, on which bar, with what fees. Publishing the engine turns that claim into something a skeptic can audit.

The platform's premise is pluggability: blocks, exchanges, data sources, and the backtest engine itself are meant to be swappable. That only works if the contracts are public and a third party can implement one without asking permission. A closed engine with a plugin API is a plugin API in name only.

The "no real-money path" guarantee is worth something only if it is verifiable. This repository publishes the allowlists and the tests that enforce them ([`tests/safety/`](https://github.com/koval-finance/koval-engine/tree/main/tests/safety)), so the guarantee is evidence rather than a promise.

Finally, keeping the engine separate from the hosted product keeps the open version complete. The commercial product sells hosting, accounts, and scale — not the ability to compute a moving average.

## What it does

- **Strategies as graphs.** A strategy is a JSON graph of typed nodes, not a subclass. 26 blocks in the compatibility catalogue compile onto 35 typed nodes across the FACT / STATE / POLICY / INTERPRETATION / EXECUTION domains.
- **Validated dataflow execution.** Graph structure, port compatibility, and emitted entity types are checked at their execution boundaries.
- **Backtesting** through a pluggable engine protocol.
- **Paper trading** against a simulated broker with a documented fill ruleset.
- **Non-production execution.** Binance Futures testnet is supported. WhiteBIT market data is read-only; execution is disabled because WhiteBIT has no verified public sandbox.
- **Market data** via read-only REST adapters with a local Parquet OHLCV cache.

## What it does not do

Read this before trusting a number it prints.

- **Paper simulation.** `PaperBroker` uses deterministic OHLC-bar fills and does not deduct commissions, funding, or borrow costs. A stop crossed by an opening gap fills at the bar open. If one bar reaches both the stop and target, the stop fills first. Order-book depth and queue position are not modelled.
- **Backtest plugins.** Each plugin owns its fill, fee, slippage, and partial-fill model. Review the selected plugin's documentation before relying on its results.
- **Sandbox execution.** The built-in Binance USD-M Futures testnet broker accepts market entries only. Every entry requires a full stop-loss/take-profit bracket, and protection is confirmed immediately after a full fill. A partial fill triggers containment instead of being adopted as a running strategy position.
- **Execution scope.** The CLI and built-in Binance sandbox broker target futures. `resolve_execution_settings` can carry spot fee settings for integrations, but this package does not implement built-in spot order execution.
- **No real-money trading.** Only `paper` and `binance_sandbox` are reachable execution modes. WhiteBIT execution fails closed. These boundaries are enforced in code and pinned by safety tests.

Backtest and paper results are simplified simulations. Depending on the omitted market effects and fill assumptions, they may overstate or understate real execution. Treat them as research evidence, not as a forecast of returns.

## Architecture

The core is MIT. Backtest engines are separate plugins discovered at runtime through a Python entry-point group, so implementations can choose their own dependencies and licence:

```
koval-engine (MIT)  ──entry-point group "koval.backtest_engines"──▶  compatible engine plugin
```

Nothing in this repository imports Backtrader, and a test ([`tests/test_license_boundary.py`](https://github.com/koval-finance/koval-engine/blob/main/tests/test_license_boundary.py)) fails the build if that ever changes. The separation lets the core stay MIT while supporting independently distributed engines.

```
src/koval/
├── engine/      backtest protocol, live engine, paper broker, metrics, account state
├── strategy/
│   ├── base/        DeclarativeStrategy ABC, TradeSetup, EntryConfig
│   ├── helpers/     pure block functions — no framework, no state
│   ├── graph/       typed dataflow engine: entities, domains, ports, executor
│   ├── nodes/       typed nodes wrapping the pure helpers
│   └── registry.py  BLOCK_CATALOG and STRATEGY_REGISTRY
├── exchanges/   data adapters, OHLCV cache, sandbox brokers
└── cli/         the `koval` console script
```

## Installing a backtest engine

`koval blocks` and `koval validate` work standalone. `koval backtest` needs a separately installed engine. Without one, the CLI exits with an actionable error rather than a traceback.

To write your own, implement `BacktestEngineProtocol` from `koval.engine.backtest_engine` and advertise a factory in the `koval.backtest_engines` entry-point group:

```toml
[project.entry-points."koval.backtest_engines"]
my-engine = "my_package.engine:create_engine"
```

Python callers can select that registered name explicitly:

```python
from koval.engine.backtest_engine import load_backtest_engine

engine = load_backtest_engine("my-engine")
```

The command-line override is instead an importable module path whose module exposes `create_engine`, for example:

```bash
KOVAL_BACKTEST_ENGINE=my_package.engine koval backtest graph.json --data candles.csv --timeframe 1h
```

Because the protocol is public, an engine written this way needs no coordination with this project.

## Contributing

See [CONTRIBUTING.md](https://github.com/koval-finance/koval-engine/blob/main/CONTRIBUTING.md). Contributions are accepted under the DCO — sign commits with `git commit -s`. Maintainer response is best-effort. Working with a coding agent? Point it at [AGENTS.md](https://github.com/koval-finance/koval-engine/blob/main/AGENTS.md) — most tools read it automatically.

## Security

See [SECURITY.md](https://github.com/koval-finance/koval-engine/blob/main/SECURITY.md). Report vulnerabilities privately through GitHub, not in a public issue. Any code path that could reach a real-money endpoint is the highest-severity report this project accepts.

## Licence

MIT — see [LICENSE](https://github.com/koval-finance/koval-engine/blob/main/LICENSE).

## Hosted product

[koval.finance](https://koval.finance) is a separate commercial hosted product built on this engine.
