# Contributing to koval-engine

Thanks for considering a contribution. This document covers setup, the rules that CI enforces, and what to expect.

Maintainer response is best-effort. This is a small project; a pull request may sit for a while before anyone looks at it.

## Setup

```bash
git clone https://github.com/koval-finance/koval-engine.git
cd koval-engine
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Requires Python 3.11 or newer.

## Tests and lint

```bash
.venv/bin/python -m pytest -m "not backtrader" -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

CI runs exactly these on Python 3.11, 3.12, and 3.13.

Tests marked `backtrader` need the GPL adapter package, which this repository does not depend on. The documented test command and CI exclude them. **Do not install `backtrader` into this repository's virtualenv** — the engine's environment must stay free of the GPL dependency.

## Tests come first

Write the failing test, watch it fail, then implement. This is not a style preference here: the engine computes numbers that people use to decide whether a strategy is worth money, and calculation or state-transition logic without a test that was observed failing is not accepted.

## Rules CI enforces

Four guard tests protect properties that are easy to break by accident. If one fails, fix the cause — do not adjust the guard.

| Guard | Rule |
|---|---|
| `tests/test_license_boundary.py` | No file may import `backtrader`, `koval.adapters.backtrader`, or `koval_backtrader`. The core is MIT; the Backtrader engine is GPL-3.0 and ships as a separate package. An import here would relicense the core. |
| `tests/safety/` | No code path may reach a real-money trading endpoint. Only `paper` and `binance_sandbox` are allowlisted modes. |
| `tests/test_public_surface.py` | Certain paths must never be tracked by git. |
| `tests/test_public_language.py` | No references to private planning documents in the published tree. |

Two more expectations, not currently automated:

- **No new runtime dependencies** without discussing it in an issue first. Runtime deps are `numpy`, `pandas`, `pyarrow`, `portalocker`, `pydantic`, and `requests`, and the list staying short is deliberate.
- **Block helpers stay pure.** Functions in `src/koval/strategy/helpers/` take arrays and parameters and return values — no framework imports, no I/O, no mutation of shared state. Arrays are chronological, with `[-1]` as the current bar.

## Working with an AI agent

This repository ships agent-facing documentation. If you contribute with a
coding agent, point it at [AGENTS.md](AGENTS.md) — most tools read it
automatically — and see [agents_docs/](agents_docs/README.md) for the deeper
references. The definition of done is `./scripts/verify.sh` exiting 0.
Review everything your agent produces before opening a pull request: the DCO
sign-off is yours, not the agent's.

## Developer Certificate of Origin

Contributions are accepted under the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Sign off each commit:

```bash
git commit -s -m "your message"
```

This appends a `Signed-off-by:` line, which certifies that you wrote the patch or otherwise have the right to submit it under the project's licence. CI rejects pull requests containing commits without it. To fix an existing branch:

```bash
git commit --amend -s        # last commit
git rebase --signoff main    # every commit on the branch
```

## Pull requests

1. Open an issue first for anything larger than a bugfix, so effort is not wasted on an approach that will not be merged.
2. Branch from `main`.
3. Write the test, then the code.
4. Run the tests and both ruff commands.
5. Open the PR and fill in the checklist.

Keep pull requests focused — one concern each. A PR that fixes a bug and also reformats an unrelated module is hard to review and likely to be sent back.

## Adding a block

A block is a pure function in `src/koval/strategy/helpers/`, a Pydantic parameter model in `src/koval/strategy/schemas.py`, a `BlockSpec` registered in `src/koval/strategy/registry.py`, and a typed node in `src/koval/strategy/nodes/`. Tests for the pure function come first; they should cover the boundaries of the calculation, not just a happy path.

`koval blocks` lists the catalogue and is generated from the code, so a correctly registered block appears there with no extra step.
