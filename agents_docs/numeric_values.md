# Historical numeric values and conditions

This is a primary-symbol, primary-timeframe graph primitive contract. It does
not change order matching, fixed risk, position exits or any live admission.
All seven nodes require `numeric_values_v1` through the existing execution
capability negotiation. Current MIT LiveEngine refuses this capability before
broker/feed/session effects. A historical plugin must implement and advertise
it explicitly; an entry-capable plugin is not sufficient.

## Public imports

```python
from koval.strategy.graph.entities import NumericValueV1, ConditionV1, ValueUnit, ValueReadiness
from koval.strategy.graph.values import (
    ValueSpecV1,
    value_at,
    graph_required_history_bars,
    VALUE_FORMULA_VERSION,
    VALUE_CAPABILITY,
)
from koval.strategy.nodes.conditions import CompareParamsV1, DistanceParamsV1, FromConditionParamsV1
from koval.strategy.nodes.cross import CrossParamsV1, CROSS_STATE_VERSION, cross_direction
```

- `value_at(ctx: BarContext, spec: ValueSpecV1) -> NumericValueV1`
- `ValueSpecV1.required_history_bars(self) -> int`
- `graph_required_history_bars(graph: dict) -> int` returns the largest numeric
  source requirement, or zero without numeric sources. It does not calculate
  legacy nodes' warmup. Hosts must combine their existing legacy requirements.
- `cross_direction(previous_left: float, previous_right: float, left: float,
  right: float) -> str | None` returns `up`, `down` or null.
- Existing `assemble_from_graph(graph)`, `GraphExecutor.build(graph).step(ctx)`,
  plugin loader, EngineRunSpec protocol v2 and execution contract v1 are unchanged.

These are new entities inheriting Entity's required `bar_index:int`,
`timestamp_ms:int` (evaluation bar open), `source_node_id:str`. Both have strict,
extra-forbidden, frozen scalar fields:

| Field | Type / rule |
| --- | --- |
| `value` | Numeric: finite float iff ready, otherwise null. Condition: strict bool iff ready, otherwise null. |
| `unit` | `price_quote_per_base`, `base_volume`, `percent`, `ratio`, `dimensionless` |
| `symbol` | str; nonempty when ready |
| `timeframe` | str or null; nonempty when ready |
| `as_of_ms` | nonnegative int or null; last contributing input bar **end** |
| `available_at_ms` | nonnegative int or null; cannot precede as_of |
| `readiness` | `ready`, `warming_up`, `missing`, `stale`, `invalid` |
| `reason` | required str or null; nonempty when unavailable, null when ready |
| `formula_version` | required nonempty str |
| `input_cursor` | str or null; nonempty when ready |

There are no new defaults on PolicyDecision, MarketEvent or older entities.
Condition units retain the operand measurement unit. Its bool is not a numeric
conversion. No implicit symbol, timeframe or unit conversion is allowed.
Formula identity includes resolved parameters; each operand's formula may differ
(e.g. close versus EMA) but cannot silently change within a crossing sequence.
Input cursors identify the evaluation/input history, not a full dataset hash;
hosts must also seal input content and the complete software identity.

## Catalog and parameters

Every input has maximum cardinality one. `[1]` on a scalar output means one
entity each evaluation, including explicit unavailable; events are optional.
Source and condition outputs allow fan-out. Existing domain/DAG rules apply:
SOURCE0, FACT/STATE1, POLICY2, INTERPRETATION3, EXECUTION4.

| ID / domain | Inputs | Outputs | Parameters |
| --- | --- | --- | --- |
| `source.value` / SOURCE | none | `value:NumericValueV1[1]` | ValueSpecV1 below |
| `fact.compare` / FACT | `left:NumericValueV1[1]`, `right:NumericValueV1[0..1]` | `condition:ConditionV1[1]` | required `operator`; `literal/unit/lower/upper=null`, `inclusive=true` |
| `fact.cross` / FACT | `left:NumericValueV1[1]`, `right:NumericValueV1[1]` | `event:MarketEvent[0..1]` | `direction=both`, one of `up/down/both` |
| `fact.distance` / FACT | `price:NumericValueV1[1]`, `level:NumericValueV1[1]`, `atr:NumericValueV1[0..1]` | `value:NumericValueV1[1]` | required `mode=percent/atr`; no default |
| `state.condition_hold` / STATE | `condition:ConditionV1[1]` | `condition:ConditionV1[1]` | required strict `bars:int`, 1..400; no default |
| `fact.condition_edge` / FACT | `condition:ConditionV1[1]` | `event:MarketEvent[0..1]` | strict empty object; rising only |
| `policy.from_condition` / POLICY | `condition:ConditionV1[1]` | `policy:PolicyDecision[1]` | strict empty object |

