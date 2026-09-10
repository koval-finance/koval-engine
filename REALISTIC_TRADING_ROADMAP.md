# Koval Realistic Trading Roadmap

Repository scope: **koval-engine**  
Status: **engine implementation complete; cross-repository acceptance pending**  
Last reviewed: **2026-09-10** (implementation review complete; see below)

## Purpose

This document is the koval-engine part of the cross-repository program that
makes Koval a trustworthy strategy runner for historical backtests, live paper
sessions, and Binance sandbox sessions. The target is not to predict live PnL.
The target is to make every simulated result causal, reproducible, conservative,
versioned, and explicit about effects that are unavailable.

The companion file has the same name in `koval-backtrader`, `koval-app`, and
`koval-landing`. The release order for every observable execution change is:

1. publish the MIT contracts, primitives, paper behavior, and fixtures here;
2. publish a compatible `koval-backtrader` implementation;
3. pin both releases and expose the contract in `koval-app`;
4. update `koval-landing` claims only after end-to-end acceptance passes.

## Boundaries

- Preserve the no-real-money invariant. Only `paper` and `binance_sandbox`
  execution modes exist.
- Keep Backtrader and all GPL-derived code outside this repository.
- Add no runtime dependency without the separately approved dependency process.
- Preserve released execution versions. Corrected or richer semantics use a new
  version unless a documented defect policy explicitly permits an in-version fix.
- A missing venue effect is `unavailable`, never silently observed as zero.
- The application owns durable evidence storage and promotion policy. This
  repository owns public acquisition primitives, normalized contracts, paper
  execution, account state, and shared parity fixtures.

## Current baseline

`paper_ohlcv_fixed_v1` remains immutable for reproducibility.
`paper_ohlcv_realistic_v2` adds conservative same-bar protection, favorable
limit-gap handling, actual-fill risk enforcement, and an explicit
equal-timestamp policy. The engine now also publishes canonical OHLCV evidence,
funding and fee evidence, instrument and mark-price risk primitives, a calibrated
partial-fill/latency proxy, capability negotiation, and broader conformance
scenarios. Binance sandbox execution remains supervised and fail-closed.

Checked task boxes below mean the MIT engine implementation and its focused
tests exist in this repository. They do not mean the cross-repository acceptance
paragraph has passed; the verification checklist records those remaining gates.

## P0 — Correctness and backtest-to-paper parity

### ENG-001 — Correct favorable limit-gap matching

- [x] Add a failing regression fixture for long and short limit entries whose bar
  opens through the limit at a better price.
- [x] Fill at the favorable open before applying adverse modeled costs, while
  never violating the submitted limit after costs.
- [x] Cover the case where the open is already marketable but the remaining bar
  range does not cross the literal limit.
- [x] Publish the fixture through `koval.examples.parity_fixtures` for the plugin
  and application contract suites.

Acceptance: the MIT paper broker and the released Backtrader plugin produce the
same reference price, actual fill, costs, position, and final equity for both
directions.

### ENG-002 — Version realistic protection timing

- [x] Specify a successor to `paper_ohlcv_fixed_v1`; do not silently reinterpret
  historical v1 results.
- [x] Remove the artificial full-bar interval between an entry fill and active
  protection.
- [x] Choose and document one evidence-backed approach: lower-timeframe execution
  bars, or a conservative same-bar OHLC path policy with ambiguity reporting.
- [x] Define equal-timestamp ordering for entry fills, bracket placement, stop,
  take-profit, cancellation, and dynamic replacement.
- [x] Report when OHLC data cannot determine the intrabar outcome and expose the
  selected conservative rule in resolved metadata.

Acceptance: entry-bar stop and target scenarios have shared golden fixtures, no
position is silently left unprotected, and ambiguous bars cannot receive an
optimistic outcome without an explicit policy.

### ENG-003 — Make the broker ledger authoritative for risk

- [x] Feed normalized actual quantity, actual fill price, commissions, funding,
  and realized PnL back into strategy account state.
- [x] Make position sizing validate risk after gap and modeled execution costs,
  not only against the requested setup price.
- [x] Define the daily PnL boundary from an explicit prior balance/equity
  baseline so the first bar and overnight move are not discarded.
- [x] Separate daily-loss gates from drawdown measured against the all-time equity
  peak.
- [x] Reconcile cash, realized PnL, unrealized PnL, fees, funding, and equity after
  every state transition.

Acceptance: risk gates and UI account values are derived from the same broker
ledger, including gaps, fees, session boundaries, restarts, and rejected fills.

### ENG-004 — Expand the shared execution conformance suite

