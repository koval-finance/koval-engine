# Injected Binance Spot Testnet contract

Version 1, introduced in engine 0.12.6. This is a transport-free broker port for
one allocated Spot account session and one position. The existing LiveEngine
strategy evaluator is reused. The engine contains no Spot REST client, credentials,
endpoint selector or venue simulator. Production is a separate explicit v2
[injected contract](spot_production_contract.md); Testnet defaults and IDs remain v1.

The built-in `build_broker("binance_spot_testnet", ...)` still rejects the target
before credentials or network access. Inject an implementation of `SpotBroker`
from `koval.engine.spot_contract`. The compatibility matrix entry is necessary
but does not alone authorize execution: broker identity, graph capability,
durable callbacks and fresh account reconciliation are additional gates.

## Admission and scope

- Broker attributes: `target="binance_spot_testnet"`, `exchange="binance"`,
  `market="spot"`, and `spot_identity: SpotIdentity`.
- Identity fields: nonempty opaque `account_id`, exact `symbol`, `base_asset`,
  `quote_asset`; `symbol == base_asset + quote_asset`; distinct assets;
  `exchange="binance"`, `market="spot"`, `target="binance_spot_testnet"`,
  `environment="testnet"`, `leverage=1`, `contract_version=1`.
- `LiveEngineConfig` must agree. `exchange=None` can use the explicit broker
  identity; omitting both identities cannot bypass the policy.
- Require a synchronous durable `on_record` callback. A successful return means
  the record is committed. A no-op satisfies the Python shape but is not a valid
  operational implementation. Every method, callback and feed interaction must
  run under one serialized host worker; concurrent calls are unsupported.
- Only market BUY, long-only, leverage one, one position, fixed linked SELL
  stop/target protection. Both legacy fixed-bracket graphs and native
  `exec.order_constructor` graphs are accepted with long-only direction gates.
  Dynamic/trailing/breakeven protection, disabled TP, signal exits, extra
  execution nodes, aggregator/scoring pipelines and higher timeframes fail
  explicitly. Other existing Paper/Futures graphs retain their existing policy.
- Instrument metadata, balance attribution, Testnet-only endpoints, redirects,
  credentials, Decimal filters/rounding, durable IDs, fill deduplication,
  reconciliation freshness, access control and operator approval belong to the
  host. Declared identity is not proof of a host's transport configuration.

## Public broker methods

| Method | Contract |
|---|---|
| `submit_entry(BrokerOrderIntent) -> BrokerOrderAck` | Market BUY. Round base quantity down as venue filters require; never increase it. An acknowledgement is not an execution delta. No blind retries. |
| `poll_fills(session_id) -> list[SpotFill]` | Ordered new execution deltas and terminal events, attributed to saved client IDs. Can repeat identical events. |
| `cancel_entry(symbol, session_id, client_order_id) -> None` | Cancel only the unfilled entry remainder. Return is not proof of cancellation; engine reconciles next. |
| `place_spot_protection(ProtectiveOrderIntent) -> SpotProtection` | One linked SELL OCO list for the confirmed net inventory. `side="sell"`, `reduce_only=False`: do not send a Futures reduce-only parameter. |
| `reconcile_spot(session_id, intents) -> SpotReconciliation` | Fresh complete venue evidence, including executions racing reads/cancellation. The host also restores saved protection IDs from the journal. |
| `cancel_all(symbol, session_id) -> list[BrokerOrderAck]` | Cancel only session-owned working orders. Receipts do not prove cancellation. |
| `submit_exit(BrokerOrderIntent) -> BrokerOrderAck` | Engine-recorded market SELL with role `flatten`, exact remaining inventory and stable client ID; host may round down, leaving visible dust. |

`pending`, `position`, a Futures `margin_used`, `flatten`, `modify_stop` and
`modify_protection` are not used by the Spot path. The public protocol documents
all required methods; it does not implement them.

## Fill and fee semantics

