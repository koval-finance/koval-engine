# koval-engine

An open engine for building and validating trading strategies as dataflow graphs. Validate strategies, run paper simulations, and integrate a pluggable backtest engine. There is no real-money code path.

[![CI](https://github.com/koval-finance/koval-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/koval-finance/koval-engine/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/koval-engine.svg)](https://pypi.org/project/koval-engine/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/koval-finance/koval-engine/blob/main/LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/koval-finance/koval-engine/badge)](https://scorecard.dev/viewer/?uri=github.com/koval-finance/koval-engine)

## Status

Beta. This is the `0.12.x` series and the public API may change before 1.0.

Version 0.12 adds protocol 2 with explicit warmup/evaluation boundaries and a
synchronous runtime journal for auditable host archives. It also strengthens
WhiteBIT acquisition coverage, execution-evidence validation, uncertain sandbox
entry containment, and trade narratives.
The 0.12.1 patch adds an optional backtest preparation hook that computes exact
finite-window EMA/RSI/ATR values for built-in graph nodes in batch. It also
hardens evidence decoding, fill causality, funding-grid validation and WhiteBIT
fee conversion without changing the engine protocol or fill profiles.
See the [runtime and plugin migration contract](https://github.com/koval-finance/koval-engine/blob/main/agents_docs/runtime_contract.md)
and the [execution realism review](https://github.com/koval-finance/koval-engine/blob/main/agents_docs/realism_review.md).

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

- **Paper simulation.** `PaperBroker` ships three versioned profiles. `paper_legacy_v1` (the default) preserves the original cost-free behavior. `paper_ohlcv_fixed_v1` preserves execution contract v1, including next-open market entries and one-bar-delayed protection. `paper_ohlcv_realistic_v2` activates protection on the entry bar, records unresolved same-bar ambiguity, resolves it stop-first, and handles favorable limit gaps at the favorable open without violating the limit after costs. A market fill already beyond its bracket is contained at the same market reference rather than an unreachable stop or target. The broker sizes and reconciles from actual fills through an append-only cash ledger.
- **Optional execution evidence.** A v2 paper run can receive historical funding, fee schedules, instrument constraints, mark prices, and a lagged impact calibration. Supplied evidence is identified on fills and in resolved metadata; missing evidence is reported as `unavailable`, not silently treated as observed zero. Instrument rules can quantize or reject orders, mark prices can trigger maintenance-margin liquidation under the currently supported single-position cross-margin model, and one shared bar-volume budget can produce partial fills with explicit remainder and protection policies.
- **Auditable runtime inputs.** An optional explicit runtime contract separates warmup from evaluation and fixes initial risk baselines and replay endings. A synchronous host archive callback exports full OHLCV, decisions, executions, cashflows and account snapshots with ordered IDs and integrity hashes. Archive storage and broker restart recovery remain the host's responsibility.
- **Backtest plugins.** Plugins remain separate packages. `EngineRunSpec` can request an execution-contract version and named capabilities; negotiation fails closed when a plugin cannot honor them. Passing the same public fixtures and archived evidence proves that runtimes followed the same declared deterministic rules. It does not prove that an OHLCV simulation reproduced a real exchange's order-book queue or historical account fill.

- **Sandbox execution.** The built-in Binance USD-M Futures testnet broker accepts market, limit and stop entries. Conditional orders use Algo Service by default and fills are read from the triggered child order. Every entry requires a full stop-loss/take-profit bracket, and protection is confirmed immediately after a full fill; a working entry is supervised independently of blocked or retrying market-data requests. Venue commissions are read from `userTrades` and are never converted from another asset. A partial fill triggers containment instead of being adopted as a running strategy position. Signed requests support measured server-time skew; excessive skew and exhausted market-data retries fail with stable reasons.
- **Execution scope.** The CLI and built-in Binance sandbox broker target futures. Data adapters serve both spot and futures, selected by `exchange_type`. Fees for a venue and market without a built-in schedule must be passed explicitly — `resolve_execution_settings` raises rather than defaulting to zero. Paper supports long-only spot at leverage one; the built-in exchange sandbox does not execute spot orders.
- **No real-money trading.** Only `paper` and `binance_sandbox` are reachable execution modes. WhiteBIT execution fails closed. These boundaries are enforced in code and pinned by safety tests.

Paper session metadata includes content identities for actual primary/warm-up
candles and supplied execution evidence. Set `LiveEngineConfig.exchange` (or use
a factory-built broker) to name the venue. Unknown-venue runs are explicitly not
comparable. `identified_simulation` means the inputs are identified, not that
all evidence was available or archived. The host must retain the exact inputs.
Nonzero cancellation/replacement latency and non-unit contract multipliers are
rejected by the current paper broker. Authenticated testnet acceptance is
separate from the automated HTTP-mock suite.

Even the richest paper profile is a calibrated OHLCV proxy, not an order-book or queue-position simulator. Latency values are deterministic assumptions, and a bar's volume does not show which liquidity was available at a particular price. Results may overstate or understate actual execution. Treat them as reproducible research evidence, not as a forecast or guarantee of returns.

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
