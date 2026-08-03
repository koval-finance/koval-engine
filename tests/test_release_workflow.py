"""Release workflow guards for the irreversible PyPI publication path."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
MIRROR_WORKFLOW = ROOT / ".github" / "workflows" / "mirror.yml"
SCORECARD_WORKFLOW = ROOT / ".github" / "workflows" / "scorecard.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_build_runs_all_quality_gates_before_artifact_upload():
    workflow = _workflow_text()
    upload = workflow.index("actions/upload-artifact@")

    required_before_upload = (
        "ruff check .",
        "ruff format --check .",
        'pytest -m "not backtrader" -q',
        "python -m build",
        "twine check --strict dist/*",
    )
    missing = [command for command in required_before_upload if command not in workflow[:upload]]
    assert missing == [], f"release artifact is uploaded before these gates run: {missing}"


def test_changelog_is_validated_before_pypi_publish():
    workflow = _workflow_text()
    changelog_validation = workflow.index("No changelog entry found")
    publish = workflow.index("pypa/gh-action-pypi-publish@")
    assert changelog_validation < publish


def test_pypi_publish_precedes_the_public_github_release():
    workflow = _workflow_text()
    assert "build:\n    needs: verify" in workflow
    assert "publish:\n    needs: build" in workflow
    assert "github-release:\n    needs: publish" in workflow


def test_scorecard_job_can_read_repository_contents():
    workflow = SCORECARD_WORKFLOW.read_text(encoding="utf-8")
    assert (
        "    permissions:\n"
        "      contents: read\n"
        "      security-events: write\n"
        "      id-token: write"
    ) in workflow


def test_tag_triggered_mirror_pushes_the_fetched_main_ref():
    workflow = MIRROR_WORKFLOW.read_text(encoding="utf-8")
    assert "refs/remotes/origin/main:refs/heads/main" in workflow
    assert "git push gitlab main --tags" not in workflow


def test_release_comment_scopes_token_claim_to_pypi():
    workflow = _workflow_text()
    assert "no long-lived PyPI token is used" in workflow
    assert "no long-lived token exists in this repository" not in workflow
