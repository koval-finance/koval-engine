# Execution realism review — 0.11

Purpose: findings, source rationale, and the limits of the release evidence.
Reviewed on 2026-09-11; all external sources below are primary project or venue
documentation. This review does not assert strategy profitability.

## Findings addressed

| Finding | Correction | Regression coverage |
|---|---|---|
| Live runtime ignored dynamic take-profit | Snapshot both hooks; validate and replace protection | `tests/engine/test_runtime_contract.py`, public runtime fixtures |
| Factory dropped optional execution evidence | Pass all five inputs and identify their contents | `tests/engine/test_run_identity.py` |
| Identical profile names hid different inputs | MIT market, candle-stream, evidence and run identity contracts | `tests/engine/test_run_identity.py` |
| Spot short halted paper while plugin continued | Stable soft refusal; later longs remain possible | runtime contract and fixtures |
| Direct spot broker bypassed leverage guard | Validate in the broker constructor too | run identity suite |
| Empty release directory passed source validation | Required-artifact flag in release workflow | `tests/test_dist_release_gate.py` |
| Canonical evidence admitted extra or misaligned candles | Require aligned half-open ranges, venue and source | `tests/engine/test_realism_review.py` |
| Funding ended silently | Enforce supplied coverage at every bar | realism review suite |
| Cancellation/replacement latency was inert | Reject unsupported nonzero values | realism review suite |
| Contract multiplier disagreed with cash accounting | Refuse non-unit contract size in base-quantity broker | realism review suite |
| Non-quote fees could be debited as quote cash | Check recognized symbol quote and reject conversion | realism review suite |
| Partial exits lost cost attribution | Accumulate spread and slippage for every delta | realism review suite |
| Sandbox conditional orders used a retired API | Default Algo Service routing, child fills, verified cancellation and orphan discovery | `tests/exchanges/test_binance_algo.py` |
| Replacement could validate against an intermediate bracket | Validate the final pair before any change; preserve old sandbox orders until acceptance | `tests/exchanges/test_dynamic_protection.py` |
| Stop-only updates bypassed accepted-price reporting and cancellation confirmation | Apply paper tick normalization; return sandbox normalized prices and retain uncertain orders | realism review and dynamic protection suites |

## Source rationale

