# Injected cash Spot production contract

Version 2 is an explicit, host-owned port for `binance_spot_live_internal`,
`environment="production"`, `contract_version=2`. It contains no HTTP transport,
credentials resolver, account discovery or automatic connection. The built-in
factory still accepts exactly `paper` and `binance_sandbox`; production cannot
be selected through that factory or by replacing a sandbox URL. Sandbox origin
and redirect restrictions remain unchanged. Offline conformance is not venue,
account, deployment or permission acceptance.

## Admission and supported strategy

Import `SPOT_PRODUCTION`, `SpotIdentity`, `SpotBroker`,
`spot_execution_capabilities` and `validate_spot_selection` from
`koval.engine.spot_contract`. Existing broker method signatures are unchanged.
`spot_execution_capabilities()` returns a fresh
`koval_spot_execution_capabilities_v2` declaration. Testnet remains explicitly
v1, with its original defaults and generated IDs.

Production requires exact equality of `broker.spot_identity` and
`LiveEngineConfig.spot_identity`: nonempty opaque account ID, Binance Spot,
exact symbol/base/quote, leverage one and the production target/environment/v2.
Config and broker must also explicitly agree on `spot_software_binding` and
`spot_claim_generation`, both nonempty host-assigned strings. The software
binding identifies the accepted complete deployment, not just its version label.
The session ID must be durably allocated and exclusively owned by the host.
Use a synchronous durable `on_record`: returning acknowledges committed bytes.
No-op persistence is invalid for operation.

The supported subset is one primary symbol/timeframe, one cash long MARKET
entry, one fixed linked SELL stop/target bracket. Legacy fixed-bracket and native
order-constructor graphs retain the same evaluator. Trailing, disabled targets,
HTF, position exits, numeric/condition state capabilities, recipes, Grid/DCA,
Futures, margin and other venues are refused. Admission does not rewrite a graph.

Every production intent/ACK/fill has `metadata["spot_context"]` equal to the
public execution state's `context`: identity, session and strategy/timeframe/
software/claim binding. `SpotProtection.context` and `SpotReconciliation.context`
use the same value. Every journal record has a hash-covered `spot_context`.
Generated production client and list IDs include the canonical context hash;
Testnet IDs are unchanged. Preserve `ProtectiveOrderIntent.metadata` including
`list_client_order_id`. Cross-environment/account/software events are refused.

## Broker evidence and mutation permits

The seven `SpotBroker` methods retain their signatures:
`submit_entry`, `poll_fills`, `cancel_entry`, `place_spot_protection`,
`reconcile_spot`, `cancel_all`, `submit_exit`. Their base fill, fee and bracket
semantics follow [the Spot accounting contract](spot_testnet_contract.md).
Each mutating broker method must obtain its own host-owned durable dispatch
permit. Engine journal intent is necessary but is not that permit. The engine
never supplies network authority or retries an unknown request.

Production reconciliation additionally supplies:

- `terminal_order_ids`: all known terminal entry, exit and protective child IDs,
  including previous attempts. Omission is unknown, not terminal.
- `terminal_list_ids`: original list client IDs proved terminal, including a
  submitted list whose response was lost. Active protection instead needs both
  children and its matching linked list evidence.
- `unknown_request_ids`: any unresolved request, including an unknown cancel.
- `fees_complete`: all attributable executions and their fees have reconciled.
- `base_free`: exact session-attributed free base, required for additional disposal.

A complete fresh report covers every saved operation, trade and list, including
cancel/fill races and all pages. Its `revision` identifies the acquisition; the
host verifies freshness. Reported absence alone cannot resolve a saved intent or
list. Unknown outcomes inhibit entries and prevent new mutations. The host must
query original IDs; never resend because a response timed out. Flatness requires
exact zero inventory, no owned/unknown orders and complete fees/terminal proof.

Requested absolute SL/TP remain frozen at decision time. Only the existing
bounded tick tightening is allowed; actual fills never recenter the bracket.
Protection covers confirmed net inventory. ACK alone does not prove an active
list; reconciliation can contain an immediate or partial leg execution. A
partial BUY inhibits additions, cancels its remainder once and reconciles late
fills before protection. A partial exit also latches the entry gate. Dust remains
visible; untradeable inventory is not flat.

Base and quote fees use exact Decimal accounting. A third-asset fee remains in
its original asset/quantity, inhibits additions and makes valuation/PnL
incomplete; observation and safe containment remain possible. No conversion
service or invented zero price is supplied. Fills are immutable deltas, not
cumulative quantities, durably recorded before account mutation. Hosts provide
causally ordered deltas; repeated identical events are harmless, conflicting
receipts refuse. A persistence/callback failure poisons the instance, which can
no longer poll, reconcile or mutate; recover a fresh instance from verified facts.

## Public controls

| Method | Behavior |
|---|---|
| `halt_spot_entries(reason: str) -> dict` | Permanently inhibits entries and saves the first reason. Works between bars. Does not cancel, sell or discard an already-sent BUY. |
| `observe_spot() -> dict` | Polls and reconciles attributable fills into the existing ledger. No evaluation, place or cancel. Retains partial/unprotected/unknown facts and never clears a halt. |
| `supervise_spot() -> dict` | Bounded protection/containment without evaluating entries. Preserves valid OCO during a pause and obeys per-action host permits. After terminal custody it only observes. |
| `close_spot() -> dict` | Existing single recorded cancel/SELL lifecycle. Never resends its SELL on partial execution or timeout. |
| `retry_spot_close(*, attempt_id: str, expected_state_hash: str) -> dict` | Production-only, separately authorized additional disposal. Returns an immutable attempt receipt, including unknown outcome. |
| `finish_spot(*, disposition: str, evidence_hash: str) -> dict` | Production-only terminal custody acknowledgement with no venue mutation. |

