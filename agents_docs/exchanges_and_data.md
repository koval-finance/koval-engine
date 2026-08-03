# Exchanges and data

Purpose: how market data enters the system, and why execution is
deliberately limited. Read [invariants.md](invariants.md) before touching
anything here — this package's safety posture lives in this layer.

## Data adapters (read-only)

- `src/koval/exchanges/base.py` — the adapter ABC.
- `src/koval/exchanges/binance.py`, `src/koval/exchanges/whitebit.py` —
  read-only REST adapters for OHLCV and venue metadata.
- HTTP in tests is faked with the `responses` library; no test may hit a
  real network endpoint.

## OHLCV cache

`src/koval/exchanges/ohlcv_cache.py` caches fetched candles as Parquet under
`data_cache/`, guarded with `portalocker` for concurrent access. Delete the
directory to force a refetch; nothing else depends on it.

## Sandbox brokers

- `src/koval/exchanges/binance_sandbox.py` — Binance USD-M Futures
  **testnet** execution. Market entries only; every entry requires a full
  stop-loss/take-profit bracket, confirmed immediately after a full fill. A
  partial fill triggers containment (close what filled) instead of adopting
  an unprotected position. Reachable base URLs are allowlisted in
  `koval.exchanges.binance_sandbox._ALLOWED_BASE_URLS`.
- `src/koval/exchanges/whitebit_sandbox.py` — fails closed. WhiteBIT has no
  verified public sandbox, so execution is disabled; data stays read-only.
- `src/koval/exchanges/sandbox_factory.py` — the only place execution modes
  are resolved. `paper` and `binance_sandbox` are the complete list.

## What is not accepted

Adding a real-money execution path, widening the sandbox allowlist, or
adding an execution mode is not a contribution this project accepts, from
anyone, for any reason. The invariant is pinned by
[`tests/safety/test_no_real_money_path.py`](../tests/safety/test_no_real_money_path.py).

Update this file when: an adapter is added, the cache format changes, or the
sandbox ruleset changes.
