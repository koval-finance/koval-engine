# Release process

Purpose: how a version reaches PyPI, and which parts are irreversible.
Agents prepare releases; a human runs every git and tag command.

## Sources of truth

- The version lives **only** in `pyproject.toml`. `koval.__version__` reads
  it from installed distribution metadata (`src/koval/_version.py`); there
  is no VERSION file to bump.
- Release notes live in `CHANGELOG.md` under a `## [X.Y.Z]` heading. The
  release workflow extracts that section and **fails if it is empty**, so
  keep the Unreleased section populated as changes land.

## The pipeline

Pushing a `v*` tag triggers `.github/workflows/release.yml`, four stages in
order:

1. **verify** — ruff, formatting, and the full default suite on Python 3.11,
   3.12, and 3.13.
2. **build** — checks the tag matches the `pyproject.toml` version,
   validates the changelog entry, builds sdist and wheel, runs
   `twine check --strict`.
3. **publish** — PyPI Trusted Publishing (OIDC) through the `pypi`
   environment, with PEP 740 attestations. No long-lived PyPI token is used.
   **This stage is irreversible: a version can be yanked but never reused.**
4. **github-release** — creates the GitHub release with the built artifacts
   and the extracted notes.

Stage ordering is pinned by
[`tests/test_release_workflow.py`](../tests/test_release_workflow.py).

## Checklist before suggesting a tag

1. `./scripts/verify.sh` exits 0.
2. The `pyproject.toml` version and the changelog heading agree.
3. `git status --porcelain` shows nothing unexpected, and `git ls-files`
   shows no private path (the public-surface guard also pins this).
4. Stop. Suggest the tag commands; the owner runs them.

Update this file when: the workflow stages change, the version source moves,
or the changelog convention changes.
