"""Every scheduled job must record that it started and how it ended.

`runtime_job_status` is the authoritative "did the scheduler actually run it"
answer — the triage skill reads it, and `scripts/confidence_report.py` decides
Elixir is healthy partly from it. A job that skips either half makes that answer
wrong in a specific, quiet way:

- no `mark_job_start`: `running` is never set, so `clear_stale_running_jobs`
  cannot notice the job died mid-run, and `run_count` never moves.
- an exception path with no `mark_job_failure`: the row keeps showing the last
  success forever. The job can fail every week and the status stays green.

Found on 2026-08-09: `member_outreach_propose` did both — `run_count: 0`,
`last_started_at: null`, and a handler that logged a traceback and returned.
"""

from __future__ import annotations

import ast
import pathlib

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "runtime"
MARKS = ("mark_job_start", "mark_job_success", "mark_job_failure")


def _calls(node, name: str) -> bool:
    return any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", None) == name for n in ast.walk(node)
    )


def _job_names(node, mark: str) -> set[str]:
    found = set()
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == mark
            and n.args
            and isinstance(n.args[0], ast.Constant)
        ):
            found.add(n.args[0].value)
    return found


def _modules():
    for path in sorted(RUNTIME.rglob("*.py")):
        try:
            yield path, ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere
            continue


def test_every_job_that_reports_an_outcome_also_reports_a_start():
    """Without a start there is no `running` flag, so a job that dies mid-run is
    indistinguishable from one that never ran."""
    starts, outcomes = set(), set()
    for _path, tree in _modules():
        starts |= _job_names(tree, "mark_job_start")
        outcomes |= _job_names(tree, "mark_job_success") | _job_names(tree, "mark_job_failure")

    missing = sorted(outcomes - starts)
    assert not missing, f"these jobs record an outcome but never a start: {missing}"


def test_no_job_swallows_an_exception_without_recording_a_failure():
    """A handler that logs and returns leaves the status showing the last
    success. Inner helpers that return a sentinel for the caller to turn into a
    failure are fine — this looks only at handlers inside the job function that
    itself reports the outcome."""
    offenders = []
    for path, tree in _modules():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Only the job function itself — the one that marks the outcome.
            if not (_calls(fn, "mark_job_success") or _calls(fn, "mark_job_failure")):
                continue
            nested = {
                n
                for child in ast.iter_child_nodes(fn)
                for n in ast.walk(child)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try) or node in nested:
                    continue
                if any(node in ast.walk(inner) for inner in nested):
                    continue
                for handler in node.handlers:
                    # Recording EITHER outcome is a deliberate statement. A
                    # handler that marks success is saying "this exception means
                    # a legitimate skip" — clan_wars_intel does exactly that for
                    # the ValueError that means "not enough data yet". What is
                    # never acceptable is swallowing and recording nothing.
                    if _calls(handler, "mark_job_failure") or _calls(handler, "mark_job_success"):
                        continue
                    if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
                        continue
                    if any(isinstance(n, (ast.Return, ast.Pass)) for n in ast.walk(handler)):
                        offenders.append(f"{path.name}:{handler.lineno} in {fn.name}")

    assert not offenders, (
        "these handlers swallow an exception without recording a job failure, so "
        f"the status keeps showing the last success: {offenders}"
    )
