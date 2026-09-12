# Architecture

Purpose: the map — what lives where and how a bar flows through the system.
For the typed-graph contracts specifically, see
[graph_contracts.md](graph_contracts.md).

## Modules

| Path | Owns |
|---|---|
| `src/koval/engine/backtest_engine.py` | the plugin protocol plus versioned execution-capability negotiation |
| `src/koval/engine/market_identity.py`, `run_identity.py` | canonical market vocabulary, evidence content hashes, and actual-input run identity |
| `src/koval/engine/protection.py` | shared validation of a dynamic protective-bracket snapshot |
| `src/koval/exchanges/binance_algo.py` | conditional-order translation and actual child-order lookup through the sandbox transport |
| `src/koval/engine/live_engine.py` | the bar loop for paper and sandbox sessions |
| `src/koval/engine/live_feed.py` | replay/polling feeds, continuity checks, bounded outage retries, and between-bar supervision |
| `src/koval/engine/paper_broker.py` | simulated bracket/OCO fills with pessimistic ambiguous-bar resolution |
| `src/koval/engine/account_ledger.py` | append-only cash movements and reconciliation equations |
| `src/koval/engine/account_state.py` | account snapshots, explicit daily baseline, peak drawdown, and position state |
| `src/koval/engine/execution_conformance.py` | shared deterministic scenario generation and cross-runtime result comparison |
| `src/koval/engine/execution_proxy.py` | partial-fill volume budget, lagged impact evidence, and latency timeline |
| `src/koval/engine/fee_evidence.py` | historical/current/approximate maker-taker fee evidence |
| `src/koval/engine/funding.py` | normalized perpetual-funding evidence and cashflow signs |
| `src/koval/engine/higher_timeframe.py` | confirmed-only causal aggregation of higher-timeframe bars |
| `src/koval/engine/instrument_risk.py` | historical instrument rules, mark prices, maintenance margin, and liquidation |
| `src/koval/engine/market_data.py` | immutable canonical OHLCV encoding, identity, validation, and hashing |
| `src/koval/engine/history_window.py` | `DEFAULT_HISTORY_BARS` — the one candle window every runtime injects |
| `src/koval/engine/paper_profile.py` | immutable v1 profiles and the conservative `paper_ohlcv_realistic_v2` policy |
| `src/koval/engine/paper_fills.py` | pure fill, cost-split, commission and margin arithmetic |
| `src/koval/examples/parity/` | the public golden execution fixtures every runtime must reproduce |
| `src/koval/engine/trade_metrics.py` | performance metrics over closed trades |
| `src/koval/strategy/base/` | `DeclarativeStrategy` ABC, `TradeSetup`, `EntryConfig` |
| `src/koval/strategy/helpers/` | pure block functions (signals, filters, exits, risk, interp) |
| `src/koval/strategy/graph/` | the typed dataflow engine — see [graph_contracts.md](graph_contracts.md) |
| `src/koval/strategy/nodes/` | typed nodes wrapping the helpers |
| `src/koval/strategy/registry.py` | `BLOCK_CATALOG` and `STRATEGY_REGISTRY` |
| `src/koval/strategy/schemas.py` | Pydantic parameter models per block |
| `src/koval/strategy/block_assembler.py` | JSON graph in, runnable strategy out |
| `src/koval/exchanges/` | data adapters, OHLCV cache, sandbox brokers — see [exchanges_and_data.md](exchanges_and_data.md) |
| `src/koval/cli/` | the `koval` console script (`koval --help` for the commands) |

Tests mirror this layout under `tests/`.

## Execution flow

1. An archived canonical dataset or live feed supplies validated, chronological
   bars. A live feed emits only closed bars and supervises working sandbox orders
   while polling or retrying market data.
2. At each timestamp the paper broker settles funding, evaluates mark-price
   liquidation, applies already-active protection, and then evaluates entry work
   under the profile's documented equal-timestamp policy. A fill that is already
   beyond its bracket is immediately contained at the same market reference,
   never at a protective level the bar did not trade through after entry.
3. The engine injects market state into the strategy — chronological numpy
   arrays, `arr[-1]` = current bar.
4. Confirmed higher-timeframe inputs are aggregated only when their complete
   source interval ended no later than the primary decision time. A rolling live
   window opens wherever the feed did, so a bucket the window only partly spans
   is skipped as a coverage boundary; a hole inside a fully spanned bucket still
   fails.
5. The strategy's graph executes in topological order, constrained by domain
   rank; policy output becomes a `TradeSetup` or nothing.
6. Risk gates size or veto the setup using the account snapshot derived from the
   broker ledger. The realistic profile rechecks requested risk and margin at the
   actual normalized fill.
7. The broker receives the order with its mandatory bracket. Fill deltas, fees,
   funding, margin release, liquidation, and final PnL reconcile back through the
   same account state.

## The plugin seam

Backtesting is not implemented here. `koval backtest` discovers engines
through the `koval.backtest_engines` entry-point group and speaks
`BacktestEngineProtocol` to whichever is installed. The protocol carries a
version, pinned by
[`tests/engine/test_protocol_version.py`](../tests/engine/test_protocol_version.py).
This is a licence boundary as much as an API: implementations choose their
own dependencies and licence, and nothing in this package may import them by
name.

Market-aware data/cache identity, all paper profiles, evidence contracts, and
public parity fixtures live in this package. Historical execution still belongs
to the installed plugin. `EngineRunSpec.execution_contract_version` and
`required_execution_capabilities` must be negotiated against the plugin's
`ExecutionCapabilities`; an unsupported version or feature is a hard error.

A second runtime proves parity through three published entry points:

- `koval.examples.parity_fixtures()` — golden fixtures naming the candles,
  execution assumptions, expected fills and final equity every runtime must
  reproduce. They ship inside the wheel; `examples/parity/CHECKSUMS.txt` pins
  their bytes.
- `koval.engine.execution_conformance.generate_conformance_scenarios()` — the
  wider deterministic matrix (both directions, every entry type, gaps,
  ambiguity, cancellation, dynamic protection, insufficient margin).
- `koval.engine.execution_conformance.compare_execution_results()` — the
  comparison itself. Numbers are compared within `relative_tolerance` (default
  `DEFAULT_RELATIVE_TOLERANCE`), because two implementations of the same
  arithmetic disagree in the last bits for roughly half of realistic inputs
  purely from operation order; everything else must match exactly. A runtime
  difference that is intentional needs an `IntentionalDifference` waiver with a
  reason code, and an unused waiver is itself a failure.

Runtime equality is meaningful only when both runtimes consume the same
canonical candle bytes, evidence intervals, profile version, and parameters.
It establishes deterministic contract parity, not historical order-book parity.
The current liquidation primitive is deliberately limited to one cross-margin
position using quote-currency collateral; isolated and portfolio margin require
separately versioned evidence and behavior.

The current runtime calls both dynamic protection hooks after matching each bar.
Its shared vocabulary, evidence transport, identity encoding, spot-refusal policy,
and plugin adoption rules are specified in [runtime_contract.md](runtime_contract.md).
Review findings and source rationale are in [realism_review.md](realism_review.md).

`run_boundaries.py` owns the optional common warmup/evaluation and initial-risk
contract; `runtime_journal.py` owns ordered archive records and integrity checks.
LiveEngine remains independent of host storage. See
[runtime_contract.md](runtime_contract.md) for capability negotiation and archive
boundaries, and [realism_review.md](realism_review.md) for acceptance limits.

Update this file when: a module is added, moved, or renamed; the execution
flow gains or loses a stage; the plugin protocol changes.
