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

Before finalizing a candidate, check live PyPI release metadata, remote tags
(`git ls-remote --tags origin`) and the release workflow runs. A source commit
named "Release", a local tag, or a local artifact is not publication evidence.
Retain an existing candidate version only while it is unpublished and its
release tag is unused. Never reuse a PyPI version, including a yanked one. Resolve
an identity conflict before changing the version or a plugin dependency range.
Consolidate unpublished candidate notes into the selected release section, with
a comparison link from the previous public tag, so the extracted notes describe
every change shipped since that release.

## The pipeline

Pushing a `v*` tag triggers `.github/workflows/release.yml`, four stages in
order:

1. **verify** — ruff, formatting, and the full default suite on Python 3.11,
   3.12, and 3.13.
2. **build** — checks the tag matches the `pyproject.toml` version,
   validates the changelog entry, builds sdist and wheel, verifies both
   artifacts carry this tree's `src/koval` sources
   (`python scripts/check_dist.py --require-artifacts`), runs `twine check --strict`.
3. **publish** — PyPI Trusted Publishing (OIDC) through the `pypi`
   environment, with PEP 740 attestations. No long-lived PyPI token is used.
   **This stage is irreversible: a version can be yanked but never reused.**
4. **github-release** — creates the GitHub release with the built artifacts
   and the extracted notes.

Stage ordering is pinned by
[`tests/test_release_workflow.py`](../tests/test_release_workflow.py).

## Local artifacts

`dist/` is gitignored, so a build left there is invisible to `git status` while
staying installable by path. A sibling checkout that installs
`file://…/dist/koval_engine-<version>-py3-none-any.whl` gets whatever that file
holds, under a version string that names something else — which is how a
pre-release engine was once measured as if it were the published release.

When an artifact is retained acceptance evidence, copy it byte-for-byte into
ignored `.private/` scratch and record its SHA-256 before replacing it. Keep the
historical copy out of `dist/` and do not use it as published-package evidence.

`scripts/check_dist.py` runs in the default suite against `dist/` and fails when
an artifact there does not carry the current sources. Delete a stale build
rather than keeping it; the released artifact is always available from PyPI.

## Protocol compatibility and local release validation

Read [runtime_contract.md](runtime_contract.md) before updating a plugin. A plugin
must admit the intended engine release in its dependency range, offer every
requested protocol capability, and pass the shared fixtures using the installed
wheel. Do not change a sibling virtualenv that another session is using; use an
isolated copy/environment for diagnostics.

Build and validate from the final working tree:

```bash
.venv/bin/python -m build --no-isolation
.venv/bin/python scripts/check_dist.py --require-artifacts
.venv/bin/python -m twine check --strict dist/*
```

`--require-artifacts` fails for an empty/missing directory. Without that flag,
local checks may legitimately report nothing to verify. Rebuild artifacts after
any subsequent source/fixture change, and never reuse a pre-release wheel as a
published artifact just because its filename has the release version.

For release preparation, build into a fresh ignored scratch directory and pass
that directory to `scripts/check_dist.py --require-artifacts`. Validate metadata
versions as well as package bytes in both artifacts. Install the wheel normally
in a fresh engine-only virtualenv outside the checkout, run `pip check`, confirm
the imported package path and all installed package bytes, and exercise the
public API without `PYTHONPATH` pointing at the source. Repeat with the sdist;
never install Backtrader into an engine validation environment. Run the available
Python 3.11/3.12/3.13 gates and report missing runtimes. The tag workflow still
owns the full remote matrix and rebuilds its own published artifacts; local
hashes are preparation evidence, not predicted public wheel/sdist hashes.

Binance conditional API acceptance uses the current `algo` default. The offline
suite validates request/response behavior, but does not replace an authenticated
supervised testnet smoke run. No release claim should imply production fills
were validated by mocks. Record outstanding downstream acceptance explicitly.

## Checklist before suggesting a tag

1. `./scripts/verify.sh` exits 0.
2. The `pyproject.toml` version and the changelog heading agree.
3. `git status --porcelain` shows nothing unexpected, and `git ls-files`
   shows no private path (the public-surface guard also pins this).
4. Stop. Suggest the tag commands; the owner runs them.

Update this file when: the workflow stages change, the version source moves,
or the changelog convention changes.
