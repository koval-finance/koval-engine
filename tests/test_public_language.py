"""The public tree must not carry internal roadmap or process language.

Phase numbers, block numbers, and references to private planning documents are
meaningless to an outside reader and advertise that the code was carved out of
a private roadmap. This guard keeps them out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# These two guards must spell out the very patterns they forbid.
SELF_REFERENTIAL = {"test_public_language.py", "test_public_surface.py"}
# Untracked maintainer-private directories are excluded: this guard protects
# the published tree, and the public-surface guard already forbids tracking
# these paths at all.
EXCLUDED_PARTS = {
    ".agents",
    ".claude",
    ".git",
    ".private",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "data_cache",
    "dist",
    "superpowers",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".in",
    ".ini",
    ".json",
    ".md",
    ".mdc",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {".gitignore", "CODEOWNERS", "LICENSE"}
FORBIDDEN = re.compile(
    r"Phase[ -]?\d+"
    r"|Task[ -]\d+"
    r"|BLOCK \d+"
    r"|existing API error mapping"
    r"|error mapping \(422\)"
    r"|koval-ai"
    r"|agent_docs"
    r"|docs/superpowers"
    r"|koval[\\/]adapters[\\/]backtrader"
    r"|Trello"
    r"|technical_debt\.md"
    r"|product_roadmap\.md"
    r"|troubleshooting_known_issues\.md"
    r"|Node Manifesto",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "text",
    [
        "Phase4 implementation",
        "existing API error mapping",
        "error mapping (422)",
    ],
)
def test_internal_language_detector_covers_compact_phases_and_hosted_api_phrases(text):
    assert FORBIDDEN.search(text)


def _scanned_files():
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name in SELF_REFERENTIAL
            or any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts)
        ):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
            yield path


def _numbered_lines_to_scan(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if path.name == ".gitignore" and not line.lstrip().startswith("#"):
            # Ignore rules must name paths that another guard forbids. Comments
            # are public prose and remain subject to this language guard.
            continue
        yield number, line


def test_no_internal_process_language_in_published_tree():
    offenders = []
    for path in _scanned_files():
        for number, line in _numbered_lines_to_scan(path):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == [], "internal references leaked into the public tree:\n" + "\n".join(
        offenders
    )


def test_public_language_scanner_covers_repository_text_and_configuration():
    scanned = {path.relative_to(ROOT) for path in _scanned_files()}
    assert {
        Path(".gitignore"),
        Path(".github/workflows/release.yml"),
        Path("CHANGELOG.md"),
        Path("MANIFEST.in"),
        Path("README.md"),
        Path("pyproject.toml"),
    } <= scanned
