## What this changes

<!-- One or two sentences. If it fixes an issue, write "Fixes #123". -->

## Why

<!-- The problem this solves. Skip if the linked issue already explains it. -->

## Checklist

- [ ] Tests added or updated, and they failed before the change
- [ ] `pytest -m "not backtrader" -q` passes
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] Every commit is signed off (`git commit -s`) — CI enforces this
- [ ] No new runtime dependency (or the reason for one is explained above)
- [ ] No file imports `backtrader` or `koval.adapters.backtrader`
- [ ] `CHANGELOG.md` updated under `[Unreleased]`, if this is user-visible

## Notes for the reviewer

<!-- Anything you are unsure about, or a decision worth a second opinion. Optional. -->
