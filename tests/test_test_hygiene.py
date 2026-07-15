import subprocess
import sys
from pathlib import Path


def test_suite_has_no_expected_failure_budget():
    """A broken live contract must fail normally instead of disappearing."""
    marker = "pytest.mark." + "xfail"
    offenders = [
        path
        for path in Path(__file__).parent.glob("test_*.py")
        if marker in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_production_exception_budget_is_reviewed():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/check_exception_hygiene.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
