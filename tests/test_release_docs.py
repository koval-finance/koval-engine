"""Accuracy guards for documentation published with the distribution."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def _project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def test_readme_does_not_make_unscoped_execution_model_claims():
    readme = README.read_text(encoding="utf-8")
    misleading = (
        "Execution modelling is fees-only",
        "no partial fills",
        "`exchange_type` is fixed to `future`",
        "There is no slippage model",
    )

    assert [claim for claim in misleading if claim in readme] == []
    assert "**Paper simulation.**" in readme
    assert "**Backtest plugins.**" in readme
    assert "**Sandbox execution.**" in readme
    assert "market, limit and stop entries" in readme
    assert "partial fill triggers containment" in readme


def test_readme_links_are_absolute_for_the_pypi_description():
    readme = README.read_text(encoding="utf-8")
    link_targets = re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", readme)
    relative = [target for target in link_targets if not target.startswith(("https://", "http://"))]

    assert relative == []


def test_readme_does_not_advertise_unpublished_planning_documents():
    readme = README.read_text(encoding="utf-8")

    assert "execution_contract_plan.md" not in readme
    assert "These capabilities are not implemented by this documentation update." not in readme


def test_contributing_says_the_documented_command_excludes_plugin_tests():
    contributing = CONTRIBUTING.read_text(encoding="utf-8")

    assert "deselected by default" not in contributing
    assert "The documented test command and CI exclude them" in contributing


def test_changelog_scopes_long_lived_credential_claim_to_pypi():
    changelog = CHANGELOG.read_text(encoding="utf-8")

    assert "No long-lived credentials exist in CI" not in changelog
    assert "No long-lived PyPI credential is used" in changelog


def test_changelog_has_one_unreleased_section():
    changelog = CHANGELOG.read_text(encoding="utf-8")

    assert changelog.count("## [Unreleased]") == 1


def test_changelog_has_a_dated_entry_and_links_for_the_project_version():
    changelog = CHANGELOG.read_text(encoding="utf-8")
    version = _project_version()

    assert re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE
    )
    assert (
        f"[Unreleased]: https://github.com/koval-finance/koval-engine/compare/v{version}...HEAD"
        in changelog
    )
    assert f"[{version}]: https://github.com/koval-finance/koval-engine/" in changelog


def test_readme_names_the_current_minor_series():
    readme = README.read_text(encoding="utf-8")
    version = _project_version()
    minor_series = version.rsplit(".", 1)[0]

    assert f"`{minor_series}.x` series" in readme


def test_public_docstrings_do_not_claim_legacy_validation_is_unchanged():
    paths = (
        ROOT / "src" / "koval" / "strategy" / "block_assembler.py",
        ROOT / "tests" / "strategy" / "test_block_assembler.py",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "behaviour is unchanged" not in text
    assert "behaviour is preserved" not in text
    assert "validation is stricter" in text


def test_security_policy_names_the_actual_module_level_origin_allowlist():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "BinanceSandboxBroker._ALLOWED_BASE_URLS" not in security
    assert "koval.exchanges.binance_sandbox._ALLOWED_BASE_URLS" in security
