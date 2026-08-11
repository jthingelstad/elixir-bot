#!/usr/bin/env python3
"""Check the live Codex automations against the objective-owner plan."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO / "AGENT-TEAM" / "automations.toml"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
OBJECTIVES = {"run", "game", "agent"}


def _prompt(entry: dict) -> str:
    paths = [
        "AGENTS.md",
        "AGENT-TEAM/WORKFLOW.md",
        "AGENT-TEAM/README.md",
        entry["role_file"],
    ]
    rendered = ", ".join(f"`{REPO / path}`" for path in paths)
    return (
        f"Read {rendered} completely, then pursue the `{entry['objective']}` objective "
        "exactly as written. Measure live evidence before changing anything. Own a clear "
        "finding through source fix, regression coverage, gates, commit, push, and natural "
        "acceptance instead of creating role handoff tickets. Use GitHub only for multi-run "
        "work, external blockers, or Jamie decisions. Acquire the local objective lease before "
        "repository mutation, preserve the member-visible and irreversible human boundary, "
        "never force member traffic for validation, and end with a clean repository. If main "
        "is unexpectedly ahead of origin/main, remain read-only and never publish a commit "
        "this run did not create. Keep automation memory compact with exactly Current state, "
        "Active watches, and one replace-in-place Latest run section."
    )


def _expected(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "kind": "cron",
        "name": entry["name"],
        "prompt": _prompt(entry),
        "status": entry["status"],
        "rrule": entry["rrule"],
        "model": entry["model"],
        "reasoning_effort": entry["reasoning_effort"],
        "execution_environment": "local",
        "cwds": [str(REPO)],
    }


def audit(plan: dict, *, codex_home: Path = CODEX_HOME) -> tuple[list[str], list[str]]:
    successes: list[str] = []
    failures: list[str] = []
    entries = plan.get("automation", [])
    objectives = [entry.get("objective") for entry in entries]
    if len(entries) != 3 or set(objectives) != OBJECTIVES or len(set(objectives)) != 3:
        failures.append("plan must contain exactly one run, game, and agent objective")

    for entry in entries:
        if entry.get("status") != "ACTIVE":
            failures.append(f"{entry.get('id', '(unknown)')}: every objective must be ACTIVE")
            continue
        if entry.get("schedule_kind") not in {"time_window", "recovery"}:
            failures.append(
                f"{entry['id']}: objective schedule_kind must be time_window or recovery"
            )
            continue
        role_path = REPO / entry["role_file"]
        if not role_path.exists():
            failures.append(f"{entry['id']}: missing objective file {entry['role_file']}")
            continue
        path = codex_home / "automations" / entry["id"] / "automation.toml"
        if not path.exists():
            failures.append(f"{entry['id']}: missing {path}")
            continue
        try:
            actual = tomllib.loads(path.read_text())
        except Exception as exc:
            failures.append(f"{entry['id']}: invalid TOML: {exc}")
            continue
        expected = _expected(entry)
        drift = [key for key, value in expected.items() if actual.get(key) != value]
        if drift:
            failures.append(f"{entry['id']}: drift in {', '.join(drift)}")
        else:
            successes.append(
                f"OK  {entry['id']}  {entry['objective']}  {entry['rrule']}  {entry['model']}"
            )
    return successes, failures


def main() -> int:
    plan = tomllib.loads(PLAN_PATH.read_text())
    successes, failures = audit(plan)
    for success in successes:
        print(success)
    for failure in failures:
        print(f"FAIL  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
