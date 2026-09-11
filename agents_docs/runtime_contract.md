# Runtime and identity contract — 0.11

Purpose: the MIT-owned contract that paper runtimes, backtest plugins, and hosts
consume. This document describes engine behavior; plugin adoption and application
acceptance must be measured independently.

## Versions and compatibility

The package release is `0.11.1`. `ENGINE_PROTOCOL_VERSION` remains `1` because
the plugin spec/result interfaces have no breaking structural change. The
runtime behavior identifier is `koval_runtime_v2`; the run identity schema is
`koval_run_identity_v1`. Both constants are exported from
`koval.engine.run_identity`.

Existing paper fill profiles retain their names and matching rules:
`paper_legacy_v1`, `paper_ohlcv_fixed_v1`, `paper_ohlcv_realistic_v2`.
The default remains cost-free legacy. The fixed profile still delays protection
by one bar. The realistic profile must be selected explicitly.

Defect policy for 0.11: honoring a previously ignored strategy hook, refusing
unsupported inputs, and correcting metadata/accounting attribution are runtime
fixes, not changes to v1 fill arithmetic. Replaying an old run requires its
recorded package version as well as its profile. Do not substitute 0.11 for 0.10
when reproducing a strategy with `on_tp_update`.

## Account binding and replay endings in 0.11.1

`DeclarativeStrategy.bind_account(provider)` accepts a zero-argument callable
returning an immutable `AccountSnapshot`. `account_snapshot()` reads it, or
returns `None` for a standalone strategy. LiveEngine binds its ledger-backed
account before execution; plugins bind their independently maintained account.
Graph contexts prefer this provider and do not book requested setup callbacks
into a second account. Standalone graphs retain the previous fallback.

Before each `on_bar`, the runtime processes funding and fills, updates account
quantities/margin, marks remaining exposure at the candle close, and injects
state. `on_open_position` receives a copy of the setup with actual fill price,
filled quantity and accepted protective levels. It fires once per position;
subsequent partial quantities are read from account snapshots. Hooks run within
fill processing; the complete bar-close equity is available at `on_bar`.

`LiveEngineConfig.end_of_data_policy` accepts `flatten_at_last_close` (default)
or `mark_at_last_close` (paper only). On natural feed exhaustion the latter
cancels unfilled entries, retains protected exposure and marks it at the last
close, matching the backtest plugin. It does not simulate an exit fee or create
a closed trade. A stop signal or error still flattens; the terminal run identity
records the applied policy. This is a completed simulation result, not a durable
resume/checkpoint mechanism. Sandbox containment behavior is unchanged.

PaperBroker acknowledges its already-matched protective legs using the
`protection_reference` snapshot. A same-bar partial exit does not restore the
pre-exit quantity. A gap beyond an accepted bracket remains a simulated market
containment fill, while external broker fills still require strict protection
validation.

## Dynamic protection

`LiveEngine` reads `on_sl_update(trade_id)` and then `on_tp_update(trade_id)`
after the current bar has been matched. Both values are captured before any
order changes. `None` means keep that leg. A target update invokes
`Broker.modify_protection(stop_price=..., target_price=...)`; stop-only callers
retain the `modify_stop` path for broker compatibility.

`koval.engine.protection.validate_protection_update` defines the common rule:

- All levels are positive and finite.
- A long stop cannot decrease; a short stop cannot increase.
- The final long stop is strictly below the final target; the final short stop
  is strictly above it. Validate against the new pair, not a partially changed
  pair. Targets may move closer or farther away.
- Entry price is not a constraint on dynamic updates. A stop can lock in profit.
- Invalid updates change neither paper leg. Broker failures or unsupported
  hooks trigger containment and a clear runtime error.

Paper replaces both values together after matching and uses them on the next
processed bar. It also updates a carried entry remainder. Supplied instrument
evidence normalizes both replacement prices and validates constraints again.
The strategy/account snapshot uses accepted paper prices, not requested prices.
Stop-only paper updates use the same validation and normalization.

