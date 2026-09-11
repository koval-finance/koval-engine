# Koval Realistic Trading Roadmap

Repository scope: **koval-engine**  
Status: **0.11.0 engine candidate implemented and locally verified; downstream acceptance pending**
Last reviewed: **2026-09-11** — ENG-008 and ENG-010 through ENG-014 are implemented
in the 0.11.0 candidate. The earlier measurement against released 0.10.0 remains
historical evidence, not acceptance of the new pair. See the 0.11 review below.

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

### ENG-008 — Honor `on_tp_update` in the live and paper runtime

The 0.10.0 differential harness correctly found that paper ignored this hook.
Implemented for 0.11.0 under `koval_runtime_v2`:

- [x] Observe failing long/short target regressions, then call both protection
  hooks after bar matching.
- [x] Snapshot both requested legs before applying a replacement. Paper updates
  them together; sandbox accepts the new reduce-only pair before canceling the
  old pair and contains any uncertain outcome.
- [x] Publish `validate_protection_update`: positive finite levels, a stop that
  never increases risk, and strict ordering of the final pair. Targets can move
  in either direction; dynamic stops may lock profit beyond entry.
- [x] Publish `long_dynamic_target_v2` and `short_dynamic_target_v2` through
  `koval.examples.parity_fixtures`, with explicit after-bar hook actions.

Acceptance still required downstream: the plugin passes these fixtures against
an installed 0.11 wheel and removes `paper_runtime_ignores_take_profit_updates`.
The exact contract is in [runtime_contract.md](agents_docs/runtime_contract.md).

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

## P0 — Release identity

### ENG-009 — Make a published version identify the code

Closed on 2026-09-10: the published distribution **is** the code in this
repository. The report this item was opened for measured a stale local build
and attributed its behavior to the release.

`koval-engine==0.10.0` was uploaded to PyPI at 2026-09-10T12:02:56Z by the
tag-triggered release workflow, from `9b7ff22` — the commit `v0.10.0` points
at. Both published artifacts carry this tree's engine sources exactly, with no
missing, extra, or altered file:

```console
$ python scripts/check_dist.py <downloaded pypi artifacts>
koval_engine-0.10.0-py3-none-any.whl: matches src/koval at 0.10.0
koval_engine-0.10.0.tar.gz: matches src/koval at 0.10.0
```

Run against the published wheel in a clean virtualenv, both demonstration cases
behave exactly as this repository does: the buy limit fills at the favorable
open of 96.0, and the stop entry that would need roughly 1096 of margin against
1000 of capital is rejected, leaving 1000.00 free. No stored result recording
`koval-engine: 0.10.0` from PyPI is ambiguous.

The 694-line `paper_broker.py` belongs to
`dist/koval_engine-0.10.0-py3-none-any.whl`, a local build from 2026-09-06 that
already carried `version = "0.10.0"` four days before the release commit.
`dist/` is gitignored, so it never reached PyPI — it reached the sibling
checkouts instead. Both record a local file install rather than a PyPI one, and
they do not even record the same file:

```console
$ cat …/koval-backtrader/.venv/…/koval_engine-0.10.0.dist-info/direct_url.json
{"archive_info": {"hash": "sha256=55392aa1…"},
 "url": "file:///…/koval-engine/dist/koval_engine-0.10.0-py3-none-any.whl"}
$ cat …/koval-app/.venv/…/koval_engine-0.10.0.dist-info/direct_url.json
{"archive_info": {"hash": "sha256=78d95008…"},
 "url": "file:///…/koval-engine/dist/koval_engine-0.10.0-py3-none-any.whl"}
```

Three builds carried the string 0.10.0 on this machine; only one of them is a
release. The reproducibility failure was real, but it lived in the local
install path, not in the published version.

- [x] Establish which build PyPI carries by comparing both published artifacts
  against `src/koval` at the tag. They match, so `main` stays 0.10.0: there is
  no second execution contract to disambiguate with a 0.11.0, and nothing to
  yank.
- [x] Add a release gate that fails when a built artifact's sources differ from
  the tree — `scripts/check_dist.py`, run by the release workflow between
  `python -m build` and the artifact upload, and by the default test suite
  against a local `dist/` so a stale artifact cannot sit where an install or a
  parity measurement can find it.
- [x] Re-run the `koval-backtrader` differential harness against the released
  engine. Done 2026-09-11. The plugin venv loads `koval-engine 0.10.0` from
  PyPI (`INSTALLER: pip`, no `direct_url.json`, `paper_broker.py` at 1656
  lines). Result: `paper_limit_entry_ignores_favorable_open`,
  `paper_limit_entry_requires_range_touch` and
  `paper_affordability_not_rechecked_at_fill` no longer reproduce and are the
  plugin's to retire; `paper_runtime_ignores_take_profit_updates` still does
  (ENG-008). The plugin's suite is red on this — its stale-exemption guard
  refuses to let a fixed divergence sit unnoticed, which is the guard working.
  Tracked there as BT-005.

