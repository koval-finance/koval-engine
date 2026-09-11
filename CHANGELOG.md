# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below 1.0, the public API may change in a minor release.

## [Unreleased]

## [0.11.0] - 2026-09-11

### Added

- `scripts/check_dist.py` — a release gate that fails when a built wheel or
  source distribution does not carry this tree's `src/koval` sources, or names
  a version the tree is not at. The release workflow runs it between
  `python -m build` and the artifact upload; the default test suite runs it
  against a local `dist/`, so a stale build cannot be installed or measured as
  if it were the release.

### Changed

- Publish the MIT `koval_runtime_v2` and `koval_run_identity_v1` contracts,
  canonical market identity (`spot` / `future`, with `spot` / `perpetual`
  contract types), streaming identities for actual candles and warm-up, and
  content hashes for execution evidence. Paper statuses and trades record
  identity and a reproducibility grade; an unknown venue stays not comparable.
- `SandboxBrokerConfig` accepts and forwards funding, fees, instrument specs,
  mark prices and the execution proxy. Injected paper brokers are checked
  against explicit live configuration before a session starts.
- Binance sandbox defaults to the current conditional Algo Service API.
  Conditional triggers are followed to their actual child orders for fills and
  fees; cancellation is verified, and reconciliation includes algo open orders.
  The previous wire protocol remains an explicit `conditional_order_api="legacy"`
  option. No production origin or execution mode was added.
- Release source verification now requires artifacts in CI; local empty `dist/`
  checks remain allowed. The roadmap and public runtime/review documentation
  ship in the source distribution.

### Fixed

- Honor `on_tp_update` alongside stop updates. Validate both requested legs
  before replacement; targets may move either way while stops cannot widen risk.
  Paper applies changes after the current bar; sandbox accepts a replacement
  pair before canceling the previous reduce-only pair and contains uncertainty.
- Normalize stop-only paper updates against instrument evidence. Sandbox stop
  updates report the accepted price and require confirmed replacement/cancellation;
  an immediate fill or uncertain cancellation retains identifiers for containment.
- Reject spot shorts with `spot_short_unsupported` and continue the paper
  session, allowing later permitted entries. Reject spot leverage at the direct
  broker boundary as well as the factory.
- Enforce candle evidence request bounds, timeframe alignment, venue and source.
  Reject a run that outlives its supplied funding coverage, non-unit contract
  multipliers in base-quantity accounting, mismatched named-symbol fee currency,
  and fee evidence on a cost-free legacy profile.
- Reject nonzero cancellation/replacement latency, which previously appeared in
  metadata without changing execution. Other supported timing assumptions remain
  available in the OHLCV execution proxy.
- Preserve spread and slippage attribution across all partial-exit deltas.
- Publish three additional shared regression fixtures: long/short target
  replacement and spot-short refusal; extend the deterministic conformance matrix.
  Existing fixture bytes and v1 matching arithmetic are unchanged.

### Compatibility and validation

- `ENGINE_PROTOCOL_VERSION` remains 1. Existing paper profile names and the
  legacy default remain. Runtime defect fixes are identified by package 0.11.0
  and `koval_runtime_v2`; replay older results with their recorded package version.
- The engine remains MIT with the same six runtime dependencies. No application
  or GPL plugin implementation is included. Read
  [the runtime contract](agents_docs/runtime_contract.md) for plugin migration and
  [the review](agents_docs/realism_review.md) for sources and remaining acceptance.
- Automated verification uses mocked HTTP. A compatible plugin release and a
  supervised authenticated Binance testnet acceptance run remain separate gates;
  an OHLCV simulation does not guarantee production exchange fills or returns.

## [0.10.0] - 2026-09-10

### Added

- **Koval execution contract v1 for paper trading.** `paper_ohlcv_fixed_v1`
  (`koval.engine.paper_profile`) fills market entries at the next bar's open,
  makes protection eligible from the bar after the fill, applies an explicit
  spread, slippage and commission to every fill, treats take-profit as a
  market-on-touch order, debits `notional / leverage` of margin, and rejects an
  entry it cannot afford. `paper_legacy_v1` keeps the previous semantics and
  stays the default.
