# Graph contracts

Purpose: the typed dataflow engine's rules. This is the highest-risk area of
the repository — contracts here are public API that third-party graphs and
plugins depend on. Plan first, change little.

## Domains

Native `exec.spot_dca` / `exec.spot_grid` use a separate `SpotRecipeRequest`
terminal and confirmed `SpotRecipeStatus` feedback. They cannot be mixed with
single-entry/position-exit terminals. See [bounded Spot execution](spot_multi_order.md)
for their required ports, parameters, static host requirements and paper API.

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
| EXECUTION | construct entry orders with risk or identified position-close requests |

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
RSI/EMA facts, MACD/RSI/EMA/ATR/cooldown policies and states, native
aggregation/scoring, and order risk expose actual parameters, scalar values,
predicates and results.
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

Two policies need more than a single threshold. `policy.atr_volatility` allows a
bar when `atr != 0`, `close != 0`, `atr_pct >= min_atr_pct` and, when the optional
`max_atr_pct` is set, `atr_pct <= max_atr_pct`. It blocks with
`volatility_too_low` or `volatility_too_high`. Its parameters omit `max_atr_pct`
from `model_dump()` while it is unset, and the evidence values and predicate
name the cap only when it is set, so graphs saved before the cap keep their
serialized form. `policy.cooldown` (`bars >= 1`) keeps a flat-bar count in its
`NodeState`. It blocks with `cooldown_active` while `BarContext.position_size`
is non-zero and for the first `bars` flat bars afterwards, counting the bar
whose close first shows the position gone as flat bar 1. It always allows
before the first position. It relies on the host evaluating the graph on every
bar, in a position or flat, and it cannot see an entry and exit inside one bar.

`exec.order_constructor` takes an optional `trail_pct` (`> 0`, `< 100`) for a
native trailing stop. The node itself only tags the order with
`metadata["trail_pct"]`; the initial stop still comes from `sl_pct`, and size and
target do not change. While the position is open, `GraphStrategy.on_sl_update`
calls `trailing_stop_price` with the bar close: a long stop only rises, a short
stop only falls, and the hook returns `None` when the stop does not move. The
trail is evaluated once per closed bar, so intrabar highs do not move it. The
parameters omit `trail_pct` from `model_dump()` while it is unset, and the order
metadata stays empty, so graphs saved before 0.12.4 keep their serialized form.
`build_graph_strategy` raises `GraphValidationError` when a graph sets
`trail_pct` and a legacy `dynamic_exit` callable is also supplied. Legacy
`exit.trailing_stop` and `exit.breakeven` blocks stay on the GraphStrategy seam
through `extract_dynamic_exit`; there is no `exec.dynamic_stop` node, no
activation threshold. Take-profit remains a bracket by default; explicit
historical disabling is described below.

## Historical position management

`exec.position_exit` (`Close Position`) is an EXECUTION terminal with empty,
strict params. Its required input `condition: PolicyDecision` accepts one
connection; its terminal output `exit: PositionExitRequest` is separate from
entry `OrderRequest`. It emits only when the consumed decision allows, position
size is positive, direction is `long`, and `BarContext.position_id` is present.
False/missing conditions, flat/short positions and missing identity emit `None`.
The existing `policy.ema_trend` with `direction="bearish"` supplies strict
`close < ema`; equality or insufficient history permits no exit. The MIT
terminal accepts any compatible policy; application hosts may restrict authors.

Import `PositionExitRequest` from `koval.strategy.graph.entities`. It inherits
`bar_index`, `timestamp_ms`, `source_node_id` and adds:

| Field | Contract |
| --- | --- |
| `symbol` | Current configured symbol |
| `position_id: int` | Run-local identity of the current position |
| `position_side` | Literal `"long"` |
| `quantity_fraction` | Literal `1.0`; host resolves actual remaining quantity |
| `order_type` | Literal `"market"` |
| `reason` | Literal `"signal"` |
| `decision_context` | Detached decision observations, or `None` without recording |