Acceptance: `pip install koval-engine==<version>` yields byte-identical
execution behavior to this repository at the corresponding tag, and a downstream
result recording an engine version can be replayed from that version alone. Met
for 0.10.0 by the comparison above; the gate keeps it true for the next release.

## P0 — Cross-repository contract gaps

Opened 2026-09-11 from the first end-to-end read of this repository against
`koval-backtrader` at its 0.11.0 working tree. Each item is a place where the
two runtimes, or this repository and its own published claims, do not agree.
None of them is caught by either suite today, because each lives in the gap
between them.

### ENG-010 — Preserve evidence through broker construction

- [x] `SandboxBrokerConfig` accepts funding, fee schedules, instrument specs,
  mark prices, and execution proxy; `build_broker` forwards all five.
- [x] `PaperBroker.execution_evidence` identifies each supplied normalized input
  by content hash; absent inputs have explicit `unavailable` status.
- [x] An injected paper broker is checked against explicitly configured profile,
  market, venue, symbol, capital, and evidence before processing begins.

Acceptance here: factory and direct construction expose the same evidence
manifest for the same inputs; content changes remain distinguishable even when
an evidence ID or profile name is reused. Plugin adoption remains external.

### ENG-011 — Publish the MIT run identity contract

- [x] Own normalized identity in this MIT repository; hosts own archive retention,
  archive verification and promotion decisions.
- [x] Publish `koval_run_identity_v1`, `build_run_identity`, `content_sha256`,
  `execution_evidence_manifest` and constant-memory `CandleStreamIdentity`.
- [x] Include actual market, candle/warm-up identity, execution version/evidence,
  graph hash, run parameters, package version, and reproducibility grade in live
  statuses and trade records.
- [x] Grade unspecified venue, empty input and sandbox runs `not_comparable`;
  `identified_simulation` does not certify historical realism or archive retention.

Acceptance here: actual input prefixes are recorded; the terminal status records
all consumed candles. The plugin must import this vocabulary before applications
compare the runtimes. Do not claim both released runtimes already implement it.

### ENG-012 — Settle the spot-refusal policy

- [x] Choose `reject_and_continue`: valid spot-short setups receive
  `ORDER_REJECTED` with `spot_short_unsupported`; a later long may trade.
- [x] Preserve a typed low-level refusal (`PaperOrderRejected`, a `ValueError`
  subclass), with the stable reason exposed by `submit_entry` acknowledgements.
- [x] Publish `spot_short_rejected_v2` and add a market-aware conformance scenario.
- [x] Close the direct-constructor spot leverage bypass.

Acceptance here: paper rejects without exposure or session halt. Downstream
parity must set the scenario market rather than silently using futures.

### ENG-013 — Name the market vocabulary both runtimes must use

- [x] Publish `MarketIdentity` and `resolve_market_identity` in the MIT engine.
- [x] Record `spot` / `future`, with `spot` / `perpetual` contract types. Normalize
  supported futures spellings; refuse delivery/inverse products and unknown venues.
- [x] Reuse exported `assert_supported_market` and `compatibility_for` so venue
  validation has one owner.

Acceptance here: identity builders and runtime evidence checks share the same
vocabulary. Plugin-local `futures` identity must migrate to canonical `future`.

### ENG-014 — Require artifacts in release verification

- [x] Add `--require-artifacts`; an empty/missing directory exits nonzero.
- [x] Use it in the release workflow after building and before upload.
- [x] Keep permissive local-`dist/` checks when the flag is absent.

Acceptance: a release gate cannot pass without comparing an artifact; the local
suite still supports a checkout that has never built a distribution.

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
- [x] Run the compatible `koval-backtrader` suite against the candidate engine.
  Done 2026-09-11 against released 0.10.0. It exits 1, and every failure is
  that repository's to fix: five stale divergence exemptions, two fixture
  assertions that predate `long_entry_bar_ambiguity_v2` and the second
  direct-setup fixture, and one test still encoding the pre-ENG-003 daily-PnL
  boundary. No failure indicts the engine. Tracked there as BT-005 and BT-006.
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

## 0.11 implementation review (2026-09-11)

The candidate closes the six previously unchecked engine items above and adds
regressions for evidence bounds, exhausted funding coverage, partial-exit cost
attribution, quote-fee validation and unsupported contract multipliers. Nonzero
cancellation/replacement latency now fails explicitly: its earlier appearance in
metadata did not mean the paper loop scheduled those effects. Earlier checked
R5 boxes must be read with this current limitation.

