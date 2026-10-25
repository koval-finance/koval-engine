# Frozen historical levels

`LevelSnapshotV1` and STATE rank 1 `state.level_setup` freeze causal price bounds
on a trigger close, then emit at most one later bullish `level_opportunity`.
The opportunity is a `MarketEvent`, not an order, fill, stop or position exit.
Existing interpretation, policy, direction and execution nodes remain the consumers.
No detector, risk reference, stop/sizing implementation or migration is included.

## Public interfaces

- `koval.strategy.graph.entities.LevelSnapshotV1`: strict, frozen entity schema.
- `koval.strategy.graph.level_state.LevelSetupParamsV1`: strict parameter schema.
- `koval.strategy.nodes.levels.level_setup_evaluator(params, *, node_id="state.level_setup", graph_binding_hash=None) -> NodeEvaluate`.
  GraphExecutor binds the actual graph instance ID. Direct factory calls use the type ID.
- `koval.strategy.nodes.levels.validate_level_setup_state(params, state, *, node_id="state.level_setup", graph_binding_hash=None) -> None`.
- `koval.strategy.graph.levels.level_capabilities() -> dict`.
- `koval.strategy.graph.levels.graph_level_history_requirements(graph: dict) -> dict`.
- `koval.strategy.graph.levels.level_event_binding(ctx, level: LevelSnapshotV1, event: MarketEvent | None) -> bool`:
  a structural same-event, same-decision oracle for a future consumer; no execution permission.

Input ports are `trigger: MarketEvent` (one required connection),
`lower: NumericValueV1` (one required connection), and `upper: NumericValueV1`
(zero or one connection). A connected trigger can produce no event on a bar:
its empty runtime list still services an existing setup. There is no trigger OR/fan-in.
Outputs are `level: LevelSnapshotV1` (always an entity) and
`event: MarketEvent | None` (nonterminal). Feedback cycles are refused.

Required parameters have no defaults:

| Parameter | Contract |
| --- | --- |
| `mode` | `sweep_reclaim`, `breakout_retest`, or `breakout` |
| `expires_after_bars` | Strict integer 1..400 |
| `invalidation_buffer_pct` | Finite number 0 inclusive to 100 exclusive |
| `tolerance_pct` | Required finite 0 inclusive to 100 exclusive for `breakout_retest`; forbidden otherwise, including null |

`rearm` is fixed to `new_trigger_after_terminal`, also its sole default.
Booleans, extras, unsupported variants and nonfinite parameters fail. New explicit
parameters affect new graph identity; old entity/default/normalized dumps stay unchanged.

## Bounds and clocks

Ready bounds are finite positive `price_quote_per_base`, with the same primary
symbol/timeframe. The source entity must belong to the current primary cursor;
`as_of_ms` can legitimately be older for offsets or previous-channel values.
Availability cannot exceed the actual decision timestamp. Missing/unready, stale,
future, zero, negative or inverted bounds cannot arm; identity/unit conflicts raise
before state mutation. Static source units and cardinality are checked at assembly.

Absent upper means upper equals lower; otherwise upper must be at least lower.
Center is their arithmetic mean (preserving subnormal prices and avoiding intermediate overflow). Retest
requires a single level: `lower` receives the previous channel upper; omit `upper`.
The caller owns the causal close-cross trigger. Breakout may use a held compression
rising edge; no range-detector dependency is imposed on these explicit producers.
A future detector must provide causal confirmed bounds and reasons. Its score would
remain a measurement, not a calibrated probability; no detector formula is supplied.

`BarContext.timestamp_ms` is the signal candle open. `decision_timestamp_ms` must
be at or after its close and before the next close, with an aligned declared primary
history span. Current OHLC must be finite positive and internally consistent, and
the current closes array must agree. A primary gap, missingness or discontinuity
invalidates an armed setup. The host remains responsible for individual timestamps
inside historical arrays, which BarContext does not carry. No feed registry, alternate
symbol, HTF binding or observed network-receive clock is introduced.