Only unconsumed terminal outputs populate `StepResult.position_exits`; entry
orders stay in `StepResult.orders`. Each exit's own context contains only its
consumed condition and terminal, including EMA/close values and source/timing
identity. `StepResult.decision_context` and `TradeSetup.decision_context` retain
selected-entry semantics. No entry snapshot is overwritten. Evidence remains
`koval_trade_decision_context_v1`.

`BarContext.position_id: int | None = None` preserves old callers.
`GraphStrategy.on_open_position(trade_id, setup)` assigns identity; the matching
close hook clears it. A late close callback for a different currently open
identity is ignored. Hosts synchronize position size/direction before `on_bar`,
including while open. `DeclarativeStrategy.get_position_exit_request() ->
PositionExitRequest | None` defaults to `None`. GraphStrategy returns a detached
copy of the first terminal exit from the cached step, suppressing an intent if
its position has closed or been replaced. Entry/exit queries and `on_bar` share
one evaluation per candle. Repeated queries are intents, not acknowledgements:
the execution host owns pending-close deduplication and fills.

### Optional take-profit

`exec.order_constructor` adds `take_profit_mode: Literal["bracket", "disabled"]`
with default `"bracket"`, propagated through `OrderRequest` and `TradeSetup`.
Bracket retains explicit `tp_pct` or RR fallback when `tp_pct=None`. Disabled
emits a real `target_price=None` / `take_profit=None`; RR does not determine a
target. Non-null `tp_pct`, request `target_price`, or setup `take_profit`
conflicts with disabled and fails validation. Initial stop/sizing are unchanged.
This mode is authored on the native constructor; legacy exit blocks and the
discrete pricing pipeline retain bracket semantics.

Missing and explicit bracket are omitted from constructor parameter and
OrderRequest dumps, preserving old JSON, normalized graph/default hashes and
entry evidence. Disabled is explicit. Adding an explicit field to raw JSON
changes its raw hash until normalized; no saved graph is rewritten.
`TradeSetup(take_profit=None)` without disabled keeps the legacy host's RR
fallback contract. Its new field is appended to preserve positional callers.

### Host requirements

`DeclarativeStrategy.required_execution_capabilities() -> frozenset[str]`
defaults to empty. GraphStrategy requires `position_exit_v1` for an exit terminal
and `optional_take_profit_v1` for a disabled constructor. The pure
`graph_execution_capabilities` in `graph/execution_capabilities.py` derives the
same set without stepping nodes or changing saved input. MIT
`negotiate_execution_capabilities` adds graph requirements to explicit
`EngineRunSpec` requirements. Plugins must call negotiation before execution
and advertise only implemented features. Protocol versions are unchanged;
entry execution does not imply either capability.

MIT `LiveEngine` implements neither feature. It rejects required capabilities
immediately after assembly, before broker construction/access, feed iteration,
session callbacks or journal records. Injecting a broker does not opt it in.
Setup validation also rejects disabled TP from a custom strategy. Existing
bracket graphs remain supported. This is a historical intent contract, not
live/paper or exchange-sandbox enablement.

The historical host owns next-available-open execution after the decision,
position identity checks, pending-close lifecycle, partial-fill protection and
exit evidence attribution. The MIT executor does not simulate fills or certify
those behaviors. See [runtime requirements](runtime_contract.md) and
[public usage](../README.md).

`koval.strategy.graph.series.strategy_indicator_series` is the public derived
chart API. It resolves saved graph defaults and uses the node formulas with the
same bounded history (default 1000). Points have timestamps and null warmup
values; repeated indicator types retain separate node IDs. It never invokes the
strategy evaluator and must not be labeled recorded decision evidence. Callers
must check the engine identity and compare overlapping recorded observations.
Tests: `test_decision_evidence.py` and `test_strategy_series.py` under
`tests/strategy/graph/`.