`ValueSpecV1` requires `component`. `context=primary` is the only accepted
binding; `offset_bars=0` is an integer >=0. `period=null` and `std_dev=null`
resolve by variant. Unsupported components/extra fields/non-primary context fail
validation. Non-null period on OHLCV and non-null std_dev outside BB fail.
Defaults serialize explicitly, round-trip, and do not migrate saved graphs.

| Component | Period default / history including offset O | Unit |
| --- | --- | --- |
| `open/high/low/close` | no period; 1+O | price_quote_per_base |
| `volume` | no period; 1+O | base_volume |
| `ema` | 20; N+O | price_quote_per_base |
| `atr` | 14; N+1+O | price_quote_per_base |
| `adx` | 14; 2N+1+O, plus N valid DX samples | dimensionless |
| `relative_volume` | 20; N+1+O | ratio |
| `bb.middle/upper/lower` | 20; N+O; std_dev=2.0 | price_quote_per_base |
| `bb.width_pct` | 20; N+O; std_dev=2.0 | percent |
| `bb.zscore` | 20; N+O; std_dev=2.0 | dimensionless |
| `channel.lower/upper/center` | 20; N+1+O | price_quote_per_base |

N is 2..400; k=std_dev is finite >0. Composite history is never clamped to400.
For example N400/O20 channel needs 421 retained bars. Host history bounds remain
2..100000. `prepare_backtest` and derived series reject too small a history
budget; direct evaluation returns warming_up for insufficient actual history.
A cross additionally requires two consecutive ready evaluations (previous values
live in bounded state, not an extra retained array slot). Hosts must check
`graph_required_history_bars` before persistence/admission and budget warmup.

## Arithmetic and readiness

Arrays are chronological, include the current closed bar, and may never be
silently replaced with scalar OHLCV, another feed, or HTF arrays. Offsets remove
the suffix **before** applying formulas. EMA uses the existing SMA seed and the
entire retained prefix; ATR uses existing Wilder smoothing and N+1 minimum.
Changing the retained history can change recursive values and is part of run
identity. Legacy prepared scalar pairs are intentionally ignored by source.value:
they lack source timing/provenance binding. Prepared strategy evaluation falls
back to the same direct evaluator; there is no new prepared formula or speedup.

BB uses mean and population sigma (`ddof=0`) on the last N closes,
upper/lower=middle +/- k*sigma. Width is `(upper-lower)/middle*100`, preserving
legacy operation order exactly. Zero middle yields invalid/zero_middle for width.
Z-score is `(close-middle)/sigma`; zero sigma yields missing/zero_variance, not
infinity or false. Nonfinite required middle/sigma intermediates yield
invalid/nonfinite_intermediate even when division would produce a finite zero. Flat BB bands/width can otherwise be ready. Channel uses the
previous N highs/lows, excluding the selected current bar; center=(upper+lower)/2.
Channel as_of/availability is the previous bar end.

Compare operators are `gt/gte/lt/lte/eq/ne/between`. Equality is exact (no epsilon).
Right input and literal mode are exclusive. Without right, literal and matching
unit are required. Between requires ordered finite lower+upper with units, no
right/literal; `inclusive=true` means both bounds inclusive, false means both
exclusive. Other operators reject bounds and inclusive=false. Defaults including
canonical null fields are accepted on round-trip. Build-time validation rejects
variant/connection conflicts and statically known unit mismatches; runtime guards
also reject symbol/timeframe/unit mismatches from custom numeric producers.

Distance requires price units on every operand and identical symbol/timeframe.
Percent forbids the ATR port and computes `100*(price-level)/level` (evaluated as
`(price-level)/level*100`); ATR mode requires ATR and computes `(price-level)/ATR`.
The denominator must be >0. Output unit is percent or ratio; negative results
are valid. Missing/unready propagates null, nonpositive denominator and overflow
produce invalid, never an event or order.

Input unavailability propagates deterministically in port order. Missing inputs
are missing/missing_input; stale evaluation cursors are stale/stale_input_cursor;
future availability is missing/future_availability. An unavailable compare emits
null. The policy bridge allows only ready true, puts readiness/reason and a
condition snapshot in metadata, and otherwise denies. Ready false remains
semantically different from unavailable.

