# Troubleshooting

Purpose: dated failure memory — symptom, cause, fix. Add an entry whenever a
failure costs someone real time; delete entries that stop being true.

## 2026-08-03 — "deselected" tests in every run

**Symptom:** pytest reports deselected tests no matter what you do.
**Cause:** tests marked `backtrader` need `koval-backtrader`, a separately
distributed GPL plugin.
**Fix:** nothing — the default command excludes them on purpose. Never
install that plugin into this repository's virtualenv; to run those tests,
use the throwaway environment described in [testing.md](testing.md).

## 2026-08-03 — a guard test fails and the fix is not obvious

**Symptom:** one of the repository-level guards is red.
**Cause and fix, by guard:**

- `test_license_boundary.py` — something imports the GPL plugin. Remove the
  import; the dependency direction only points the other way.
- `test_public_surface.py` — a maintainer-private path got staged. Unstage
  it (`git restore --staged <path>`); nothing under an ignored directory may
  be tracked.
- `test_public_language.py` — a published file contains internal planning
  vocabulary. Reword the file; never widen the guard to pass.
- `test_agents_docs.py` — a pointer file, the docs index, a relative link, or
  the verify script drifted. The assertion message names the exact file.
- `tests/safety/` — stop. Nothing lands until the no-real-money invariant is
  green again; see [exchanges_and_data.md](exchanges_and_data.md).

## 2026-08-03 — `.gitignore` edits break the sdist test

**Symptom:** `test_sdist_contents.py` fails, or the built sdist is
mysteriously empty, after an innocent-looking `.gitignore` change.
**Cause:** that test builds its copy-exclusion set from the **basename** of
every `.gitignore` line. A glob line like `dir/*` becomes `*` and excludes
everything.
**Fix:** the hazard is any path-glob line whose basename resolves to
something broad (`dir/*` becomes `*` and excludes everything). Plain names
and the existing suffix globs (`*.py[cod]`, `*.egg-info/`) are fine because
their basename is the whole pattern, not a bare wildcard; no negations.

## 2026-08-03 — `koval backtest` says no engine is installed

**Symptom:** the CLI refuses to backtest.
**Cause:** this package ships the protocol, not an engine; discovery uses
the `koval.backtest_engines` entry-point group.
**Fix:** install a compatible plugin, then rerun. `koval validate` and
`koval blocks` work without one.

## 2026-08-03 — stale candles in a backtest

**Symptom:** fetched data does not reflect the venue.
**Cause:** the Parquet cache under `data_cache/` serves what it has.
**Fix:** delete `data_cache/` and rerun; see
[exchanges_and_data.md](exchanges_and_data.md).

Update this file when: a failure costs anyone more than ten minutes, or a
listed entry goes stale.
