# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below 1.0, the public API may change in a minor release.

## [Unreleased]

## [0.9.0] - 2026-07-30

First public release.

### Added

- **Typed dataflow strategy engine.** Strategies are JSON graphs of nodes across the FACT, STATE, POLICY, INTERPRETATION, and EXECUTION domains, with structural and entity validation.
- **Block catalogue** of 26 blocks — signals, filters, entries, exits, dynamic exits, and risk — compiled onto 35 typed nodes. Block helpers are pure functions over numpy with no framework dependency.
- **Pluggable backtest engine.** `BacktestEngineProtocol` plus discovery of independently distributed implementations through the `koval.backtest_engines` entry-point group, keeping this package MIT.
- **Paper broker** with a documented bracket/OCO fill ruleset and pessimistic resolution of ambiguous bars.
- **Non-production execution.** Binance Futures testnet supports fail-closed, market-entry-only execution with immediate bracket confirmation. WhiteBIT market data is read-only; execution is disabled because WhiteBIT has no verified public sandbox.
- **Market data adapters** for Binance and WhiteBIT (read-only REST) with a local Parquet OHLCV cache.
- **`koval` command-line interface** — `blocks`, `validate`, and `backtest`, the last against either a local CSV or fetched candles.
- **Runnable examples** with bundled synthetic OHLCV data, so the quickstart works offline and deterministically.
- **Typing.** The package ships a `py.typed` marker.
- **Version introspection.** `koval.__version__` reports the installed distribution version, read from distribution metadata so that `pyproject.toml` stays the only source of truth.
- **Agent-facing documentation.** A root `AGENTS.md` (read natively by most coding agents) with thin pointers for Claude Code, Gemini CLI, Cursor, and GitHub Copilot; a topic-per-file `agents_docs/` knowledge base; and `scripts/verify.sh`, the single command that reproduces CI. Cross-file consistency is pinned by `tests/test_agents_docs.py`.

### Security

- No code path reaches a real-money trading endpoint. Only `paper` and `binance_sandbox` are allowlisted execution modes; WhiteBIT execution fails closed. The invariant is pinned by `tests/safety/`.
- The MIT/GPL boundary is enforced by a test rather than by convention: no file in this package may import Backtrader.
- Releases are published to PyPI through Trusted Publishing (OIDC) with PEP 740 attestations. No long-lived PyPI credential is used in CI.

[Unreleased]: https://github.com/koval-finance/koval-engine/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/koval-finance/koval-engine/releases/tag/v0.9.0
