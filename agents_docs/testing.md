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

Tests marked `backtrader` exercise a separately distributed GPL plugin —
`koval-backtrader` on PyPI. The default command deselects them, CI never runs
them here, and the plugin must never be installed into this repository's
virtualenv: that would put a GPL package in the environment of an MIT project
and cross the boundary this split exists to keep.

To run them anyway, use a throwaway environment that is not this one:

```bash
python3 -m venv /tmp/koval-plugin-check
/tmp/koval-plugin-check/bin/pip install -e ".[dev]" koval-backtrader
/tmp/koval-plugin-check/bin/python -m pytest -m backtrader -q
rm -rf /tmp/koval-plugin-check
```

The engine's own `.venv` is untouched by that, which is the point. Never
shortcut it by copying an adapter directory into `src/` — a grafted tree makes
the licence-boundary guard fail for a reason, and the failure is correct.

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
