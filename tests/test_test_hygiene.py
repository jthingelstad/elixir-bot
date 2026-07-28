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


def test_no_orphaned_bytecode_keeps_deleted_modules_importable():
    """A deleted module must actually be gone, not just gone from git.

    `git rm` removes sources but not the untracked `__pycache__` beside them, so
    stale .pyc files can keep a retired package importable in a working tree
    while `git status` looks clean. Found during the trim-and-fit sprint (#214):
    `import engine.recognition` still SUCCEEDED after the package was deleted in
    #207, from five orphaned .pyc files — which quietly violated the sprint's
    own rule that retired code is not kept executable.
    """
    root = Path(__file__).resolve().parent.parent
    orphans = []
    for cache in root.rglob("__pycache__"):
        if ".venv" in cache.parts or ".git" in cache.parts:
            continue
        for pyc in cache.glob("*.pyc"):
            module = pyc.name.split(".")[0]
            if not (cache.parent / f"{module}.py").exists():
                orphans.append(str(pyc.relative_to(root)))
    assert not orphans, (
        "orphaned bytecode for deleted modules (they may still be importable): "
        f"{sorted(orphans)[:10]}"
    )
