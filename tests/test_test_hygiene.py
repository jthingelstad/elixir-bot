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


def test_silent_infrastructure_catch_is_rejected():
    """The narrow-catch rule has to actually fire.

    It was added because the broad-catch policy could not see the two failures
    that cost us most: `except sqlite3.OperationalError: pass` killed the whole
    post-test invariant sweep for three weeks, and a channel-share resolve
    failure dropped posts. Both were narrow, so both passed hygiene.

    A checker nobody has watched fail is a checker you are trusting on faith.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import ast

    import check_exception_hygiene as hygiene

    def silent(source: str) -> bool:
        handler = next(
            node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ExceptHandler)
        )
        return bool(
            hygiene._caught_names(handler.type) & hygiene._INFRASTRUCTURE_EXCEPTIONS
        ) and hygiene._is_silent(handler)

    # The exact shape that went undetected.
    assert silent("try:\n    x()\nexcept sqlite3.OperationalError:\n    pass\n")
    # Swallowed into a default is just as invisible as `pass`.
    assert silent("try:\n    x()\nexcept requests.RequestException:\n    rows = []\n")
    # Logging it, re-raising it, or recording an incident all clear the rule.
    assert not silent(
        "try:\n    x()\nexcept sqlite3.OperationalError:\n    log.warning('nope')\n    rows = []\n"
    )
    assert not silent("try:\n    x()\nexcept OSError:\n    raise\n")
    # A parse guard is not an infrastructure failure and stays unpoliced --
    # there are ~150 of these and logging them would drown the signal.
    assert not silent("try:\n    int(v)\nexcept (TypeError, ValueError):\n    pass\n")


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
