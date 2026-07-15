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