The source review also found Binance's conditional API migration. The candidate
now defaults to Algo Service, tracks the actual child order for fills/fees,
verifies cancellation, and checks both ordinary and algo open orders during
reconciliation. An explicit `legacy` wire option remains for older testnet
integrations. The testnet origin allowlist and the two execution modes are
unchanged. HTTP mocks do not replace authenticated sandbox acceptance.

Primary sources, regression coverage, and remaining limitations are recorded in
[realism_review.md](agents_docs/realism_review.md); plugin migration details are in
[runtime_contract.md](agents_docs/runtime_contract.md). The prior 0.10 plugin
measurement below cannot serve as acceptance for these new contracts.

Local engine verification: **1196 passed, 6 skipped, 6 deselected** on Python
3.13, with lint/formatting and built-artifact checks passing. An isolated plugin
snapshot produced **231 passed, 9 failed** across five selected suites and failed
`pip check` because its dependency range still excluded engine 0.11. This is a
diagnostic of that snapshot, not acceptance of the concurrently changing plugin.
The exact failures and handoff are recorded in the review document above.

- [x] Observe focused failures before behavioral fixes.
- [x] Publish MIT contracts and additional immutable parity fixtures.
- [x] Run the engine's complete verification gate.
- [ ] Obtain a compatible released plugin's full passing suite against the final
  0.11 wheel, with its dependency range and shared identity vocabulary updated.
- [ ] Run supervised authenticated Binance testnet acceptance for the Algo API.
- [ ] Run installed-wheel application contract tests after application integration.

## What koval-app needs for matching backtest and paper results

Recorded here and mirrored in `koval-backtrader` so neither repository has to be
read to know what the other owes. Nothing in this section is application work;
it is what this repository and the plugin must publish before the application
can honestly show a backtest and a paper session side by side.

**Owned by this repository**

1. Release identity is settled: `koval-engine==0.10.0` on PyPI is this tree at
   `v0.10.0`, and `scripts/check_dist.py` gates every later release (ENG-009).
   An application that pins the version and installs from PyPI gets exactly one
   execution contract; installing from a local `dist/` is what produced the
   second one.
2. Honor `on_tp_update` in 0.11.0 (ENG-008), publishing its ordering and
   replacement validation as `koval_runtime_v2`.
3. Keep `paper_ohlcv_fixed_v1` immutable, and publish `paper_ohlcv_realistic_v2`
   with the fixtures the plugin has to reproduce.
4. Ship the parity fixtures for every scenario class the plugin declares a
   divergence for, so agreement is proven from shared evidence rather than from
   each side's own tests.

**Owned by koval-backtrader**

1. Retire the divergence exemptions its stale-exemption guard names, and get
   its suite green again (BT-005, BT-006 there). Five of six no longer
   reproduce against the released engine.
2. Implement the successor execution-model version against
   `paper_ohlcv_realistic_v2` without changing `ohlcv_fixed_v1` replay
   (BT-002 there). This is the critical path: until it lands, the only profile
   pair both runtimes implement is the least realistic one either offers, so a
   realistic paper session has no comparable backtest.
3. Consume the funding, fee-role, instrument and liquidity contracts, which are
   released, rather than approximating them.
4. Fix its account wiring (BT-007 there). `strategy.account` is a backtest-only
   attribute this repository never reads, graph strategies still size from
   requested prices, and `on_stop_moved` re-opens the position instead of
   calling `PlatformAccountState.on_stop_update`, bypassing the
   "must not increase risk" guard the live runner enforces.

**The contract the application depends on**

- One profile pair per run: a backtest on `ohlcv_fixed_v1` is comparable only to
  a paper session on `paper_ohlcv_fixed_v1`, and the same for the successor
  versions. Mixing profiles is not a supported comparison and the application
  should refuse it rather than render it.
- `paper_legacy_v1` remains the default when no execution config is passed, so
  an application that omits one gets the cost-free simulation. Selecting a
  realistic profile is the application's explicit decision.
- Engine 0.11 publishes and records market, data/evidence and execution identity.
  The plugin must adopt the MIT contract before the application compares runs.
  A result missing any identity is not comparable; `identified_simulation` is
  input identity, not a guarantee of archival completeness or venue realism.
- An effect that is unavailable is reported as `unavailable`, never as zero.
  Rendering a missing funding cashflow as `0.00` is a claim the engine did not
  make.

Acceptance for the application tier, once both repositories have released: the
same graph, candles, capital and profile produce the same fills, the same cost
attribution and the same closing equity in a backtest and in a paper session,
and any remaining difference is one the plugin declares with a reason code.

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
