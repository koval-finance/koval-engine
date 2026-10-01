# Graph contracts

Purpose: the typed dataflow engine's rules. This is the highest-risk area of
the repository — contracts here are public API that third-party graphs and
plugins depend on. Plan first, change little.

## Domains

`Domain` (`src/koval/strategy/graph/domains.py`) has six members, each with a
rank. `SOURCE` (rank 0) is internal — it feeds market data into the graph; no
author-written node currently uses it. The five domains an author writes
nodes for:

| Domain | Role |
|---|---|
| FACT | compute observations from market data — no opinions |
| STATE | remember things across bars |
| POLICY | decide whether and how to act |
| INTERPRETATION | score and explain — the "why" surfaced with a trade |
| EXECUTION | turn a decision into a `TradeSetup` with risk applied |

Domains are rank bands, not a strict pipeline: FACT and STATE share a rank,
so edges between them are legal in either direction (a Liquidity Pool fact
built from events; a Sweep fact born from active pool state). Execution runs
in rank/topological order; an edge may not point to a domain whose rank is
strictly lower than its producer's.

## Where each contract lives

| File | Contract |
|---|---|
| `src/koval/strategy/graph/entities.py` | entity types carried on edges |
| `src/koval/strategy/graph/domains.py` | the domain enum and its ordering |
| `src/koval/strategy/graph/ports.py` | port declarations and compatibility |
| `src/koval/strategy/graph/node.py` | the node ABC |
| `src/koval/strategy/graph/registry.py` | node registration |
| `src/koval/strategy/graph/validation.py` | structural checks: cycles, port compatibility, domain ordering |
| `src/koval/strategy/graph/executor.py` | runtime execution and emitted-entity checking |
| `src/koval/strategy/graph/state_store.py` | per-node persistent state |
| `src/koval/strategy/graph/compat.py` | the compatibility catalogue mapping blocks onto typed nodes |
| `src/koval/strategy/graph/strategy.py` | the graph-backed strategy |

## Validation boundaries

- **Assembly time** (`validation.py`): structure, port compatibility, domain
  ordering, cycle detection. A bad graph fails with `GraphValidationError`
  before any bar runs.
- **Runtime** (`executor.py`): each node's emitted entities are checked
  against its declared output ports. A node lying about its outputs fails
  loudly, not silently.

## Rules

- Never widen a port type or an entity contract to make one graph pass; that
  weakens every graph. Fix the graph, or add an explicit new port.
- A new node needs tests at both boundaries — a validation test and an
  executor test — plus golden coverage if it changes an existing preset's
  behaviour — see [testing.md](testing.md).
- The compatibility catalogue in `compat.py` is a public promise; changing an
  existing mapping is a breaking change and needs a changelog entry.

Update this file when: a domain, entity, or port contract changes; validation
gains or loses a check.

## Optional historical indicator preparation

`build_graph_strategy()` instances expose `prepare_backtest(candles, *,
history_bars)`. The host calls it once per instance with the actual primary
`(N, 6)` feed after runtime-boundary trimming, including retained warmup. It must
then inject chronological float64 windows of that same feed. This optional
capability does not change `EngineRunSpec` or protocol negotiation.

`graph/indicators.py` owns per-run tables for built-in EMA/RSI/ATR nodes. The
`BarContext.indicators` field defaults to `None`; a prepared context receives
only its current timestamp's scalar pairs after timestamp/history-size checks.
Nodes and state stores still execute sequentially. Other nodes, paper/sandbox
runtimes, older hosts and invalid feeds use the existing scalar path.

`helpers/rolling_indicators.py` accepts normalized float64 price arrays. It
vectorizes independent windows, keeping each window's serial operations and
seed exact. The previous crossover value belongs to the current window, not
the previous rolling window. Work remains O(bars × history); NumPy removes
per-element Python execution. Temporary vectors are bounded by a 4,096-window
tile, and tables occupy O(bars × distinct requested indicators). Tests compare
exact helper values, causal prefixes, state transitions and dtype normalization.

## Optional decision evidence and chart series

`BarContext.evidence` observes the current evaluation through `DecisionRecorder`.
It produces version `koval_trade_decision_context_v1`, copied to
`StepResult.decision_context` and then `TradeSetup.decision_context`. Founder
RSI/EMA facts, MACD/RSI/EMA/ATR policies and states, native aggregation/scoring,
and order risk expose actual parameters, scalar values, predicates and results.
Unsupported observations stay `partial` or `unavailable`; an empty context must
never be presented as a passed condition. No OHLCV arrays are embedded.

Legacy compilation retains source block IDs. Native temporal aggregation keeps
the original contributing fact snapshot and timestamp, including when a later
bar completes the chain. Consumers must distinguish signal candle open time,
decision time, and subsequent execution times. Recording does not rerun nodes
or change their mathematical predicates. Compatibility entry indicators and
reasons are projections of the same observations.

For a step that emits orders, `StepResult.decision_context` belongs to the first
terminal order, matching `GraphStrategy`'s existing selection. Risk is recorded
under the constructing node, and only that order's emitted-input dependencies
appear in the context, entry indicators, and fallback reasons. An unrelated
intent cannot supply `why_entry`. Disconnected nodes and other terminal orders
remain in `StepResult.entities_by_node` for diagnostics but do not affect the
selected trade's evidence status. A step without orders retains all observed
nodes as diagnostic context and has no selected-order risk.

Temporal aggregators select evidence from the same completed chain they emit,
including its saved upstream observations. They exclude competing chains and
current observations that did not contribute to the chosen chain. Observation
identity is `(runtime_node_id, timestamp_ms)`, so repeated occurrences of one
fact remain distinct. Retention and exported snapshots are detached copies.
This selection changes neither the entity/port contracts nor order execution;
the decision-context version remains `koval_trade_decision_context_v1`. Native
execution nodes without risk instrumentation still expose missing risk, never
another order constructor's risk.

Connected inputs are a default dependency set, not proof that a node consumed
them. A node that ignores or selects inputs must call `observe(ctx, inputs=...)`
with the entities actually used; an empty collection clears dependencies.
Built-in policy filters and trend/volatility states calculate from `BarContext`
and ignore their optional `context` ports. Context scoring records the last
state of each kind that supplies a scoring key, matching the calculation;
earlier duplicates and ignored state kinds/statuses are excluded. Temporal
selection continues to use `retain(...)` for observations saved across bars.

`koval.strategy.graph.series.strategy_indicator_series` is the public derived
chart API. It resolves saved graph defaults and uses the node formulas with the
same bounded history (default 1000). Points have timestamps and null warmup
values; repeated indicator types retain separate node IDs. It never invokes the
strategy evaluator and must not be labeled recorded decision evidence. Callers
must check the engine identity and compare overlapping recorded observations.
Tests: `test_decision_evidence.py` and `test_strategy_series.py` under
`tests/strategy/graph/`.