- **Public parity fixtures** shipped in the wheel (`koval.examples.parity_fixtures`)
  with a checksum manifest. Every runtime — this paper broker, a backtest plugin,
  and the application — must reproduce the same fills and the same final equity.
- `koval.engine.history_window.DEFAULT_HISTORY_BARS` — the one candle window
  every runtime injects into a strategy, now also the `LiveEngineConfig`
  default (previously 2000 in live sessions and 300 in the plugin).
- `EventType.ORDER_REJECTED`, emitted when an entry cannot be afforded. The
  session continues; it is an event, not a halt.
- `PlatformAccountState.on_fee` — commissions debit the wallet.
- `VenueCapabilities` (`koval.exchanges.capabilities`): the market an adapter
  serves, its data environment, the venue's entry order types, and the
  provenance class of its fee schedule, funding history and symbol spec.
- Market-aware adapters and factory: `get_exchange_adapter(name, exchange_type=...)`,
  `BinanceAdapter(market="spot"|"future")`, `WhiteBITAdapter(market=...)` with
  `*_PERP` symbols for futures.
- Sandbox limit and stop entries, venue commissions read from `userTrades`, and
  `PollingFeed(between_bars=...)` with `LiveEngine.watch_orders()` so a working
  entry is protected within seconds of its fill.
- `paper_ohlcv_realistic_v2`, a successor execution profile with entry-bar
  protection, conservative stop-first ambiguity reporting, explicit
  equal-timestamp ordering, and favorable long/short limit-gap fills.
- An append-only account ledger for trade PnL, fees, funding and liquidation
  fees, with reconciliation against balance, unrealized PnL, equity and margin.
- Evidence contracts for immutable canonical OHLCV datasets, historical
  perpetual funding, maker/taker fee schedules, instrument constraints,
  maintenance-margin tiers and mark-price series.
- An opt-in calibrated OHLCV execution proxy with one shared bar-volume budget,
  incremental partial fills, stable order identity, remainder policies,
  protection resizing, latency timestamps and restart reconciliation.
- Confirmed-only higher-timeframe aggregation and live-session metadata for
  warm-up coverage and required timeframe availability.
- Versioned backtest execution-capability negotiation and a deterministic
  cross-runtime conformance matrix with reason-coded difference waivers.
- Public parity fixtures for favorable long/short limit gaps and conservative
  same-bar entry/protection ambiguity.
- `koval backtest --exchange-type {spot,future}` threads the venue market
  through the data adapter, the OHLCV cache and the fee model. Omitted, it
  keeps each adapter's native market (binance: future, whitebit: spot).
- Built-in WhiteBIT **spot** fee schedule (0.10% maker / 0.10% taker). A
  `whitebit`/`future` run still raises for explicit `maker_fee_bps` /
  `taker_fee_bps` — the public perpetual figure was not pinned to a primary
  source.
- Public parity fixture `long_leverage_spread_rejected_v1`: an entry that
  clears the nominal affordability check but not its adverse-adjusted fill.

### Changed

- The OHLCV cache key is `(exchange, market, symbol, timeframe)` and its layout
  is `<root>/<exchange>/<market>/<SYMBOL>_<timeframe>.parquet`. A pre-market
  file is adopted once, under the market its adapter used to serve.
- `resolve_execution_settings` raises for a venue and market with no built-in
  fee schedule instead of resolving to 0 bps. Pass `maker_fee_bps` and
  `taker_fee_bps` explicitly.
- A take-profit whose bar opens beyond the target now uses that open as the
  fill reference in both paper profiles. Legacy results improve slightly for
  that case; every other legacy number is unchanged.
- Risk gates distinguish daily loss from all-time peak drawdown, use an explicit
  prior-equity daily baseline, and consume actual fill quantities, prices and
  costs from the broker's authoritative ledger.
- Paper execution can normalize orders against time-valid instrument evidence,
  settle funding before same-timestamp orders, evaluate liquidation from mark
  price, and carry fee evidence on fills and ledger entries.
- `PollingFeed` supervises sandbox orders while market-data requests block or
  back off, and stops after a bounded consecutive-failure budget. Binance
  sandbox signed requests support measured midpoint clock-skew correction.
