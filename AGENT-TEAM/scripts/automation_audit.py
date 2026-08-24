#!/usr/bin/env python3
"""Validate the objective registry and compare it with live Codex automations."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO / "AGENT-TEAM" / "automations.toml"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
REQUIRED_KEYS = {
    "id",
    "name",
    "objective",
    "objective_file",
    "status",
    "rrule",
    "model",
    "reasoning_effort",
}
VALID_STATUSES = {"ACTIVE", "PAUSED"}


def prompt(entry: dict, repo: Path = REPO) -> str:
    paths = [
        repo / "AGENT-TEAM/WORKFLOW.md",
        repo / "AGENT-TEAM/README.md",
        repo / entry["objective_file"],
    ]
    rendered = ", ".join(f"`{path}`" for path in paths)
    return (
        f"Follow the project instructions already loaded by Codex. Read {rendered} "
        f"completely, then pursue the `{entry['objective']}` objective exactly as written. "
        "Measure current evidence before changing anything. Own a clear gap through source "
        "fix, regression coverage, verification, deployment or restart when required, and "
        "natural acceptance. Use issues only for multi-run work, external blockers, durable "
        "audits, or Jamie decisions. Run preflight first; acquire the objective lease only "
        "before mutation; never publish pre-existing work; preserve human and privacy "
        "boundaries; and end with a clean repository. Keep automation memory to Current "
        "state, Active watches, and one replace-in-place Latest run."
        " Resolve the memory path with `uv run --locked python "
        "AGENT-TEAM/scripts/automation_memory.py <automation-id>` so an unset "
        "CODEX_HOME cannot lose the state."
    )


def validate(plan: dict, repo: Path = REPO) -> list[str]:
    failures: list[str] = []
    if plan.get("version") != 2:
        failures.append("registry version must be 2")
    if plan.get("repo") != ".":
        failures.append("registry repo must be . so it is portable between checkouts")
    entries = plan.get("automation", [])
    if not entries:
        failures.append("registry must contain at least one automation")
        return failures
    ids = [entry.get("id") for entry in entries]
    objectives = [entry.get("objective") for entry in entries]
    if len(ids) != len(set(ids)):
        failures.append("automation ids must be unique")
    if len(objectives) != len(set(objectives)):
        failures.append("objectives must have exactly one owner")
    for entry in entries:
        missing = sorted(REQUIRED_KEYS - entry.keys())
        if missing:
            failures.append(f"{entry.get('id', '(unknown)')}: missing {', '.join(missing)}")
            continue
        if entry["status"] not in VALID_STATUSES:
            failures.append(f"{entry['id']}: status must be ACTIVE or PAUSED")
        if not (repo / entry["objective_file"]).is_file():
            failures.append(f"{entry['id']}: missing {entry['objective_file']}")
    return failures


def expected(entry: dict, repo: Path = REPO) -> dict:
    return {
        "id": entry["id"],
        "kind": "cron",
        "name": entry["name"],
        "prompt": prompt(entry, repo),
        "status": entry["status"],
        "rrule": entry["rrule"],
        "model": entry["model"],
        "reasoning_effort": entry["reasoning_effort"],
        "execution_environment": "local",
        "cwds": [str(repo)],
    }


def audit(
    plan: dict, *, codex_home: Path = CODEX_HOME, repo: Path = REPO
) -> tuple[list[str], list[str]]:
    successes: list[str] = []
    failures = validate(plan, repo)
    if failures:
        return successes, failures
    for entry in plan["automation"]:
        path = codex_home / "automations" / entry["id"] / "automation.toml"
        if not path.exists():
            failures.append(f"{entry['id']}: live automation is not installed")
            continue
        try:
            actual = tomllib.loads(path.read_text())
        except Exception as exc:
            failures.append(f"{entry['id']}: invalid live TOML: {exc}")
            continue
        wanted = expected(entry, repo)
        drift = [key for key, value in wanted.items() if actual.get(key) != value]
        if drift:
            failures.append(f"{entry['id']}: live drift in {', '.join(drift)}")
        else:
            successes.append(
                f"OK  {entry['id']}  {entry['objective']}  {entry['status']}  {entry['rrule']}"
            )
    return successes, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry-only", action="store_true", help="skip live Codex automation comparison"
    )
    args = parser.parse_args()
    plan = tomllib.loads(PLAN_PATH.read_text())
    if args.registry_only:
        successes: list[str] = []
        failures = validate(plan)
        if not failures:
            successes.append(f"OK  registry  {len(plan['automation'])} objective owners")
    else:
        successes, failures = audit(plan)
    for success in successes:
        print(success)
    for failure in failures:
        print(f"FAIL  {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
