# Bounded Spot multi-order execution

This additive MIT contract implements cash-funded price-drop DCA and fixed
arithmetic buy-first Grid for one Binance Spot symbol/timeframe. It supplies pure
controllers, accounting, deterministic paper matching and shared adapter fixtures.
It supplies no exchange transport. These recipes do not establish profitability.

## Compatibility and admission

Public nodes are `exec.spot_dca` and `exec.spot_grid`. They require execution
contract **2**, plus `spot_multi_order_v1`, `spot_reservations_v1`,
`spot_causal_ohlcv_v1`, `spot_state_restore_v1` and the respective `spot_dca_v1`
or `spot_grid_v1`. `graph_execution_capabilities()` derives these statically;
omitting explicit host flags cannot bypass negotiation. Graph validation permits
exactly one recipe terminal and refuses mixed legacy execution terminals or a
consumed recipe output. A connected start policy is required.

Use `koval.engine.spot_multi_order.SpotPaperSessionV1`, a dedicated paper lane.
Existing LiveEngine/PaperBroker profiles and Spot Testnet v1 refuse these nodes.
The existing graph first-order bridge, TradeSetup, bracket semantics, profiles,
hashes and default execution contract remain unchanged. Engine protocol stays 2.
Historical plugins must explicitly implement contract 2 and pass shared fixtures;
an engine source test cannot establish adapter parity or a compatible release pair.

No leverage, borrowing, shorting, scheduled DCA, size multipliers, geometric Grid,
recentring, portfolio inventory, automatic optimization or production target is
supported. Evaluation dates can be independent and overlapping across sessions.

## Parameters and graph ports

Both nodes have `start: PolicyDecision` (required), `stop_hold: PolicyDecision`
and `stop_flatten: PolicyDecision` (optional), at most one connection each.
They emit terminal `recipe: SpotRecipeRequest` and nonterminal
`status: SpotRecipeStatus`. Status contains confirmed phase, cycle, prices,
inventory and free/reserved balances. It requires `BarContext.spot_runtime` from
the capable host. This is separate from `OrderRequest` and short-entry hooks.
GraphStrategy exposes `get_spot_recipe_request()` after its cached bar evaluation.

`SpotDcaParams` and `SpotGridParams` in `koval.strategy.schemas` reject extra keys.
Except `version=1`, parameters are mandatory. Numeric inputs may be decimal text
or JSON numbers; booleans, nonfinite values, negatives, values above 1e18,
more than 18 fractional places, or strings above 128 characters fail. Counts are
strict integers. Owned precision-256 arithmetic preserves derived ratios/costs;
restored accounting values are not truncated to the input scale.

| DCA | Meaning |
| --- | --- |
| budget_quote / fee_reserve_quote | Allocation cap and cleanup cushion within it |
| base_quote / addition_quote | Fixed gross notional caps excluding BUY fees |
| max_additions | 0..64 |
| deviation_pct | Positive, below 100; count × deviation below stop_loss_pct |
| take_profit_pct | Positive, at most 1000 |
| stop_loss_pct | Positive, below 100 |

| Grid | Meaning |
| --- | --- |
| budget_quote / fee_reserve_quote | Allocation cap and cleanup cushion |
| lower_price / upper_price | Fixed positive range |
| intervals | 1..64 intervals, N+1 points including bounds |
| quantity_per_slot | Fixed positive requested base size, rounded down |
| stop_price | Required positive stop strictly below normalized range |

Grid points are arithmetic and floored once to a common price tick. Reject
duplicate normalized points. Select initial points strictly below the activation
close, excluding the upper endpoint; activation must be inside the range and
select at least one bid. The selected slot set stays fixed. No initial base or
unbacked asks are created. DCA addition BUY prices are floored; target/stop prices
are ceiled. Reject normalized duplicate additions or additions at/below the stop.
Requested/admitted slot quantities remain visible; rearm preserves admitted size.

