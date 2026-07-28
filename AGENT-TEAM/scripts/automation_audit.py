#!/usr/bin/env python3
"""Check or apply the versioned Codex automation plan for AGENT-TEAM."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO / "AGENT-TEAM" / "automations.toml"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _prompt(entry: dict) -> str:
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
        "Start with the shared preflight, respect issue claims and lane boundaries, do one "
        "focused thing, write the required AGENT-TEAM run note, and end with a clean repository. "
        "If main is ahead of origin/main, remain read-only: never commit, push, deploy, or restart "
        "into pre-existing commits. Never push a commit this run did not create."
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


def _replace(text: str, key: str, value) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    line = f"{key} = {encoded}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    return text.rstrip() + "\n" + line + "\n"


def _apply(path: Path, expected: dict) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist; create the automation in Codex once so its project target is known"
        )
    text = path.read_text()
    for key, value in expected.items():
        text = _replace(text, key, value)
    text = _replace(text, "updated_at", int(time.time() * 1000))
    path.write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="update existing Codex automation files"
    )
    args = parser.parse_args()

    plan = tomllib.loads(PLAN_PATH.read_text())
    failures: list[str] = []
    for entry in plan["automation"]:
        role_path = REPO / entry["role_file"]
        if not role_path.exists():
            failures.append(f"{entry['id']}: missing role file {entry['role_file']}")
            continue
        path = CODEX_HOME / "automations" / entry["id"] / "automation.toml"
        expected = _expected(entry)
        if args.apply:
            try:
                _apply(path, expected)
            except Exception as exc:
                failures.append(f"{entry['id']}: apply failed: {exc}")
                continue
        if not path.exists():
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
            print(f"OK  {entry['id']}  {entry['rrule']}  {entry['model']}")

    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