`SpotFill` extends `BrokerFill`, leaving existing brokers unchanged. Required
fields are `session_id`, `client_order_id`, nonempty `exchange_order_id`, `symbol`,
actual execution `side` (BUY=`buy`, SELL=`sell`), `role` (`entry`, `stop`, `target`,
`flatten`), `status`, `quantity`, `price`, integer `timestamp_ms`, stable nonempty
`event_id`, and `fees: tuple[SpotFee, ...]`. Each `SpotFee` contains uppercase
`asset` and nonnegative decimal-string `quantity`; an empty tuple means the
host has established no commission for that event. Do not guess zero when fee
reconciliation is incomplete.

Event timestamps must be nonnegative integers, excluding booleans and numeric
strings. Role and side remain mandatory on zero-quantity terminal events.
Malformed events fail before durable fill records or account mutation. A
terminal event without an executed quantity never supplies an equity mark;
before the first bar, only actual execution prices provide the fallback mark.

Quantities and prices are finite decimal strings; quantity is a **delta**, not
cumulative executed quantity. Emit each actual venue trade at its actual price,
not a rounded cumulative average. Use one immutable event ID per venue trade,
scoped by account/symbol/order; terminal events use separate stable IDs. Engine
in-memory idempotency supplements host durable deduplication. Repeating identical
events is harmless; changing an event under the same ID halts execution.

`partial`/`filled` execution events must have positive quantity and price.
`canceled`/`expired` may carry a final executed delta (with price and fees) or
zero/None quantity with no fees. Fee-only corrections and late fee mutations
require host reconciliation and a new session; they are not v1 events. Old
`metadata.commission*` fields are rejected on Spot fills: populate `SpotFee`
explicitly. The legacy `realized_pnl` field is not an accounting input; the
engine calculates Spot economics from executions and inventory cost.

- A BUY decreases quote cash by gross quantity times price plus quote fees.
  Base commission reduces acquired inventory and is expensed at that fill price.
  Inventory cost is net acquired quantity times price, with weighted cost across
  entry deltas. Base fees never debit quote cash a second time.
- A SELL removes sold quantity plus any base commission from inventory. Release
  weighted cost proportionally. Gross realized PnL is disposed quantity times
  fill price minus released cost; base commission at fill price and quote
  commission are expenses. Cash increases only by sold notional minus quote fee.
- Other fee assets remain in `fees_by_asset`; no conversion rate is invented.
  `valuation_complete=False`, `unvalued_fee_assets` lists them, and
  `net_realized_pnl=None`. Additions halt; confirmed inventory is still protected.
- Marked `equity` covers only allocated quote cash plus net base times the latest
  closed-bar price (last fill price before any bar). It excludes unvalued third
  assets. Platform float account projections serve the existing evaluator;
  Decimal balances are authoritative for venue reconciliation. Accounting uses
  a private 256-digit Decimal context, independent of host Decimal settings,
  retaining residual quantities across the accepted input magnitude range.

After every execution, the evaluator's float position is projected from the
Decimal inventory quantity and cost. A near-full sale can leave genuine dust
smaller than the float spacing of the original position; it remains nonzero
in the exact ledger, projection and restore. Do not infer flatness from float
subtraction or discard the residual to satisfy a projected quantity check.

`engine.spot_account` exposes exact string `quote_cash`, `quote_reserved`,
`available_quote_cash`, `base_quantity`, `inventory_cost`, `equity`,
`gross_realized_pnl`, `valued_fees`, fee totals and valuation status. Available
cash is quote cash minus venue reservation; it is `None` before preflight or
while uncertain. Quote cash includes allocated free and reserved quote.
`flat_confirmed`, `pending_entry`, `entries_halted`, `uncertain`, and the last
confirmed `protection` separate facts from unresolved operations. These fields
are also in `on_status.spot_account`; uncertain valuation makes top-level net
PnL/fee metrics null rather than presenting an incomplete total as complete.

## Reconciliation and protection evidence

