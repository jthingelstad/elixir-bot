#!/usr/bin/env python3
"""Check or apply the versioned Codex automation plan for AGENT-TEAM."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO / "AGENT-TEAM" / "automations.toml"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _prompt(entry: dict) -> str:
    if entry["schedule_kind"] == "dispatcher":
        paths = [
            "AGENTS.md",
            "AGENT-TEAM/WORKFLOW.md",
            "AGENT-TEAM/README.md",
            entry["role_file"],
            "AGENT-TEAM/dispatch.toml",
        ]
        rendered = ", ".join(f"`{REPO / path}`" for path in paths)
        return (
            f"Read {rendered} completely, then execute exactly one dispatcher heartbeat as "
            "written. Do not do role work yourself. Preserve preflight, global `wip` "
            "serialization, deterministic priority, the one-child limit, approval gates, "
            "claim cleanup, and normal app-visible task creation."
        )
    paths = [
        "AGENTS.md",
        "AGENT-TEAM/WORKFLOW.md",
        "AGENT-TEAM/README.md",
        entry["role_file"],
    ]
    rendered = ", ".join(f"`{REPO / path}`" for path in paths)
    return (
        f"Read {rendered} completely, then execute the role exactly as written. "
        f"Your automation identity is `{entry['id']}` / `{entry['name']}`; use the attribution "
        "contract and helper in AGENT-TEAM/README.md for every issue comment and commit. "
        f"This is the `{entry['schedule_kind']}` calendar activity; issue handoffs use "
        f"`{entry['dispatch_label']}` and run on demand. Run as a normal app-visible project "
        "task and follow the title protocol in AGENT-TEAM/README.md. Start with the shared "
        "preflight, stop if any other `wip` owns the shared checkout, respect issue claims and "
        "lane boundaries, do one focused thing, write the required AGENT-TEAM run note, and end "
        "with a clean repository. "
        "If main is ahead of origin/main, remain read-only: never commit, push, deploy, or restart "
        "into pre-existing commits. Never push a commit this run did not create."
    )


def _expected(entry: dict) -> dict:
    if entry["schedule_kind"] == "dispatcher":
        return {
            "id": entry["id"],
            "kind": "heartbeat",
            "name": entry["name"],
            "prompt": _prompt(entry),
            "status": entry["status"],
            "rrule": entry["rrule"],
        }
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
    for entry in plan["automation"]:
        status = entry.get("status")
        schedule_kind = entry.get("schedule_kind")
        if status == "ACTIVE" and schedule_kind not in {
            "time_window",
            "recovery",
            "dispatcher",
        }:
            failures.append(
                f"{entry['id']}: ACTIVE requires time_window, recovery, or dispatcher schedule_kind"
            )
            continue
        if status == "PAUSED" and schedule_kind != "event_driven":
            failures.append(f"{entry['id']}: PAUSED requires event_driven schedule_kind")
            continue
        role_path = REPO / entry["role_file"]
        if not role_path.exists():
            failures.append(f"{entry['id']}: missing role file {entry['role_file']}")
            continue
        path = codex_home / "automations" / entry["id"] / "automation.toml"
        expected = _expected(entry)
        if not path.exists():
            if status == "PAUSED":
                successes.append(f"OK  {entry['id']}  PAUSED  event_driven  absent (expected)")
                continue
            failures.append(f"{entry['id']}: missing {path}")
            continue
        try:
            actual = tomllib.loads(path.read_text())
        except Exception as exc:
            failures.append(f"{entry['id']}: invalid TOML: {exc}")
            continue
        drift = [key for key, value in expected.items() if actual.get(key) != value]
        if drift:
            failures.append(f"{entry['id']}: drift in {', '.join(drift)}")
        else:
            successes.append(
                f"OK  {entry['id']}  {entry['status']}  {entry['schedule_kind']}  {entry['model']}"
            )
    return successes, failures


def main() -> int:
    plan = tomllib.loads(PLAN_PATH.read_text())
    successes, failures = audit(plan)
    for success in successes:
        print(success)

    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