## Public Python API

Import the following from `koval.engine.spot_multi_order`:

- `SpotBindingV1`, `SpotRecipeV1`, `SpotInstrumentEvidenceV1`, `SpotFillPolicyV1`.
- `SpotOrderIntentV1`, `SpotCancelIntentV1`, `SpotReplaceIntentV1`,
  `SpotExecutionEventV1`, `SpotFeeV1`, `SpotFeeValuationV1`, `SpotReconciliationV1`.
- `create_spot_session(binding, recipe, instrument, policy)` creates isolated state.
- `advance_spot(state, observation)` returns `SpotTransitionV1` containing detached
  next state, `SpotDecisionV1` and ordered records; it performs no I/O.
- `dca_decide(controller_state, facts)` / `grid_decide(controller_state, facts)`
  consume facts `{state, kind, payload}` and return detached controller/intents.
  Hosts should use `advance_spot` to apply accounting and admission atomically.
- `submit_spot_order(state, intent)` reserves a local intent; it does not dispatch.
  `cancel_spot_order(state, client_order_id, command_id=..., generation=...)`
  retains remaining reserves. `replace_spot_order(state, request)` first cancels;
  call it again only after terminal reconciliation to admit its next generation.
- `apply_spot_event`, `apply_spot_fee_valuation`, `reconcile_spot_state` reduce
  attributed facts without I/O. Raw event replay alone does not authorize dispatch.
- `export_spot_state`, `restore_spot_state(..., expected_binding=...)` exchange
  strict, checksummed `koval_spot_session_state_v1` envelopes.
- `spot_execution_report(state, mark=...)` returns a detached JSON-compatible
  `koval_spot_execution_report_v1` dictionary. Historical adapters transport it
  under `BacktestResult.metrics["spot_execution_v1"]` without changing legacy roots.
- `compare_spot_execution_reports` returns `SpotConformanceResultV1` with
  `matches` and ordered `differences`. Identity, quantities, reserves and prices
  compare exactly; cash/cost/equity rounding differences must be strictly below
  the configured tolerance (at most 1e-18), without crossing zero's sign.

Binding fixes account/session, symbol/assets, paper/historical mode, graph hash,
software identity, policy/instrument hashes, starting quote cash, timeframe,
ending policy and optional half-open evaluation bounds. Initial base is zero.
No credentials or broker parameters exist on the dedicated paper session.
`SpotInstrumentEvidenceV1` requires explicit synthetic/historical/current/approximate
provenance, validity interval, price/lot/notional bounds and owned order capacity.
Unsupported active filters fail closed. Synthetic fixture evidence is not venue
evidence. `SpotFillPolicyV1` requires explicit fees, asset/provenance, fee caps,
spread/slippage, participation and optional expiry. No hidden zero-cost default.
Historical paper candles require historical evidence coverage. Reports retain
original provenance and label `instrument_usage.status=approximation` when a
current snapshot is reused in historical mode. This does not create missing
historical venue evidence.

```python
from koval.examples import spot_multi_order_fixtures
from koval.engine.spot_multi_order import SpotPaperSessionV1

case = next(f for f in spot_multi_order_fixtures() if f["id"] == "grid_control")
session = SpotPaperSessionV1(
    case["binding"],
    case["graph"],
    case["instrument"],
    case["policy"],
    on_record=durable_append,  # synchronous host-owned append acknowledgement
)
for observation in case["observations"]:
    session.on_candle(observation["candle"])
checkpoint = session.export_state()
report = session.report(mark="11")
# session.finish() applies the binding's explicit ending policy.
```

`command({command_id, action, reason})` supports `stop_and_hold` and
`stop_and_flatten`. `finish(policy=...)` explicitly ends data, defaults to the
bound ending policy and is idempotent. No later candles are accepted after finish.
Public `state` and reports are detached copies. Calls must be serialized.