- [x] Cover market, limit, stop, stop-loss, and take-profit behavior for long and
  short positions.
- [x] Cover favorable/adverse gaps, ambiguous bars, cancellation, OCO behavior,
  insufficient margin, end-of-data/open-at-stop behavior, and dynamic SL/TP.
- [x] Add deterministic differential scenarios generated from valid order and bar
  combinations, with minimized regression fixtures for every discovered delta.
- [x] Separate required equality from intentional runtime differences such as a
  live-session flatten, and require an explicit reason code for every difference.

Acceptance: a plugin or app release cannot claim parity by passing only the
original narrow fixtures.

## P0 — Venue, market, and live-session safety

### ENG-005 — Enforce venue/market/mode compatibility

- [x] Define one canonical compatibility matrix shared by adapter and broker
  factories.
- [x] Permit Binance sandbox only for the verified Binance futures sandbox path.
- [x] Keep WhiteBIT execution fail-closed; WhiteBIT remains public-data plus paper
  simulation while no verified public sandbox exists.
- [x] Reject symbol, market, exchange, and mode disagreements before a feed or
  broker starts.
- [x] Make spot semantics real: no synthetic short or leverage above one unless a
  separately named margin product is implemented.

Acceptance: labels are enforced behavior, not provenance-only strings.

### ENG-006 — Keep protection supervision alive during feed outages

- [x] Decouple sandbox order supervision from OHLCV polling and its retry/backoff
  lifecycle.
- [x] Continue polling or receiving broker order events while public market-data
  acquisition is unavailable.
- [x] Add server-time synchronization and measured clock-skew handling for signed
  Binance sandbox requests.
- [x] Define bounded retry, stale-order, orphan-order, and containment outcomes
  with stable reason codes.

Acceptance: a simulated network outage cannot pause protection supervision or
leave exposure without a recorded containment decision.

### ENG-007 — Close live higher-timeframe parity

- [x] Supply live strategies only with higher-timeframe bars that are confirmed
  closed at the primary decision time.
- [x] Record warm-up ranges and timeframe availability in session metadata.
- [x] Keep promotion blocked for graphs requiring unsupported HTF inputs.

Acceptance: mutating a still-open higher-timeframe candle cannot change an
earlier live decision.

## R1 — Reproducible market-data input

### ENG-101 — Expose canonical evidence-ready data

- [x] Return normalized timestamps, venue, market, canonical symbol, timeframe,
  request bounds, actual coverage, gap/duplicate checks, and source metadata from
  acquisition primitives.
- [x] Provide deterministic canonical candle encoding or an explicit versioned
  encoding contract that `koval-app` can archive and hash.
- [x] Distinguish a genuinely observed zero value from unavailable data.
- [x] Keep cache identity market-aware and prevent mutable cache refreshes from
  masquerading as reproduction of an old dataset.

Acceptance: the app can archive exact bytes and later replay them offline without
refetching or guessing adapter defaults.

## R2 — Perpetual funding

### ENG-201 — Add historical funding acquisition contracts

- [x] Normalize signed funding rate, settlement timestamp, settlement mark price,
  interval, source, and coverage for Binance and WhiteBIT perpetual markets.
- [x] Validate complete coverage for the requested run; missing intervals fail the
  R2 assessment rather than becoming zero.
- [x] Keep raw response evidence available to the application archive boundary.

### ENG-202 — Add paper funding cashflows

- [x] Extend the broker/account ledger with explicit funding debit/credit records.
- [x] Define settlement-before-order ordering at equal timestamps.
- [x] Reconcile funding into cash and equity independently from trade PnL and fees.

Acceptance: long and short fixtures cover positive/negative rates, exact boundary
settlement, missing coverage, and cash/equity reconciliation.

## R3 — Venue fee evidence

### ENG-301 — Represent venue fees without present-day leakage

- [x] Add fee currency, maker/taker role, tier/evidence identity, and discount
  treatment to normalized fills and resolved configuration.
- [x] Never apply today's venue/account rate to an old period without labelling it
  as an approximation.
- [x] Negotiate versioned fee capabilities with the plugin and application.

Acceptance: every fee in the ledger identifies its rate, role, currency, source,
and evidence status.

## R4 — Venue constraints, margin, and liquidation

### ENG-401 — Add historical instrument and risk primitives

- [x] Normalize tick size, step size, minimum quantity/notional, price bands,
  contract size, mark price, margin tiers, collateral rules, and liquidation fee.
- [x] Quantize and reject orders at simulation time using evidence valid for that
  interval.
