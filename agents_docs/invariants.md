# Invariants

Purpose: every load-bearing rule in this repository, with the test that pins
it. When a guard fails, fix the cause. Never adjust the guard to make a build
pass.

| Invariant | Pinned by |
|---|---|
| No code path reaches a real-money trading endpoint. Only `paper` and `binance_sandbox` execution modes exist; WhiteBIT execution fails closed. | [`tests/safety/test_no_real_money_path.py`](../tests/safety/test_no_real_money_path.py) |
| No file imports `backtrader` or any package derived from it. The core stays MIT; backtest engines are separate plugins. | [`tests/test_license_boundary.py`](../tests/test_license_boundary.py) |
| No maintainer-private path is tracked by git. | [`tests/test_public_surface.py`](../tests/test_public_surface.py) |
| No internal planning vocabulary appears in the published tree. | [`tests/test_public_language.py`](../tests/test_public_language.py) |
| The agent entry files, the documentation index, relative links, and the verification gate stay consistent. | [`tests/test_agents_docs.py`](../tests/test_agents_docs.py) |
| The release path runs every quality gate before the irreversible PyPI publish. | [`tests/test_release_workflow.py`](../tests/test_release_workflow.py) |
| Published docs stay accurate about what the engine does and does not model. | [`tests/test_release_docs.py`](../tests/test_release_docs.py) |
| Runtime dependencies are exactly `numpy`, `pandas`, `pyarrow`, `portalocker`, `pydantic`, `requests`. | Unenforced — reviewer's job. Open an issue before proposing a change. |
| Block helpers are pure functions: arrays and parameters in, values out. No framework imports, no I/O, no shared state. | Unenforced — reviewer's job. |
| English only in code, comments, docstrings, tests, and docs. | Unenforced — reviewer's job. |
| Agents never commit, push, tag, rebase, merge, or rewrite history. | Unenforced by tests — the project's git policy; see [agent_workflow.md](agent_workflow.md). |

The unenforced rows are marked honestly rather than dressed up. If you find a
cheap way to pin one with a test, that is a welcome contribution.

Update this file when: a guard test is added, removed, or renamed; a new
invariant is introduced; an unenforced rule gains a test.