Pure observations contain `observation_id`, a contiguous `sequence`,
`timestamp_ms`, `kind` and `payload`. Price payloads contain positive `price`,
`bar_index`, `phase` (`open`, `descending`, `ascending`, `close`), and optional
`crossing_index`/`bar_id`. Crossing identity and phase order persist across restore;
changed duplicates, rewinds and price crossings after a closed-bar decision fail.
Closed-bar payloads contain `bar_index`, `close`, optional strict signal booleans
`start`/`stop_hold`/`stop_flatten`, optional matching six-value `ohlcv`, and optional
`decision_timestamp_ms`. Conflicting closed-bar payloads fail even with a new
observation ID. The paper lane supplies these causal facts automatically.

## Accounting and lifecycles

Quote cash is actual starting cash minus BUY debits plus SELL proceeds. A smaller
recipe allocation cannot spend unassigned starting cash. Free allocated cash
subtracts order, unsubmitted-cycle and cleanup reserves. Buys reserve their worst
remaining notional and quote fees; SELLs reserve actual base plus maximum base
SELL fee. Unknown/pending-cancel orders retain reserves until complete terminal
reconciliation. One shared allocation prevents independent bids overspending it.

Gross buy VWAP excludes fees. Remaining cost basis includes actual BUY cash cost,
allocated over net base after base fees, plus valued BUY third-asset fees. SELLs
realize net proceeds less released cost and valued SELL third-asset fees. Never
subtract already accounted base/quote fees again. Third-asset fees retain raw
amounts and provenance; unresolved valuation makes net equity/PnL unavailable
and blocks BUYs. Append-only `SpotFeeValuationV1` values the original immutable
fee ID, including after a partial sale; release weights preserve equivalent
economics. New BUYs require valuation and successful reconciliation.

Events carry account/session/symbol/order/generation, immutable execution/trade
IDs, contiguous host-normalized source sequence, actual delta/cumulative base
and quote totals, explicit fees, status and execution identity. Exact duplicates
are neutral, including quarantined executions. Conflicts and identity mismatches
raise explicitly; overfills enter quarantine before accounting.
Missing sequence buffers and halts writes. Complete newer reconciliation must
prove all owned orders, final cumulative totals, reservations, cash and inventory;
an ACK or absent open-order listing alone proves neither fill nor rejection.
An unknown cancel remains latched through partial fills, unchanged order facts
and restore; only attributed terminal evidence can resolve that cancellation.
Beyond-cap/over-allocation executions are quarantined with last verified balances
retained and all writes halted. Such evidence is never silently dropped or funded.

DCA starts one cycle on an allowed connected signal while idle. The first
positive base fill arms a stop immediately, even while its order is partial.
The stop can tighten with base VWAP and never loosens. Base terminal reconciliation
fixes P0; additions use `P0*(1-i*deviation_pct/100)` and equal quote amounts.
The common target uses actual gross-average entry × `(1+TP/100)` with actual net
sellable quantity. Cancel/reconcile the old target before replacement; an
unactivated desired target cannot trigger an exit. Target touch/fill freezes
additions. Stop before base completion cancels/reconciles remaining BUYs and
closes actual raced inventory without staging additions. TP is not net profit.
DCA admission reserves capacity for the target and all additions (at least two
owned orders). An invalid follow-up target/addition cannot discard a confirmed
fill: it selects containment/flatten with the first-fill stop retained. Closure
still requires the cancellation barrier, sellable quantity and available liquidity.

Grid slots sell only actual net acquired base after their BUY becomes terminal
and reconciled, at the adjacent common price point. SELL completion rearms its
original fixed admitted bid on a later closed-bar observation, subject to cash
and the upper-range pause. Unsellable residual dust stays in its original lot
and cannot be sold again by a later ordinary lot generation.