- [x] Implement a tested maintenance-margin and mark-price liquidation state
  machine shared by paper and plugin contracts.

Acceptance: a simulated order could have been accepted by the selected venue and
liquidation reconciles through the same account ledger.

## R5 — Calibrated execution proxy

### ENG-501 — Support partial and size-sensitive fills

- [x] Define cumulative fill quantities, delta fees, stable order IDs, protection
  resizing, entry-remainder policy, partial OCO behavior, and restart recovery.
- [x] Add a shared per-bar volume budget so concurrent orders cannot consume the
  same liquidity independently.
- [x] Replace fixed slippage only when lagged volatility/participation calibration
  evidence exists; otherwise retain and disclose the fixed model.
- [x] Represent decision, submission, acknowledgement, cancellation, replacement,
  and protection latency explicitly.

Acceptance: R5 is permanently labelled a calibrated OHLCV proxy, never an
order-book or queue-position simulator.

## Required verification for every implementation phase

- [x] Write and observe the relevant failing test before changing calculation or
  state-transition behavior.
- [x] Pass focused engine tests and `./scripts/verify.sh`.
- [x] Publish or update shared fixtures and resolved metadata.
- [ ] Run the compatible `koval-backtrader` suite against the candidate engine.
- [ ] Run the installed-wheel contract and promotion tests in `koval-app`.
- [x] Review documentation and release notes without weakening safety guards.

## Post-implementation review (2026-09-10)

The engine implementation above was reviewed end to end against a running
engine, not only against its tests. Six defects were found, reproduced, and
closed test-first; `./scripts/verify.sh` is green.

- **ENG-007** — `confirmed_higher_timeframe_bars` rejected any bucket the source
  window only partly spanned. A live rolling window opens wherever the feed did
  and then slides, so this aborted every session with a configured
  `higher_timeframe`. A partly spanned bucket is now a coverage boundary; a hole
  inside a fully spanned bucket still fails.
- **ENG-401** — `MarkPriceSeries.at` scanned the series on every bar, making a
  run quadratic in its own length. A 30-day one-minute run with the full
  evidence stack took 35.7s and produced identical results in 0.90s once
  indexed; a one-year run was effectively unusable.
- **ENG-201** — funding coverage was validated against an assumed epoch-aligned
  settlement grid and rejected any window containing no settlement. Coverage is
  now judged against the phase the records reveal, and a window narrower than
  one interval may declare `interval_ms` and hold none. Known remaining limit:
  `BinanceAdapter.fetch_funding_history` still needs two settlements in the
  requested range to measure the interval, so a short-window funding series has
  to be assembled by the archiving caller.
- **ENG-202** — funding settlement rescanned every record on every bar; it now
  resumes from a cursor.
- **ENG-006** — `synchronize_clock()` existed but nothing called it, so signed
  sandbox requests always used the raw host clock. `sandbox_factory.build_broker`
  now measures skew when it builds the session's broker; skew beyond the bound
  raises `ClockSkewExceeded`, an unreachable time endpoint does not block.
- **ENG-004** — `compare_execution_results` demanded bit-identical floats. Two
  runtimes computing the same fee in a different operation order disagree in the
  last bits for ~42% of realistic inputs, so the cross-runtime gate would have
  reported float noise as a parity difference. Numbers are now compared within a
  documented `relative_tolerance`; pass `0.0` for bit equality.

Open for the reviewer's judgement, deliberately left as-is:

- `PollingFeed(max_consecutive_failures=5)` ends a session after roughly 15
  seconds of consecutive market-data errors (backoff 1+2+4+8s), which then
  contains exposure. That is a tighter outage budget than a real venue's routine
  error bursts; it is a risk-policy choice, not a defect.
- `paper_legacy_v1` remains the default profile, so a caller that passes no
  execution config still gets the cost-free simulation. Selecting
  `paper_ohlcv_realistic_v2` is the application's decision.

## Completion definition

The engine part of a tier is complete only when its contracts, independent MIT
paper implementation, evidence status, reconciliation equations, failure modes,
and shared fixtures are public and verified. A green engine suite alone does not
authorize an application promotion or a landing-page realism claim.

## Related repository documentation

- `agents_docs/architecture.md` — current module and execution boundaries.
- `agents_docs/exchanges_and_data.md` — adapter, market identity, and cache rules.
- `agents_docs/invariants.md` — safety, licensing, and dependency constraints.

This roadmap is the canonical repo-local backlog for realism work. The related
documents describe current behavior and must be updated alongside each completed
item; they do not mark an unchecked roadmap item as implemented.