A trigger's legacy base timestamp/index must equal the current cursor and its kind
and source must be nonempty. Stale/future triggers do not arm and are recorded with
a reason. Malformed types, non-JSON/nonfinite payloads and identity conflicts raise.
Legacy metadata is opaque provenance, not an additional clock authority. The trigger
key is SHA256 of its entire canonical legacy dump (sorted compact JSON, finite values).
No new default ID field is added to legacy events. The level ID hashes formula version,
owner node ID, trigger key and frozen source hash. Frozen NumericValueV1 dumps retain
source ID, formula, input cursor, measurement and availability clocks.

Later live sources cannot move frozen bounds and need not remain ready or equal.
An armed owner ignores new triggers, records why, and keeps the original deadline.
A failed rearm preserves the latest terminal snapshot and records its failure reason.

## Lifecycle and exact predicates

Before the first valid trigger: `status=unavailable`, a non-ready readiness/reason,
and null ID, bounds and frozen provenance. There is no synthetic zero or default price.
A valid trigger at close t only arms. Eligible decisions are t+1..t+M inclusive;
expiry is checked before processing t+M+1. Identical duplicates never age a setup.

On an eligible later bar, missing/gapped primary data invalidates first, followed by
strict `open < lower*(1-buffer/100)` or `close < lower*(1-buffer/100)`.
Equality alone does not invalidate. Entry predicates then use closed-bar data:

| Mode | Bullish opportunity |
| --- | --- |
| `sweep_reclaim` | `low < frozen_lower` and `close > frozen_lower` |
| `breakout_retest` | `low <= frozen_level*(1+tolerance/100)` and `close > frozen_level` |
| `breakout` | Consecutive valid `previous_close <= frozen_upper` and `close > frozen_upper`; reuses `cross_direction`, with no epsilon |

Consumption is stored before the event returns. A downstream veto still consumes;
there is no later retry without a new trigger. Terminal `consumed`, `expired` and
`invalidated` states never resurrect. A valid new trigger on a strictly later cursor
can create a new ID, and that setup also waits at least one later bar.

An emitted event identifies level/event IDs, consumption decision cursor, frozen
bounds/source/trigger hashes, formula/mode/predicate, actual primary prices and the
previous primary close/cursor. A consumed snapshot also contains that event binding.
`level_event_binding` refuses a mismatched ID/provenance or later decision. It does
not authorize a future risk consumer to reuse a consumed snapshot by itself.

## Checkpoints, evidence and limits

State is plain post-bar data, version 1, with config/formula/node binding, a complete
latest snapshot, last primary close/cursor/payload hash and a monotone trigger
watermark. Graph-built states also bind normalized upstream parameters/connections
and a fingerprint of the immutable primary decision input. Moving source windows
are data, not graph configuration. Retained recorder observations have strict field
and identity types. An integrity hash detects changed checkpoint fields; it is not an
authentication signature. Restore validates before mutating GraphStrategy state.
A noninitial level graph checkpoint requires the complete exported state map and
one shared primary cursor/identity/input fingerprint across owners and coherent
temporal ancestor cursors; an initial checkpoint must be empty.
Crossing ancestors must retain their versioned previous-operand state at that
same cursor, including primary identity, source-port bindings, compatible units
and decision-time availability. A missing or incoherent cross state refuses
before any restored state is applied; warmup checkpoints may retain null operands.

Dedup scope is precisely the latest decision payload plus the monotone primary
trigger cursor. An identical duplicate yields the saved snapshot and no event;
conflicting OHLC, decision clock, trigger or bound payload refuses. Backward clocks
or primary indices and rebound identity refuse. GraphExecutor preflights duplicates
before evaluating upstream temporal nodes: an identical repeat returns stable level
snapshots and null outputs for other/transient ports, with no orders or state drift.
GraphStrategy preserves repeated immutable intent queries within the cached decision;
it refuses changed primary OHLCV/history/identity/clocks, including after restore.
Mutable account/position feedback is outside this immutable price-input fingerprint.
Historical triggers cannot rearm at
a later cursor because their original timestamp/index is stale. This is not an
unbounded registry of arbitrary caller IDs across rewritten cursors or graph runs.

