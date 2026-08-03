# Testing

Purpose: how the suite is organized and what "tested" means here.

## Commands

```bash
./scripts/verify.sh                                  # the full gate
.venv/bin/python -m pytest -m "not backtrader" -q    # tests only
.venv/bin/python -m pytest tests/strategy/graph -q   # one area
```

## Layout

`tests/` mirrors `src/koval/`: `tests/engine/`, `tests/strategy/` (with
`graph/`, `helpers/`, `nodes/`, `presets/`), `tests/exchanges/`,
`tests/cli/`. Repository-level guards live at the top of `tests/`: licence
boundary, public surface, public language, agent docs, release workflow,
release docs, sdist contents.

`tests/safety/` pins the no-real-money invariant and is load-bearing; treat
a failure there as a stop-everything signal.

## The `backtrader` marker

Tests marked `backtrader` exercise a separately distributed GPL plugin. The
default command deselects them, CI never runs them here, and the plugin must
never be installed into this repository's virtualenv — that would cross the
licence boundary this project exists to keep.

## What "tested" means

- Tests are written first and observed failing. The engine computes numbers
  people use to decide whether a strategy is worth money; calculation or
  state-transition logic without a test that was seen failing is not
  accepted.
- Cover boundaries, not happy paths: empty arrays, exact thresholds, the bar
  where two exits collide.
- Golden tests (`tests/strategy/graph/test_*_golden.py`) pin end-to-end
  behaviour of representative graphs. If your change legitimately shifts a
  golden result, explain why; do not quietly re-record.
- HTTP is faked with `responses`; the suite must pass with no network.
- Async tests run under `pytest-asyncio` in auto mode — plain `async def`
  tests work without a decorator.

Update this file when: the suite layout changes, a marker is added, or the
gate command changes.