Binance uses two independent reduce-only orders. Replacement validates both
levels, places both new legs, requires accepted acknowledgements, and only then
cancels the old pair. This is supervised replacement, not an exchange-atomic
operation. All attempted IDs stay tracked through uncertain outcomes so
containment can cancel and reconcile them. A rejection, fill during replacement,
or unconfirmed old-order cancellation fails closed.
Stop-only sandbox replacement likewise verifies acceptance and cancellation,
preserves identifiers on uncertainty, and returns the normalized accepted price.

## Spot refusal

`spot` permits longs with leverage exactly one. A short setup returns a paper
acknowledgement with `status="rejected"` and `reason="spot_short_unsupported"`.
`LiveEngine` emits `ORDER_REJECTED`, clears the pending intent and continues.
It may take a permitted long later. Invalid setup shape is still an error.
The lower-level `submit_bracket` raises `PaperOrderRejected`, a `ValueError`
subclass with a `reason` attribute. Insufficient margin follows the same soft
refusal policy with `reason="insufficient_margin"`.

## Market vocabulary

Import `MarketIdentity`, `resolve_market_identity` and `canonical_symbol` from
`koval.engine.market_identity`. Import `assert_supported_market` and
`compatibility_for` from `koval.exchanges.execution_compatibility`.

| Field | Canonical value |
|---|---|
| `exchange` | `binance` or `whitebit` |
| `market` | `spot` or `future`; `futures`, `usdm`, `usd_m` normalize to `future` |
| `canonical_symbol` | uppercase venue symbol with `/` and `_` removed |
| `contract_type` | `spot` for spot; `perpetual` for the supported futures product |

Delivery/inverse products are not implemented by this runtime contract and are
rejected when requested. Identity validation is not a historical listing check:
instrument evidence is still needed to establish that a venue accepted a symbol
and its order constraints at a historical timestamp. In particular, WhiteBIT's
native `BTC_PERP` is `BTCPERP`; do not invent equivalence with `BTCUSDT`.

## Evidence transport and identity

`SandboxBrokerConfig`, `LiveEngineConfig`, and `PaperBroker` accept the same five
inputs: `funding`, `fee_schedule`, `instrument_specs`, `mark_prices`, and
`execution_proxy`. The factory passes them through. A supplied paper broker is
authoritative; an explicit conflicting engine profile, market, venue, symbol,
starting capital, or evidence input is rejected before processing begins.
An omitted engine evidence input does not erase evidence already on the broker.

`PaperBroker.execution_evidence` and `resolved_metadata["execution_evidence"]`
contain a manifest with these five keys. Each entry has `status` (`supplied` or
`unavailable`) and `sha256` (or `None`). `supplied` identifies an input; it does
not assert historical coverage, calibration quality, or venue equivalence.
Fee/instrument provenance and effective intervals remain in resolved metadata
and fill records. The fixed configured fee remains an explicit approximation
when there is no venue fee schedule.

`execution_evidence_manifest` and `content_sha256` are public. Hashing uses
compact sorted-key UTF-8 JSON, rejects non-finite JSON numbers, converts tuples
to arrays and Decimal values to strings, and includes public dataclass fields.
Private caches and `raw_responses` are excluded. Archive normalized evidence as
well as the original responses; raw HTTP response ordering does not change the
identity of normalized execution inputs. Reusing an evidence ID with different
rates, marks, intervals, or rules changes the content hash.

## Run identity

Every paper status and trade record includes `run_identity`. Status snapshots
identify the candle prefix processed so far; the final terminal status names
the completed run. Trade records therefore carry a prefix identity, not the
identity of later unseen candles.

`build_run_identity` returns a JSON-compatible record with:

- `schema_version`, `engine_version`, and `execution_mode`;
- `market_identity`, or `None` when no venue was declared;
- `dataset_identity.primary` and `.warmup`, naming actual consumed data;
- `execution_identity`: resolved profile, evidence manifest, runtime contract
  version, and a content SHA-256;
- `strategy_sha256` and `run_parameters`, including capital, window size, HTF
  requirements, daily baseline, peak equity, and terminal policy;
- `reproducibility_grade` and `unavailable_effects`.

`LiveEngineConfig.exchange` is optional for backward compatibility. Set it, or
inject a factory-built broker with its known exchange, to obtain market identity.
Unknown venue, empty primary input, and sandbox runs are `not_comparable`.
A named paper simulation with consumed input is `identified_simulation`. That
grade means the inputs have identities, not that the host archived them, that
the strategy is deterministic outside the engine, or that all venue effects
were available. The host owns archive verification and promotion decisions.