## Clocks and historical mode

`numeric_values_v1` currently means **historical_ideal_close_v1** availability.
The host must supply actual symbol, supported fixed timeframe, timestamp_ms,
decision_timestamp_ms, history_start_ms and contiguous confirmed history. Missing
identity/timing yields missing; invalid declared span yields invalid. No context
field or receive timestamp is invented. Available_at is the ideal input bar end,
not an observed exchange/network receive time. Primary decisions before the
current bar end are unavailable; decisions at least one full interval late are
stale. Supported fixed timeframes reuse `timeframe_ms`: 1m/5m/15m/1h/4h/1d.
The span check cannot prove every interior timestamp because arrays have no
individual timestamps: the host owns candle continuity and alignment validation.
This capability must never be advertised for observed-receive, live/paper or HTF
semantics. Those require a separately accepted binding and capability.

`strategy_indicator_series(graph, candles, *, timeframe,
history_bars=DEFAULT_HISTORY_BARS, symbol=None)` keeps legacy output unchanged.
Numeric-source series require explicit symbol and use value_at per bounded
prefix. They add readiness/reason, as_of/availability and cursor to points, plus
unit, formula, history budget and availability_model to the derived descriptor.
They remain derived, not recorded evidence. Recorded observations come directly
from evaluated entities. Decision evidence version remains
`koval_trade_decision_context_v1`; new node types are supported without rewriting
old recordings. Unavailable nodes retain partial evidence status/reasons.

## Crossing state and execution limits

Cross up is previous_left<=previous_right and current_left>current_right; down is
previous_left>=previous_right and current_left<current_right. Both consecutive
pairs must be ready. Equality only arms a later strict transition. Gap/unavailable
clears the previous pair; the next ready pair re-arms. Repeated timestamp emits
no second event; backwards time and unknown state version fail. Changing operand
formula/source identity re-arms. Direction filtering still advances state.

`CROSS_STATE_VERSION=1`, bounded JSON NodeState keys: version,
last_timestamp_ms, previous (null or two NumericValueV1 dumps). No unbounded event
history. Checkpoints are post-bar state, not pending-intent journals. A restored strategy
returns an empty transient StepResult on its already-stepped bar without replaying
any node; the next bar continues from the saved state. Hosts recover outstanding
intents through their execution journal, not graph-state restoration. Event kind=cross, bullish/up or bearish/down;
metadata carries cross_v1/state version, current operand and previous pair
provenance. Price levels contain the current right value only for price units.

Source formula prefix is `numeric_value_v1:` plus resolved ValueSpecV1 JSON;
compare prefix is `compare_v1:` plus resolved CompareParamsV1 JSON;
distance prefix is `distance_percent_v1` or `distance_atr_v1`; bridge prefix is
`from_condition_v1`. Derived formula identities append canonical JSON of stable
operand symbol/timeframe/unit/formula/source bindings, preserving transitive
formula changes through a distance before crossing. Event provenance is detached
from persistent state, so downstream metadata mutation cannot change decisions. Formula/state/capability versions are independent of package
version. No AND/OR/NOT or event-condition bridge is provided. A down-cross is data,
not a short command. MarketEvent cannot connect directly to PositionExit.

A price comparison can reach entry policies or the existing PositionExit through
the explicit bridge, subject to separate host admission. PositionExit still
requires position_exit_v1 and current identified long; existing fixed SL/risk/TP
semantics are unchanged. Historical intent tests do not establish fill parity,
paper support, venue acceptance or profitability. Keep old runs sealed under
their original full identities; a local wheel with the same version is not the
same artifact and cannot reinterpret archived results.

Tests: tests/strategy/graph/test_values.py, test_value_integration.py and
tests/strategy/nodes/test_conditions.py, plus existing legacy hash guards.

## Historical condition extensions

These additions are historical primary gates. Existing `bb.width_pct` is the
compression measurement; there is no `bb_width_pct` alias, full squeeze/range
detector, boolean AND/OR/NOT, alternate feed, or change to execution/matching.
The new source variants keep ValueSpecV1's strict/frozen schema and existing
serialization defaults. `ValueSpecV1.formula_prefix: str` and
`ValueSpecV1.formula_version: str` expose resolved formula identities. Old
components retain exactly `numeric_value_v1:<resolved JSON>` and unchanged
input cursors; `VALUE_FORMULA_VERSION` stays `numeric_value_v1`.