`SpotReconciliation` fields: exact `identity`, `session_id`, nonempty `revision`,
`quote_balance`, `quote_reserved` (default `"0"`), `base_quantity`, ordered `fills`,
`open_order_ids`, `pending_entry_ids`, optional `protection`, and `complete=True`.
All balances and orders must be attributed to this session. Account-wide wallet
balances containing unrelated holdings are invalid. Quote reservations cannot
exceed quote balance or exist without a pending entry. Every pending entry must
appear in open orders; unknown or retired-cycle open orders halt the session.
`revision` identifies the host's evidence acquisition; the engine cannot verify
its freshness against Binance. A stale/cached response must never be marked
complete. Include all fills up to that acquisition before reporting balances.

`SpotProtection` fields: `session_id`, `symbol`, nonempty venue `order_list_id`,
engine-requested `stop_client_order_id` and `target_client_order_id`, actual
`quantity`, `stop_price`, `target_price`, optional `dust_quantity`, `dust_reason`,
`quantity_step` and `price_tick` (numeric defaults `"0"`). Both child IDs must
be present in open orders, belong to the current entry, and identify one linked
active OCO list. The host must verify the list and both child orders on venue.
Two independent sells cannot be described as this contract.

The engine requests full net inventory. With a nonzero `quantity_step`, protected
quantity must be a step multiple and dust must be less than one step;
`protected + dust == net inventory` exactly. Nonzero dust requires a reason,
stays visible, and halts additions. Without a step, only exact full-quantity
coverage is accepted. An entirely unsellable/unprotectable remainder cannot be
reported as flat. The engine does not fabricate a dust liquidation.

Without `price_tick`, acknowledged levels must equal requested levels. With a
verified tick, the host may round a long stop **up** and target **down** by less
than one tick; both resulting prices must be tick multiples and preserve
`stop < average entry < target`. Larger changes, a looser stop, an increased
target, or an invalid bracket are rejected. The normalized levels are recorded.
The host still owns current price-trigger, lot, notional and order-capacity checks.

