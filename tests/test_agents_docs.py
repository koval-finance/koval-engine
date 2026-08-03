"""Consistency guards for the public agent documentation.

The root entry file, its per-tool pointers, the agents_docs/ index, and the
verification gate must stay in step with each other and with CI. Prose
promises drift; these tests do not.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "scripts" / "verify.sh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_GATES = (
    "ruff check .",
    "ruff format --check .",
    'pytest -m "not backtrader" -q',
)


def test_verify_script_is_executable():
    assert VERIFY.is_file(), "scripts/verify.sh must exist"
    assert os.access(VERIFY, os.X_OK), "scripts/verify.sh must be executable"


def test_verify_script_runs_every_release_gate():
    script = VERIFY.read_text(encoding="utf-8")
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    missing_in_script = [gate for gate in RELEASE_GATES if gate not in script]
    missing_in_workflow = [gate for gate in RELEASE_GATES if gate not in workflow]
    assert missing_in_script == [], f"verify.sh must run: {missing_in_script}"
    assert missing_in_workflow == [], f"release workflow must run: {missing_in_workflow}"


_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _documents() -> list[Path]:
    return [
        ROOT / "AGENTS.md",
        ROOT / "CLAUDE.md",
        ROOT / "GEMINI.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / ".github" / "copilot-instructions.md",
        *sorted((ROOT / "agents_docs").glob("*.md")),
    ]


def test_every_relative_documentation_link_resolves():
    broken = []
    for document in _documents():
        for target in _LINK.findall(document.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (document.parent / target).resolve().is_file():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert broken == [], "broken links:\n" + "\n".join(broken)


def test_every_agents_docs_file_is_indexed():
    index_path = ROOT / "agents_docs" / "README.md"
    assert index_path.is_file(), "agents_docs/README.md must exist"
    index = index_path.read_text(encoding="utf-8")
    files = sorted(
        path.name for path in (ROOT / "agents_docs").glob("*.md") if path.name != "README.md"
    )
    missing = [name for name in files if name not in index]
    assert missing == [], f"agents_docs/README.md must index: {missing}"


POINTERS = (
    Path("CLAUDE.md"),
    Path("GEMINI.md"),
    Path(".cursor/rules/koval-engine.mdc"),
    Path(".github/copilot-instructions.md"),
)


def test_every_pointer_file_references_the_root_entry_file():
    broken = []
    for pointer in POINTERS:
        path = ROOT / pointer
        if not path.is_file() or "AGENTS.md" not in path.read_text(encoding="utf-8"):
            broken.append(str(pointer))
    assert broken == [], f"pointer files must exist and reference AGENTS.md: {broken}"


def test_root_agents_file_stays_within_rule_budget():
    # 12,000 characters is the smallest per-file rule budget among the
    # mainstream agent tools; beyond it, tools truncate or refuse the file.
    size = len((ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    assert size <= 12_000, f"AGENTS.md is {size} characters; the budget is 12000"
