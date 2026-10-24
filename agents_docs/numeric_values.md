# Historical numeric values and conditions

This is a primary-symbol, primary-timeframe graph primitive contract. It does
not change order matching, fixed risk, position exits or any live admission.
All five nodes require `numeric_values_v1` through the existing execution
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
| `bb.middle/upper/lower` | 20; N+O; std_dev=2.0 | price_quote_per_base |
| `bb.width_pct` | 20; N+O; std_dev=2.0 | percent |
| `bb.zscore` | 20; N+O; std_dev=2.0 | dimensionless |
| `channel.lower/upper/center` | 20; N+1+O | price_quote_per_base |

N is 2..400; k=std_dev is finite >0. Composite history is never clamped to400.
For example N400/O20 channel needs421 retained bars. Host history bounds remain
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

Update this file when: numeric formulas, scalar schemas, timing, history budgets,
capabilities, crossing state or derived/recorded evidence contracts change.