ADX uses `adx_wilder_v1:<resolved JSON>`. Public pure helper
`koval.strategy.helpers.filters.trend.adx_value(highs, lows, closes, period=14)
-> float | None` is the single Wilder calculation also used by the unchanged
`adx_filter(highs, lows, closes, period=14, min_adx=25.0) -> bool` predicate.
It seeds TR/+DM/-DM from indices 1..N, updates from N+1, seeds ADX with the
first N valid DX samples, then Wilder smooths the remaining retained prefix.
It needs at least 2N+1 closed bars; an offset removes the suffix before seeding.
Smoothed TR=0 skips a DX sample. Fewer than N valid samples is null,
`warming_up/insufficient_valid_dx`. H=L=C constant data cannot produce ready
zero; constant closes with nonzero true range and zero DM can produce valid
ADX=0. Ready ADX is dimensionless in 0..100. Legacy threshold equality,
including min_adx=0, is preserved.

RVOL uses `relative_volume_v1:<resolved JSON>` and
`V[t] / mean(V[t-N:t])`: N preceding confirmed volumes exclude the selected
current volume from the denominator. [10,20,30] gives 2 at N2; [10,20,0] gives
ready zero. Zero preceding mean gives null `missing/zero_volume_mean`.
Nonfinite mean/result is invalid. Missing, nonfinite or misaligned volume
arrays and bad primary timing stay unavailable, without scalar-volume fallback.
The source, prepared strategy, derived chart and recorded output share value_at.

Import reducer parameter models and versions from
`koval.strategy.nodes.condition_reducers`:
`HoldParamsV1`, `EdgeParamsV1`, `HOLD_STATE_VERSION=1`, `EDGE_STATE_VERSION=1`,
`HOLD_FORMULA_VERSION="condition_hold_v1"`,
`EDGE_FORMULA_VERSION="condition_edge_v1"`.
Hold rejects missing bars, bool, float, zero, negative and >400. It counts once
per confirmed primary cursor, saturates at N, and outputs ready false until the
Nth ready true, then ready true. Ready false resets to zero; unavailable resets
and preserves null/readiness/reason. Gaps in timestamp or primary bar index reset;
the first post-gap true counts one. Input symbol/timeframe/unit/formula/source
binding changes reset before consuming the new cursor. Advancing input cursors
and history_start_ms do not change that binding as the retained window slides.

Edge emits only consecutive ready false -> ready true. Initial true,
unavailable -> true, false/unavailable/true, gaps, stale/future input and changed
bindings cannot fabricate a rising event. Hold3 on true/true/true produces
false/false/true and its edge fires on the third bar. Hold1 initially true
produces no event; a later false/true produces one. Event kind is
`condition_edge`, direction `bullish`, and price_levels is always empty,
including when the condition originated from a price comparison. Metadata has
`formula_version="condition_edge_v1"`, `state_version=1`, and detached full
`previous`/`current` ConditionV1 dumps (including clocks and input cursors).
GraphExecutor stamps the actual graph node ID on the event and hold output.
Evidence remains `koval_trade_decision_context_v1` and records both new nodes.

For a duplicate consumed timestamp with unchanged context/source binding and
bar index, hold returns its prior output even if the payload conflicts; edge
emits nothing. Neither revises state. A duplicate with changed binding/index
raises `ValueError`. Backward timestamps, or advancing timestamps with a
nonadvancing primary bar index, raise before state mutation. Input readiness
and time are checked on new cursors; stale/future values disarm/reset.

Reducer plain JSON state has strict, extra-forbidden version1 shapes:

- Common: `version`, `last_timestamp_ms`, `last_bar_index`, `symbol`, `timeframe`,
  `binding` (null or stable symbol/timeframe/unit/formula_version/source_node_id).
- Hold adds `formula_version`, `bars`, bounded `count` (0..bars), and its last
  ConditionV1 `output` for duplicate return.
- Edge adds `formula_version` and `previous` (null or the last ready ConditionV1).

