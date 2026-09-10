# Exchanges and data

Purpose: how market data enters the system, and why execution is
deliberately limited. Read [invariants.md](invariants.md) before touching
anything here — this package's safety posture lives in this layer.

## Data adapters (read-only)

- `src/koval/exchanges/base.py` — the adapter ABC (`fetch_ohlcv`, `metadata`,
  `capabilities`).
- `src/koval/exchanges/capabilities.py` — `VenueCapabilities`: the market an
  adapter serves, its data environment, the venue's entry order types and the
  provenance class of its fee schedule, funding history and symbol spec.
- `src/koval/exchanges/markets.py` — canonical `spot` / `future` names and each
  venue's historical default market.
- `src/koval/exchanges/binance.py`, `src/koval/exchanges/whitebit.py` —
  read-only REST adapters for OHLCV, venue metadata, and normalized perpetual
  funding history. Spot funding requests fail explicitly as unavailable.
- `src/koval/exchanges/execution_compatibility.py` — the canonical matrix for
  venue, market, and mode combinations. Both data and broker factories consult
  it before starting work.
- HTTP in tests is faked with the `responses` library; no test may hit a
  real network endpoint.

## OHLCV cache

`src/koval/exchanges/ohlcv_cache.py` caches fetched candles as Parquet under
`data_cache/`, guarded with `portalocker` for concurrent access. Delete the
directory to force a refetch. Reproducible run archives must be retained by
the consumer separately; a mutable cache is not an immutable dataset record.

The key is `(exchange, market, symbol, timeframe)` and the layout is
`<root>/<exchange>/<market>/<SYMBOL>_<timeframe>.parquet`, so spot and futures
candles for the same pair never share a file. `OhlcvCache.get` takes an
optional `exchange_type` and refuses an adapter whose `capabilities()` does not
match the requested venue and market. A pre-market file at
`<root>/<exchange>/<SYMBOL>_<timeframe>.parquet` is adopted once, under the
market that adapter used to serve (Binance futures, WhiteBIT spot).

`OhlcvCache.get_evidence` wraps a requested interval in an immutable
`CandleDataset`. It validates complete coverage and OHLCV constraints, records
venue/market/symbol/timeframe identity and actual bounds, encodes every row with
the versioned little-endian `koval_candles_f64le_v1` format, and returns its
SHA-256 hash. Evidence ranges may contain only bars closed at acquisition time;
a range that includes the current forming candle is rejected. The consumer must
archive those exact bytes; fetching the same range from a mutable cache later is
not reproduction evidence.

## Funding and instrument evidence

`ExchangeAdapter.fetch_funding_history` returns a normalized `FundingSeries` or
raises `FundingUnavailableError`. Each record carries the signed rate,
settlement time, settlement mark, interval, and source; incomplete requested
coverage is rejected and raw response pages remain available to the archive
boundary. Funding settles before any order action at the same timestamp.

Coverage is judged against the phase the records themselves reveal, not an
assumed epoch-aligned grid: a venue settling at an offset from a multiple of its
interval is still complete. A window narrower than one interval may legitimately
contain no settlement — pass `interval_ms` to `build_funding_series` to declare
one — while a window spanning a whole interval with no record is missing
evidence and is rejected. The Binance adapter still needs two settlements in the
requested range to measure the interval, so short-window funding evidence has to
be assembled by the archiving caller.

Fee schedules, instrument specifications, and mark-price series are separate
evidence inputs. Historical evidence must cover the simulated timestamp.
Present-day evidence applied outside its effective window is labelled an
approximation. Missing evidence stays unavailable; callers must not manufacture
a zero rate or unconstrained instrument and label it historical. Liquidation is
currently defined only for a single cross-margin position with quote-currency
collateral; unsupported margin modes fail before simulation.

## Sandbox brokers

- `src/koval/exchanges/binance_sandbox.py` — Binance USD-M Futures
  **testnet** execution. Market, limit and stop entries; every entry requires a full
  stop-loss/take-profit bracket, confirmed immediately after a full fill. A
  partial fill triggers containment (close what filled) instead of adopting
  an unprotected position. Reachable base URLs are allowlisted in
  `koval.exchanges.binance_sandbox._ALLOWED_BASE_URLS`. Signed requests carry a
  bounded receive window and a midpoint-measured server clock skew, which
  `sandbox_factory.build_broker` measures once when it builds the session's
  broker. Skew beyond the supported bound fails the session closed; an
  unreachable time endpoint does not, because the venue validates the timestamp
  itself.
- `src/koval/exchanges/whitebit_sandbox.py` — fails closed. WhiteBIT has no
  verified public sandbox, so execution is disabled; data stays read-only.
- `src/koval/exchanges/sandbox_factory.py` — the only place execution modes
  are resolved. `paper` and `binance_sandbox` are the complete list.

`PollingFeed` runs its between-bar broker hook while waiting, backing off, and
while the adapter request itself is blocked. Consecutive market-data failures
have a finite budget and end with `market_data_retry_exhausted`; this keeps the
order watcher alive during a transient outage without allowing an indefinitely
stale strategy session.

## What is not accepted

Adding a real-money execution path, widening the sandbox allowlist, or
adding an execution mode is not a contribution this project accepts, from
anyone, for any reason. The invariant is pinned by
[`tests/safety/test_no_real_money_path.py`](../tests/safety/test_no_real_money_path.py).

Update this file when: an adapter is added, the cache format changes, or the
sandbox ruleset changes.
