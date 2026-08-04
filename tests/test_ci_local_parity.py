"""CI and the local hook must run the SAME gates.

On 2026-08-03 three commits passed the pre-commit hook, pushed cleanly, and
failed in CI about twenty seconds later. The hook carried a comment saying it
matched `.github/workflows/tests.yml`; it matched every step except "Audit
locked dependencies", so a pip-audit CVE disclosure was invisible locally.

A comment claiming parity is not parity. The gates now live in one file that
both callers run, and these tests fail if either caller stops running it or
starts running gates of its own.
"""

from __future__ import annotations

import pathlib
import stat

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/tests.yml"
GATES = ROOT / "scripts/gates.sh"
HOOK = ROOT / ".githooks/pre-commit"

# Every gate the project requires. Named here so ADDING a gate is a deliberate
# three-line change (script + this list) rather than a silent divergence.
REQUIRED_GATES = (
    "uv lock --check",
    "pip-audit",
    "scripts/check_docs.py",
    "scripts/check_exception_hygiene.py",
    "ruff check",
    "ruff format --check",
    "mypy capabilities/",
    "pytest tests/",
    "scripts/simulate.py",
)


def test_the_gate_script_exists_and_is_executable():
    assert GATES.is_file(), "scripts/gates.sh is the single source of truth for gates"
    assert GATES.stat().st_mode & stat.S_IXUSR, "scripts/gates.sh must be executable"


def test_every_required_gate_is_in_the_script():
    body = GATES.read_text()
    missing = [g for g in REQUIRED_GATES if g not in body]
    assert not missing, f"scripts/gates.sh is missing gates: {missing}"


def test_ci_runs_the_shared_gate_script():
    assert WORKFLOW.is_file()
    assert "scripts/gates.sh" in WORKFLOW.read_text(), (
        "CI must run scripts/gates.sh so it cannot drift from the local hook"
    )


def test_the_hook_runs_the_shared_gate_script():
    assert HOOK.is_file(), ".githooks/pre-commit must be tracked, not left in .git/hooks"
    assert "scripts/gates.sh" in HOOK.read_text()
    assert HOOK.stat().st_mode & stat.S_IXUSR, "hook must be executable"


def test_ci_does_not_define_gates_of_its_own():
    """The failure mode being prevented: a gate added to CI but not the hook.

    Setup steps (checkout, setup-uv, uv sync) are fine. A `run:` that invokes a
    checker directly is not — that gate would be invisible to every developer
    until it failed after a push.
    """
    smells = ("ruff ", "mypy ", "pytest ", "pip-audit", "check_docs", "check_exception_hygiene")
    offenders = []
    for line in WORKFLOW.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith(("run:", "- run:")) or "gates.sh" in stripped:
            continue
        if any(s in stripped for s in smells):
            offenders.append(stripped)
    assert not offenders, (
        "these CI steps run gates outside scripts/gates.sh, so the local hook "
        f"cannot see them: {offenders}"
    )
