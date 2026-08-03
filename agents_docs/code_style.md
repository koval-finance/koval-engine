# Code style

Purpose: the conventions this codebase actually follows. Ruff enforces what
a tool can enforce (`ruff check .`, `ruff format --check .`, line length
100); this file covers the rest.

## Python

- Python 3.11+ — use `X | Y` unions, `match`, `Self`; no
  `from typing import Optional`.
- Type hints on every public function. The package ships `py.typed`.
- Pydantic v2 idioms: `model_validate`, `model_dump`, `model_json_schema`.
- Naming: `snake_case` files and functions, `PascalCase` classes,
  `UPPER_SNAKE_CASE` constants, `_leading_underscore` private helpers.

## House rules

- **Pure helpers.** Block functions take arrays and parameters and return
  values. No framework imports, no I/O, no mutation of shared state. Arrays
  chronological, `arr[-1]` = current bar.
- **One source of truth per formula or threshold.** A number that appears
  twice is a bug waiting to disagree with itself; name it once.
- **No error handling for scenarios that cannot happen.** Validate at the
  boundary (graph validation, CLI input, HTTP), trust inside it. Guard
  clauses for impossible states hide real bugs.
- **Comments explain why, not what.** Match the density of the surrounding
  file — most of this codebase reads without comments.
- **Explicit over clever.** Predictable, boring code wins.

Update this file when: a convention changes or a new house rule earns its
place.