[NautilusTrader's backtesting architecture](https://nautilustrader.io/docs/latest/concepts/backtesting/)
shares strategy and system components across simulation and live execution.
The corresponding Koval design choice is to publish contracts and validation on
the MIT side and require independent plugins to consume the same evidence and
conformance cases. Sharing an indicator implementation alone is insufficient.

[Backtrader's order execution documentation](https://www.backtrader.com/docu/order-creation-execution/order-creation-execution/)
describes next-bar execution and favorable-open price improvement for limit
orders. Existing Koval fixtures already cover those rules. The added hook
fixtures check that a decision at a bar close cannot change an earlier fill
inside that same bar.

[Freqtrade's lookahead analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
compares baseline and sliced runs to detect future-data dependence. Koval's
existing closed-bar/HTF tests remain necessary; stricter evidence bounds prevent
extra candles from being labelled as part of an earlier requested interval.
This is not a claim to have implemented Freqtrade's complete analysis tool.

[HftBacktest's fill-model documentation](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)
explains that replay cannot alter historical market depth and that queue position
requires observations or assumptions. Koval therefore retains its explicit
OHLCV-proxy label. A volume cap, calibrated impact coefficient, or deterministic
latency value cannot establish historical queue position or guarantee fills.

[Binance's changelog](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/change-log)
dates the USD-M conditional-order migration to 2025-12-09.
[The current trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
documents separate algo identifiers, actual child-order identifiers and separate
open-order queries. The adapter now treats these as distinct lifecycle records;
an algo trigger is not itself a priced fill.

[WhiteBIT's market documentation](https://docs.whitebit.com/concepts/markets)
distinguishes spot pairs and perpetual symbols, and
[its public futures response](https://docs.whitebit.com/api-reference/market-data/available-futures-markets-list)
identifies the quote currency and funding interval. Do not silently equate a
WhiteBIT perpetual symbol with a Binance pair or assume identical funding times.
WhiteBIT remains public-data plus paper; no execution route was introduced.

## Acceptance that remains external

The engine verification gate proves its local implementation and fixtures.
A compatible plugin must still reproduce the complete matrix against the built
0.11 wheel, retire stale exemptions and adopt the MIT identity vocabulary. Its
package dependency range must admit engine 0.11. An independently changing
working tree is not a released compatible pair.

Authenticated Binance testnet behavior, exchange-side trigger races, outages and
precision filters require a supervised sandbox acceptance run. No credentials
or real orders are needed or used by the automated suite. Sandbox fills do not
establish production venue liquidity parity.

The host must archive normalized inputs and raw evidence, implement durable
recovery, preserve run versions, and compare backtest/paper drift. Lower-timeframe
or order-book replay, multi-asset collateral, non-unit contracts, delayed
cancellation/replacement, and validated production execution are not supplied
by this release. These limitations must remain visible to users.

## Local candidate validation and plugin handoff

The final engine candidate passed `./scripts/verify.sh` on Python 3.13:
**1196 passed, 6 skipped, 6 deselected**, plus lint and formatting. Wheel and
sdist passed `scripts/check_dist.py --require-artifacts` and
`twine check --strict`. An isolated installed-wheel smoke run loaded all 13
public fixtures, built a paper broker, ran a graph, and checked the emitted
identity without importing the engine checkout.

On 2026-09-11, an isolated copy of the concurrently changing plugin was installed
with the engine candidate wheel. Its `pyproject.toml` called itself `0.11.0` but
still required `koval-engine>=0.10.0,<0.11.0`. Installation used `--no-deps` only
for this diagnostic; `pip check` correctly rejected the pair. Neither the sibling
checkout nor its virtualenv was changed, and Backtrader was not installed into
the engine virtualenv.

The following five plugin suites produced **231 passed, 9 failed**:

```bash
python -m pytest tests/test_paper_parity.py tests/test_engine_signal_parity.py \
  tests/test_market_identity.py tests/test_run_identity.py \
  tests/test_parity_fixtures.py -q
```

This was a selected-suite diagnostic, not a complete plugin acceptance run.
Snapshot fingerprint:
`7c9868fae047e80af9f9e688de24e0231f74e58ea08bb3beab44076b6b3410fb`.
The SHA-256 preimage concatenates relative POSIX path, NUL, file bytes, NUL for
`pyproject.toml`, `src/**/*.py`, and `tests/**/*.py`, sorted by relative path.
Later sibling edits are not covered by this measurement.

| Observed issue | Required downstream action |
|---|---|
| Dependency upper bound excludes the candidate | Admit the supported 0.11 range, install normally, and require `pip check` to pass |
| Four favorable limit-gap scenarios and one unaffordable stop-entry scenario have stale waivers | Remove only proven stale exemptions; retain exact ledger/fill comparison |
| `long_dynamic_target_v2` and `short_dynamic_target_v2` report no completed trades | Consume the fixture's realistic profile and `strategy_hooks`; reproduce the specified after-bar boundary and +5 PnL |
| `long_entry_bar_ambiguity_v2` reports no completed trade | Reproduce entry-bar protection and the conservative stop-first result |
| Fixture coverage test assumes only contract v1 and one direct setup | Exercise every shipped v1/v2 fixture, including the three additions, without skipping or weakening expected outcomes |
| Plugin identity tests pass their own existing format | Separately migrate to the MIT `MarketIdentity`, evidence manifest and run identity builders; passing an older schema's tests is not interoperability proof |

After those changes, run the plugin's complete gate against a normally installed
final engine wheel, then compare both runtimes' identity fields and full
conformance matrix. The single spot-short fixture passed this snapshot's selected
suite; the engine additionally tests that a permitted long can follow a refusal.

Update this file when: a finding is reopened, its regression coverage changes,
or new measured cross-runtime or sandbox acceptance evidence is available.