Binance documents linked order lists and filters in its
[Spot trading API](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/trade)
and [filters reference](https://developers.binance.com/en/docs/products/spot/filters).
The contract's offline tests do not establish authenticated venue acceptance.

## Ordering and failure policy

1. Persist entry intent synchronously before `submit_entry`. Record receipt;
   keep pending until fills and a complete reconciliation establish final state.
2. Validate/persist each execution delta before accounting it or creating
   protection. Record the protection intent before its broker call. Record the
   linked receipt, then reconcile the actual working list and balances.
3. On partial entry, halt additions, cancel the unfilled entry once, reconcile
   any raced fills, then protect the final net inventory if cancellation and
   valid protection can be proved. If cancellation is still pending, halt for
   host reconciliation without selling against an unresolved BUY.
4. If protection cannot be confirmed, record cancellation, cancel session orders,
   reconcile, then record and submit one emergency SELL only after no working
   orders remain. Unknown cancellation does not authorize another order. A
   confirmed racing exit makes a redundant SELL unnecessary.
5. Partial exits update cash/cost and require an OCO for the confirmed remainder.
   A still-valid remaining linked list is retained. Lost/invalid protection uses
   the emergency policy. Full exit plus ordinary sibling cancellation, in either
   event order, is normal once complete reconciliation proves zero inventory and
   no orders. This includes fills racing protection placement itself.
6. Acknowledgement status `filled` never fabricates a fill or proves flatness.
   An emergency SELL is never automatically resubmitted. Partial/uncertain
   closure remains halted with the actual residual inventory visible.
7. Any durable callback failure latches storage failure. The same engine cannot
   poll/reconcile/close afterward; recover a fresh instance from verified durable
   facts. On other uncertainty, no exception handler blindly submits a close.
   The host must investigate/reconcile and explicitly invoke `close_spot()` when
   appropriate. External venue protection may remain active while halted.

Existing `on_order_intent`, `on_order_ack`, `on_fill`, `on_trade`, `on_event` and
`on_status` callbacks remain available. `on_record` is always called before the
corresponding wrapped callback. Spot fills include raw fees/event IDs. A trade
callback is emitted only after executions plus fresh reconciliation confirm the
whole inventory is gone and no orders remain; `pnl=None` for unvalued fees.
Partial exits do not prematurely emit a closed trade. Stable entry client ID
identifies each trade for durable host callback deduplication.

## Public integration and recovery

```python
from koval.engine.live_engine import LiveEngine, LiveEngineConfig
from koval.engine.live_feed import StopSignal

# graph: validated market/long-only/fixed-bracket graph.
# gateway: host implementation of all SpotBroker methods above.
# durable_append: synchronous transaction/fsync-backed append owned by the host.
engine = LiveEngine(
    graph,
    LiveEngineConfig("BTCUSDT", "1h", 1000, exchange="binance", market="spot"),
    broker=gateway,
    session_id="unique-durable-session-id",
    on_record=durable_append,
)
engine.reconcile_spot()  # Required, no orders submitted.
# feed must contain closed Spot Testnet candles with validated freshness/identity.
# Wire engine.watch_orders as its serialized between-bars supervision hook.
engine.run(feed, StopSignal())  # Feed completion requests confirmed closure.
```

`engine.spot_state` is a copied public snapshot containing `execution` and
`archive_checkpoint`. Persist it at acknowledged boundaries. `spot_state` journal
records contain the execution subdocument; retain the enclosing journal record's
checkpoint alongside it. Restore into a fresh identically bound engine:

```python
recovered.restore_spot(saved_state)  # No order submission or strategy replay.
recovered.reconcile_spot()  # Fresh complete account/order/fill proof.
recovered.watch_orders()  # Supervise confirmed protection.
# recovered.close_spot()                # Explicit, durable, one-attempt closure.
```

The host must verify archive integrity/completeness, exclusive account ownership,
strategy hash, timeframe, session/account/asset identity, allocated starting
capital, all intents, protective IDs, processed executions, cancel/close attempts,
and the complete committed suffix after the snapshot. A snapshot alone cannot
prove no order was sent afterward. The public execution document contains
`intents`, `protection_intents`, `events`, `entry_id`, `close_id`,
`close_attempted`, `cancel_attempted`, `closed_entries`, and `cycle_baseline`.
Recover any newer committed intent/event/attempt from its recorded payload into
these public fields before restore; preserve causal event order and stable IDs.
Do not drop an uncertain intent or infer it failed from a timeout. Validate the
assembled document against the host's transaction log and venue reconciliation.

Each engine construction uses a new archive segment ID, avoiding journal record
and fill ID reuse after restart; `spot_restore` links the prior checkpoint.
Fill callbacks and their journal payloads carry the same segment-scoped
`fill_id`; the venue `event_id` remains the stable execution deduplication key.
The engine restores confirmed ledger facts without evaluating historical entries
or sending orders. A new reconciliation must agree exactly with cash/base and prove current
protection. Pending entries and unprotected restored inventory prevent preflight;
use public supervision/closure with explicit host reconciliation to contain them.
Protection is never blindly resubmitted from a saved snapshot.

Recovery is deliberately **exposure supervision and closure only**. Additions stay
halted even after successful reconciliation. Evaluator state, daily baselines,
peak/drawdown history and prior candle-stream identity are not resumed; status
reports `risk_metrics_continuity=False` and null risk-history metrics. Once flat,
no working orders and all executions/fees are reconciled, the host may authorize
a new session with fresh history, identity, allocated capital and risk baselines.
Do not describe that as seamless strategy continuation. Unknown third-asset fees
must be resolved by the host before making performance or promotion claims.

Source tests, built artifacts, published packages, and actual Testnet execution
are four separate acceptance boundaries. The host must adopt and verify the
published package independently before operating a gateway.

Update this file when: the injected Spot port, accounting, lifecycle, rounding,
callback ordering or recovery scope changes.