`CandleStreamIdentity(timeframe)` hashes candles with constant memory. Its
preimage is `b"KOVAL-CANDLE-STREAM-V1\n"` followed by
`canonical_candle_bytes(row.reshape(1, 6))` for every consumed row, in order.
The encoding identifier is `koval_candle_stream_sha256_v1`. The snapshot includes
row count and actual bounds, with an exclusive end. Warm-up contains only the
history tail actually retained by the configured window. The batch
`CandleDataset` format remains `koval_candles_f64le_v1`; its hash is deliberately
different. Never compare hashes from different encoding versions.

Plugins can use these same builders over their actual inputs and put the record
in `BacktestResult.metrics["run_identity"]`. Compare strategy, market, candle
encoding/hash, warm-up, execution/evidence, parameters, and compatible runtime
versions before comparing output. Different terminal policies require explicit
reason-coded result differences. Package versions and profile names alone are
not a parity proof.

## Public fixtures for plugin migration

`koval.examples.parity_fixtures()` adds:

- `long_dynamic_target_v2`: next-bar exit at 105, one unit, zero costs, +5 PnL;
- `short_dynamic_target_v2`: next-bar exit at 95, one unit, zero costs, +5 PnL;
- `spot_short_rejected_v2`: no position, unchanged equity, soft rejection.

These use the existing `requires_direct_setup` / `setup` fields and optional
`strategy_hooks`.
Hook actions name `after_bar_index` (zero-based, after matching that candle) and
`value`. The fixture strategy submits one setup and does not re-enter after
opening. An empty hook list means no update. Old fixture bytes and checksums are
unchanged. The additional checksum entries pin the new files.

`generate_conformance_scenarios()` adds both dynamic-target directions and a
spot refusal. `ConformanceScenario.market` defaults to `future`.
`ConformanceAction(kind="modify_target", value=...)` maps to
`modify_protection(target_price=value)` at the indicated after-bar boundary.
No waiver may hide an ignored target or a difference in spot-refusal behavior.

## Supported realism limits

- Paper accounting uses base-asset quantity and quote cash. Instrument
  `contract_size != 1` is rejected instead of producing inconsistent PnL,
  funding, and liquidation. Multi-currency fee conversion is unavailable;
  named-symbol fees must match the recognized quote currency.
- Legacy profiles reject supplied fee schedules because legacy does not charge
  fees. A low-level unnamed bracket assumes the caller supplied quote-denominated
  fee evidence; session APIs validate the configured symbol.
- Funding evidence must cover every processed bar timestamp, including flat
  bars. Extending a live run requires corresponding evidence; exhaustion fails
  rather than becoming zero funding.
- Nonzero cancellation/replacement latency is rejected by `PaperBroker`.
  The shared latency record can describe it, but the current paper loop has no
  delayed-mutation scheduler. Submission, acknowledgement, fill eligibility and
  protection activation remain supported OHLCV timing assumptions.
- Partial-exit reports sum spread/slippage from every fill delta. Funding
  remains a separate account cashflow, not silently assigned to trade PnL.

## Binance conditional API

`BinanceSandboxBroker(conditional_order_api="algo")` is the default in 0.11.
Stop entries and protection use `/fapi/v1/algoOrder`; normal market/limit orders
use `/fapi/v1/order`. Polling follows `actualOrderId` to the child order and
attributes fees from that actual order's `userTrades`. Cancellation is verified
by querying the algo, and reconciliation checks both ordinary and algo open
orders even without saved intents. Unknown statuses fail closed.

`conditional_order_api="legacy"` explicitly preserves the old testnet wire
protocol. Do not select it for a server that requires Algo Service. Transport
selection is recorded in `binance_sandbox_v2` resolved metadata. Production
origins and additional execution modes remain forbidden. Automated tests use
mocked HTTP only; authenticated testnet acceptance is a separate release gate.

Update this file when: runtime ordering, identity fields or encodings, supported
evidence, public fixtures, or sandbox wire protocols change.
