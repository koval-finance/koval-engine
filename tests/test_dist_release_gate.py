"""A release gate must distinguish success from having nothing to verify."""

import subprocess
import sys
from pathlib import Path


def test_required_artifacts_cannot_pass_an_empty_release_directory(tmp_path):
    gate = Path(__file__).resolve().parents[1] / "scripts" / "check_dist.py"
    result = subprocess.run(
        [sys.executable, str(gate), str(tmp_path), "--require-artifacts"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no distributions" in result.stdout