- Adapter and broker factories enforce one venue/market/mode matrix. Binance
  futures is the only sandbox execution path; spot paper rejects shorts and
  leverage above one, and WhiteBIT execution remains disabled.
- `PaperBroker` re-checks affordability against the actual adverse-adjusted
  fill price inside `process_bar`, not only the nominal price at submission.
  An entry that can no longer be afforded at fill time is dropped and reported
  as `ORDER_REJECTED`; the session keeps running.
- `LiveEngine._on_open` records the position's real debited margin in
  `PlatformAccountState`, so `snapshot().margin_used` / `.free_margin` and the
  status report reflect a leveraged position for its whole life. The paper
  broker carries it on the entry `Fill`; the Binance sandbox derives it from
  `/fapi/v2/positionRisk` leverage, refreshed right after an entry fills.
- The `insufficient_margin` "keep trading" soft-rejection is now scoped to the
  paper broker. Any other broker's rejected entry ack halts entries and raises
  an incident, as before.

### Fixed

- Live trade records report the prices that actually filled, not the setup's
  requested prices, and carry `entry_reference_price`, `exit_reference_price`,
  `gross_price_pnl`, `commission`, `execution_costs` and `exit_reason_text`.
  `pnl` is net of both commissions.
- A live session with a configured `higher_timeframe` no longer aborts when the
  rolling window does not begin on a higher-timeframe boundary — which is every
  real feed, and every window once it starts sliding. A bucket the window only
  partly spans is a coverage boundary and is skipped; a hole inside a fully
  spanned bucket still fails.
- `MarkPriceSeries.at` is indexed instead of scanning, so mark-price liquidation
  no longer makes a run quadratic in its own length: one lookup per bar over 30
  days of one-minute marks cost ~40s before, and is now constant-time.
- `PaperBroker` resumes funding settlement from a cursor rather than rescanning
  every record on every bar.
- Funding coverage is validated against the phase the records reveal instead of
  an assumed epoch-aligned grid, so a venue settling at an offset is no longer
  rejected. A window narrower than one interval may declare `interval_ms` and
  hold no settlement; a window spanning a whole interval with no record is still
  incomplete evidence.
- `sandbox_factory.build_broker` measures Binance server-time skew when it builds
  the session's broker, so signed requests are corrected without a caller
  remembering to call `synchronize_clock`. Skew beyond the supported bound raises
  `ClockSkewExceeded`; an unreachable time endpoint does not block the session.
- `compare_execution_results` compares numbers within a documented relative
  tolerance instead of demanding bit-identical floats. Two runtimes computing the
  same fee in a different operation order disagree in the last bits for roughly
  half of realistic inputs, which the cross-runtime gate was reporting as a
  parity difference. Pass `relative_tolerance=0.0` for bit equality.
- Realistic-v2 market entries that gap completely beyond their bracket are
  contained at the contemporaneous market reference instead of receiving an
  impossible fill at an unreachable protective level.
- Confirmed dynamic stops remain tightened when a later partial entry fill
  resizes protection, and account snapshots mirror the confirmed stop.
- Manual closes without bar-volume evidence retain the disclosed fixed
  slippage fallback instead of receiving zero calibrated impact.
- Protection activation latency remains in force across multiple bars, and a
  final strategy callback receives aggregate net PnL after partial exits.
- `WhiteBITAdapter` futures symbol resolution: `BTCUSDT`, `BTC/USDT` and
  `BTC_USDT` all map to `BTC_PERP`; an unparseable pair raises instead of
  querying the wrong venue symbol.
- The Binance sandbox reads a fill's quote asset from the cached `exchangeInfo`
  `quoteAsset` field instead of guessing from a `USDT`/`USDC`/`BUSD` suffix, so
  a non-USDT quote (FDUSD, TUSD, …) no longer misattributes commission or
  corrupts `venue_realized_pnl`.
- Fee resolution rejects an unrecognised market string the same way the
  adapter factory and cache do, instead of silently treating it as spot.

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

[Unreleased]: https://github.com/koval-finance/koval-engine/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/koval-finance/koval-engine/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/koval-finance/koval-engine/releases/tag/v0.10.0
[0.9.0]: https://github.com/koval-finance/koval-engine/releases/tag/v0.9.0