State size depends on graph depth/configuration and bounded scalar provenance,
never run length or N input bars. Empty state means unstepped.
GraphStrategy checkpoints with hold/edge nodes require an integer envelope cursor,
the complete exported node-state map, and reducer cursors/primary identities that
agree with that completed bar. Initial checkpoints must contain no node states.
Invalid checkpoints refuse before state mutation or same-bar cache suppression.
Unsupported
versions, malformed cursors/count/output, extra fields and own formula/config
mismatches raise before mutation. Public
`validate_condition_reducer_state(kind: str, params: BaseModel, state: dict)
-> None` also runs in `GraphStrategy.restore_graph_state` before any restore,
so invalid snapshots cannot hide behind same-bar suppression. Changed upstream
binding resets on the next new cursor, including after restore; own changed N
refuses. Graph state envelope stays version1 with `version/states/stepped_bar`.
Snapshots are post-bar state: same-bar restore emits no transient outputs, and
the suffix reproduces uninterrupted outputs, evidence and state. This does not
provide a broker journal or adapter resume.

### Physical history and temporal replay

`graph_required_history_bars(graph: dict) -> int` keeps its old meaning: exact
largest physical source window, never a sum of temporal node latencies. N400
ADX needs 801+offset rows; RVOL needs 401+offset; existing channel400/offset20
needs 421. A historical condition graph (ADX/RVOL/hold/edge) with exact physical demand >1000
refuses at GraphExecutor.build before feed/session execution. Old graphs retain
their existing explicit larger-window contract. No arrays grow automatically,
and no demand silently clamps to 400 or 1000.

Public static discovery in `koval.strategy.graph.condition_history`:

- `graph_condition_history_requirements(graph: dict) -> dict`, schema
  `koval_condition_history_v1`: exact `source_history_bars`,
  `first_ready_bar_by_node`, `first_true_bar_by_node`, `replay_prefix_bars`,
  `unknown_nodes`, `assumptions`, and `restored_state`. It validates graph
  structure and returns exact source requirements even above the admission cap.
- `condition_capabilities() -> dict`, schema
  `koval_historical_conditions_capabilities_v1`: JSON parameter schemas for
  source/hold/edge, independent formula/state/history versions, physical limit,
  temporal-prefix policy, event/duplicate rules and historical mode binding.

History bar positions are one-based theoretical bounds under uninterrupted,
valid inputs. Sources' bounds equal their source row requirement; numeric
nodes propagate maximum parent availability, as do comparisons/bridges.
Unknown/legacy producer requirements are null and appear in unknown_nodes;
hosts must combine their independent legacy warmup requirements. A hold's
first ready bound is its parent's, but first true adds N-1. Nested holds add
those latencies across the actual DAG. An edge/cross earliest event is
max(parent first-ready+1, parent first-true). Conservative replay_prefix_bars
adds one prior-observation allowance at edge/cross; it is not the earliest
event formula. If an upstream condition first becomes ready at h, holdN can
turn true at h+N-1, and hold3's edge can fire then. h+N is only conservative
replay allowance. Four hold400 nodes can first turn true/fire edge at 1597,
with conservative prefix 1598 and physical source window 1. This remains allowed
with a bounded 1000-row stream. Runtime missingness may delay all bounds
indefinitely. A valid restored post-bar state replaces its consumed temporal
prefix; lack of an uninterrupted replay prefix is distinct from runtime input
missingness and snapshot validity.

Execution negotiation still requires `numeric_values_v1`; these additions use
the generic evaluator and do not introduce matching or broker capabilities.
Existing numeric_values_v1 alone does not identify historical condition engine bytes. Hosts must
check the new discovery schema and exact artifact identity, then prove their
plugin's timing/arrays/evidence/state path separately. Discovery declares only
historical_ideal_close_v1, modes=[historical], primary context,
adapter_execution_verified=false and adapter_resume_supported=false. Paper,
exchange sandbox/Testnet, live, HTF and cross-symbol semantics remain closed.
No adapter execution/support claim follows from source inspection or this report.

Portable literal value/hold/edge/evidence/state expectations and snapshot/suffix
inputs ship in `koval/examples/runtime/historical_conditions_v1.json` (schema
`koval_historical_conditions_fixture_v1`). Expected projections are hand-derived;
tests additionally compare the complete uninterrupted/restored outputs and state.
`koval/examples/graphs/historical_condition_hold_edge.json` is a runnable graph
with no execution terminal. Tests include `test_historical_conditions.py`,
`test_condition_fixtures.py` and `test_condition_reducers.py`.

Update this file when: numeric formulas, scalar schemas, timing, history budgets,
capabilities, crossing state or derived/recorded evidence contracts change.