Stops/hold/flatten persist a terminal command and freeze new buys/rearms first.
Cancel/reconcile all owned BUYs and SELLs, including reserved-base targets, before
creating a replacement close. Unknown cancellation forbids further writes.
Partial closes keep their identity; a successor requires a reconciled predecessor
and confirmed residual. Failed closes need a distinct explicit continuation
command. Grid terminal flatten may pool retired dust; oldest lots release their
own cost in stable order. Hold records an unprotected stopped holding. Stops
always select flatten; unavailable liquidity/dust never imply flatness.

## Causal OHLC policy and replay

`koval_spot_ohlcv_v1` assumes **open → low → high → close**. It records multi-level
ambiguity; this assumption is not a recovered market path or venue fill bound.
At an opening gap an existing stop wins over bids; an executable opening target
precedes a later intrabar stop. Without an opening target, the low/stop precedes
the intrabar high/target. Equal-price priority is stop/terminal, then SELL, then
BUY, with acceptance order/client ID tie-breaks. Limit gaps use the favorable
open; market orders use their next eligible open. Costs adjust adversely by
spread/2 + slippage and respect limit prices/ticks. Opening matches use taker
fees; other resting limit crosses use maker fees, an explicit proxy assumption.

Ordinary orders/replacements activate next candle. A newly acquired lot cannot
sell/rearm on reused extrema in its fill candle. Only protective containment may
use a later unconsumed path crossing, once, after the cancellation barrier.
All fills share `volume * participation`, including stops and terminal closes.
Exhaustion leaves actual pending/residual inventory. Expiry precedes matching
at the first bar beyond its active lifetime. Zero costs remain explicit inputs.
Final flatten uses one synthetic last-close observation and only unused final
candle volume; it cannot claim a complete close when capacity is exhausted.

Snapshots preserve controller, account, reservations, order generations, processed
events/fees, source/candle cursors, volume already consumed, graph state/history,
terminal latches and journal chain. Restore into a fresh identically bound session:
`restored.restore_state(checkpoint, journal_suffix=committed_records_after_snapshot)`.
Verify that the supplied suffix is complete against host storage. Unknown versions,
software/graph/evidence changes, integrity failures or incomplete candle checkpoints
are refused. An in-progress candle needs the complete committed suffix through
its processed boundary; replay must not silently skip its remaining phases.
Storage callback failure latches the old instance. Unknown outcomes remain
reconciling; no new identity is used as an implicit retry. There is no automatic
migration from existing bracket/Testnet snapshots and no silent software upgrade.
Each pure observation is journaled before it is applied. A failed transition
retains that pending input for investigation and requires fresh recovery; the old
instance cannot silently retry or skip the unfinished candle.

## Shared acceptance and release boundary

`koval.examples.spot_multi_order_fixtures()` loads checksummed bundled JSON cases.
`run_spot_fixture()` executes their paper or explicitly attributed controller-event
observations and compares independently specified expectations after each step.
The 16 fixtures include both zero-cost controls, base/quote/third-asset fees,
partial fills, cancel/fill races, gaps, same-candle levels, first-partial-base stop,
unknown restart, stale replacements, expiry, rejection, conflicting duplicate IDs
and deferred fee valuation. Every observation specifies ordered intent, fill and
order-state expectations alongside its accounting assertions.
Their inputs include native graph, binding, explicit costs, synthetic instrument
provenance, chronology, intents/fills, reserves, cost basis, equity and holdings.
The event driver's simulator reconciliation is an offline ledger proof, never
venue reconciliation evidence. Existing parity fixtures and their hashes are
unchanged. Unit tests alone are not adapter acceptance.

The owner chooses/releases the engine package after review. The historical adapter
must adopt its released API and independently run every fixture through its public
loader, then the consuming host must verify the released engine/adapter pair.
No compatible adapter version is asserted here. Testnet requires a separately
approved port/transport and independent lifecycle acceptance; v1 stays refused.

Update this file when: recipe parameters, public interfaces, accounting, ordering,
restore, capability or acceptance versions change.
