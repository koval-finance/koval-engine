# Architecture

Purpose: the map — what lives where and how a bar flows through the system.
For the typed-graph contracts specifically, see
[graph_contracts.md](graph_contracts.md).

## Modules

| Path | Owns |
|---|---|
| `src/koval/engine/backtest_engine.py` | `BacktestEngineProtocol`, `EngineRunSpec`, `BacktestResult` — the seam a backtest plugin implements |
| `src/koval/engine/live_engine.py` | the bar loop for paper and sandbox sessions |
| `src/koval/engine/live_feed.py` | `LiveFeed` protocol, `ReplayFeed`, `StopSignal` |
| `src/koval/engine/paper_broker.py` | simulated bracket/OCO fills with pessimistic ambiguous-bar resolution |
| `src/koval/engine/account_state.py` | per-engine account bookkeeping |
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

1. A feed (a `LiveFeed` implementation, or a backtest plugin's data source)
   produces bars.
2. The engine injects market state into the strategy — chronological numpy
   arrays, `arr[-1]` = current bar.
3. The strategy's graph executes in topological order, constrained by domain
   rank; policy output becomes a `TradeSetup` or nothing.
4. Risk gates size or veto the setup.
5. The broker (paper or sandbox) receives the order with its mandatory
   bracket; fills come back as events.

## The plugin seam

Backtesting is not implemented here. `koval backtest` discovers engines
through the `koval.backtest_engines` entry-point group and speaks
`BacktestEngineProtocol` to whichever is installed. The protocol carries a
version, pinned by
[`tests/engine/test_protocol_version.py`](../tests/engine/test_protocol_version.py).
This is a licence boundary as much as an API: implementations choose their
own dependencies and licence, and nothing in this package may import them by
name.

Update this file when: a module is added, moved, or renamed; the execution
flow gains or loses a stage; the plugin protocol changes.
