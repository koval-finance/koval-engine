# AGENTS.md — koval-engine

Context for AI coding agents and their humans. Start here; depth lives in
[agents_docs/README.md](agents_docs/README.md).

## What this is

koval-engine is the MIT-licensed core of a trading-strategy platform: a
typed dataflow strategy engine, a block library, exchange data adapters, and
sandbox brokers. It is published on PyPI as `koval-engine`; the import
package is `koval`. There is no real-money trading path, by design and by
test.

## Non-negotiables

Tests pin most of these; [agents_docs/invariants.md](agents_docs/invariants.md)
says exactly which, and marks the rest as the reviewer's job instead. All six
carry the same weight regardless of which enforces them.

- **No real-money code path.** Only `paper` and `binance_sandbox` execution
  modes exist. Never add, enable, or assume another.
- **MIT only.** No file imports `backtrader` or any package derived from it.
  Backtest engines are separate plugins found through the
  `koval.backtest_engines` entry-point group.
- **No new runtime dependencies.** The list is exactly: `numpy`, `pandas`,
  `pyarrow`, `portalocker`, `pydantic`, `requests`. Open an issue before
  proposing a seventh.
- **English only** in code, comments, docstrings, tests, and docs.
- **Agents never commit.** No commits, pushes, tags, rebases, merges, or
  history rewrites. Prepare changes, run verification, suggest a commit
  message, stop. All git actions belong to a human.
- **When a guard test fails, fix the cause — never the guard.**

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Requires Python 3.11+.

## Definition of done

```bash
./scripts/verify.sh
```

Exit code 0 means done: lint, formatting, and the full default test suite.
Nothing else counts, and no prose argument substitutes for it.

Do not install `backtrader` into this repository's virtualenv — tests marked
`backtrader` belong to a separately distributed GPL plugin and are excluded
by the default command.

## Repository map

```
src/koval/
├── engine/      backtest protocol, live engine, paper broker, metrics, account state
├── strategy/
│   ├── base/        DeclarativeStrategy ABC, TradeSetup, EntryConfig
│   ├── helpers/     pure block functions — no framework, no state, no I/O
│   ├── graph/       typed dataflow engine: entities, domains, ports, executor
│   ├── nodes/       typed nodes wrapping the pure helpers
│   ├── presets/     preset graph builders
│   ├── registry.py  BLOCK_CATALOG and STRATEGY_REGISTRY
│   └── schemas.py   Pydantic parameter models per block
├── exchanges/   data adapters, OHLCV cache, sandbox brokers
└── cli/         the `koval` console script
```

Tests mirror this layout under `tests/`.

## How to work here

1. Restate the task and name what is out of scope before editing anything.
2. Write the failing test first and watch it fail. Calculation or
   state-transition logic without a test that was observed failing is not
   accepted.
3. Implement the minimum that makes it pass. No drive-by refactoring.
4. Run `./scripts/verify.sh`.
5. Review your own diff, report, and stop before any git action.

Full workflow: [agents_docs/agent_workflow.md](agents_docs/agent_workflow.md).

## Where to read next

| Task | Read first |
|---|---|
| Add or change a block | [agents_docs/adding_a_block.md](agents_docs/adding_a_block.md) |
| Graph engine or validation | [agents_docs/graph_contracts.md](agents_docs/graph_contracts.md) |
| Exchanges, cache, or brokers | [agents_docs/exchanges_and_data.md](agents_docs/exchanges_and_data.md) |
| Safety or licensing | [agents_docs/invariants.md](agents_docs/invariants.md) |
| Everything else | [agents_docs/README.md](agents_docs/README.md) |