Existing `spot_account`, `spot_state`, `restore_spot(state)` and
`reconcile_spot()` remain public. `watch_orders()` is mutating supervision, never
an alias for read-only observation. State/account snapshots are copied under the
engine work lock. Controls serialize with bar processing and supervision;
mutating callback reentry is rejected. The host still serializes its feed and
worker and provides exclusive account ownership across processes/hosts.

## Exact host authorization boundary

Pass `spot_host=...` to `LiveEngine`. The protocol and frozen
`SpotControlReceipt` are in `koval.engine.spot_control`. No app authentication or
grant service is implemented here. The injected host is trusted infrastructure:
it authenticates the human, checks expiry/revocation and exact action/evidence,
atomically consumes a new grant, and returns an immutable saved receipt.
A caller-supplied `verified=True` or an arbitrary evidence string is not proof.

`authorize_spot_control(request: dict) -> SpotControlReceipt` receives a copied
`koval_spot_control_request_v2` containing action, full execution context, current
financial/order state hash, exact base/open orders and action-specific arguments.
The receipt must bind `request_sha256=content_sha256(request)` and unique nonempty
`grant_id`/`receipt_id`. Every new action needs fresh authority. The host must
verify immutable `evidence_hash` content for finish, not merely echo it.

For `retry_close`, engine first performs a fresh observation and compares its
`execution.observation_hash` with `expected_state_hash`. This canonical hash
covers operation/fill/order/account facts, excluding acquisition revision and
repeated fill delivery; fresh acquisition remains required. Every previous
entry/exit/list must be known terminal, with complete fees, no working or unknown
request, a positive partial-exit remainder and exact free managed base. The
receipt specifies positive `quantity`, `max_quantity`, `reference_price` and
`max_notional`. Requested quantity cannot exceed free remainder or either ceiling.
The price is host-verified fresh reference evidence: a MARKET order cannot
promise its eventual price or guaranteed notional. The host still verifies venue
filters, sell fees, executable quantity and its wire permit at dispatch.

Attempt ID determines one new durable SELL identity. Reservation and intent are
committed before dispatch. Same attempt and payload returns the saved receipt
without another send or authority consumption, including after timeout/restore.
Changed expected hash under the same ID refuses. Old close ID and all earlier
attempts/receipts remain intact. No automatic new attempt or dust disposal exists.

For `finish`, allowed dispositions are exactly `closed_flat` and
`custody_transferred`. A fresh observation and terminal/fee proof precede host
authorization. `closed_flat` additionally requires exact zero and no owned or
unknown orders. The receipt specifies `protection_disposition="retain"` or
`"cancel_owned"` and exact `retained_order_ids`. Retain requires verified active
OCO when orders remain; cancel_owned requires cancellation already proved by
reconciliation. Finish itself never cancels protection.

The single-use terminal record includes exact inventory and retained protection;
it is committed before acknowledgement. Persistence failure is not custody
success. Every later Stop, EOF, exception cleanup, finalize, close and supervision
preserves terminal mode and sends no venue mutation. Observation can journal late
attributable fills and update exact balances without changing the original
custody receipt. `custody_transferred` never means flat. Claim release, physical
process exclusion and subsequent owner-managed venue work remain host concerns.
Testnet Stop behavior is unchanged.

## Recovery and public state

Production `spot_state` includes v2 `execution`, `archive_checkpoint` and
`archive_segments`, containing complete acknowledged records/checkpoints from
each segment. Persist these with the independently committed host head. Execution
includes all entry/protection intents, ACKs, events, pending/cancel facts, original
close ID, disposal receipts, halt reason, terminal disposition and observation
hash. The list can grow: hosts should archive it durably and budget memory for
this intentionally bounded session contract.

`restore_spot` verifies segment hashes, context and linkage, checkpoint equality,
all durable operation/event identities and terminal/attempt preservation.
`spot_host.verify_spot_restore(bundle: dict) -> str` must independently verify
completeness against authoritative acknowledged storage, exclusive ownership
and exact software/claim identity, then return `content_sha256(bundle)`.
Rehashing the supplied bundle alone is invalid host verification. Missing,
unacknowledged or mixed tails refuse. There is no force restore. Failed production
restore inhibits that instance; construct a fresh one after investigation.

Hosts assembling a snapshot after an interrupted callback must retain every
newer committed intent, fill, attempt and terminal record, and replay those
facts into the public execution fields before submitting the bundle for
verification. They must never drop an unknown request. The engine reuses its
existing ledger to replay confirmed events; it does not resume strategy history.
Successful restore always inhibits entries, preserves terminal mode and saved
IDs/receipts, and requires fresh observation/reconciliation for exposure work.
Risk-history continuity remains unavailable, as in Testnet recovery.

Offline tests exercise injected broker evidence; they do not establish current
Binance filters, transport correctness, authenticated account acceptance or
cross-platform/process durability. Each host must independently verify its
adopted package bytes and its own composition.

Update this file when: production identity, public controls, evidence, host
authorization, archive verification or terminal custody behavior changes.