One setup retains its original arming evidence through the existing recorder.
Later opportunities depend on those observations, not changing bound observations.
State stores no OHLCV windows or list of prior setups: O(1) lifecycle/dedup records
plus O(the fixed arming dependency graph's evidence). Payload size still depends on
caller provenance size and graph size. A rolling 1000-row stream with 20,004 bars /
10,002 setups exercises stable bounded state. Source-window sliding is not rebinding.

Physical retained source history (1000), numeric period max (400), host warmup
(default 400, option 0..1000 in the consuming application), expiry and temporal replay
prefix are different quantities. Engine discovery does not set the host's warmup.
Longer streamed prefixes are not clamped to 1000 and do not require larger arrays.
The level history report composes event-port latency and preserves unknown legacy
requirements as null. Existing numeric and condition-history schemas remain unchanged.
Ideal readiness/opportunity bounds assume valid qualifying inputs; runtime delay can
be indefinite. A valid restored post-bar state replaces its consumed prefix only.

Semantic report schema is `koval_historical_levels_capabilities_v1`, history schema
`koval_level_history_v1`, formula `level_setup_v1`, entity/state versions 1.
The report exposes input/output/parameter/entity schemas, limits and all policies.
`numeric_values_v1` remains a necessary generic execution capability; it is insufficient
for level admission. Both `adapter_execution_verified` and `adapter_resume_supported`
are false. The engine claims primary `historical_ideal_close_v1` semantics only.
Paper, sandbox, Spot Testnet, live, recipes, HTF and cross-symbol use are unsupported.

## Portable examples and receiving-runtime proof

Package assets:

- `examples/runtime/historical_levels_v1.json`: literal lifecycle/expiry/invalidation/retest/cross oracles,
  schema `koval_historical_levels_fixture_v1`.
- `examples/runtime/historical_level_graph_v1.json`: held compression, frozen channel and RVOL policy graph,
  schema `koval_historical_level_graph_fixture_v1`, plus the separate unverified
  `koval_historical_level_plugin_scenario_v1` scenario.
- `examples/graphs/historical_frozen_level.json`: the complete graph using existing fixed risk and signal exit.

The integration fixture explicitly uses RVOL period 2: prior volumes 100,100 and
current 200 yield ready 2; current 50 yields ready 0.5. The ordinary source default
remains 20. A low-volume break consumes the same opportunity and cannot retry when
RVOL later becomes 2 without a new compression edge. This is an engineering fixture,
not a squeeze strategy or economic result.

The receiving authorized isolated harness must resolve the unchanged plugin through
`load_backtest_engine()` and drive `EngineRunSpec` through its verified public
configuration mapping. The fixture pins Spot cash long-only, realistic-v2, zero
spread/slippage, 10 bps quote-notional fee paid in USDT, initial quote 1000,
signal at row 6, buy no earlier than row 7 open 113, exit signal row 7, sell no earlier
than row 8 open 114. Submitted quantity comes from the existing order constructor;
actual filled q must be derived independently from recorded fills. Assert q>0,
buy cash `1000-113.113*q` / base q, final cash `1000+0.773*q` / base zero, fee asset/model,
reserves and equity reconciliation, no oversell or double debit. Keep the runtime's
existing stop-versus-signal priority/protected-inventory tests and ensure active
long exits precede new entries without same-decision reentry or retrospective stops.

The MIT engine venv contains no GPL plugin. This delivery therefore supplies the
scenario but does not claim actual-plugin execution, reproduce an adapter defect,
or authorize an adapter patch. Snapshot suffix tests do not prove pending-order
recovery, stops after crash, adapter resume, transport, venue fills or profitability.
Exact replay still requires the host's full software/input identity; changed pairs
must refuse it, and sealed old runs are not migrated.
