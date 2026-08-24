#!/usr/bin/env python3
"""Resolve a registered Codex automation's compact-memory path safely."""

from __future__ import annotations

import argparse
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO / "AGENT-TEAM" / "automations.toml"
DEFAULT_CODEX_HOME = Path.home() / ".codex"


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """Use CODEX_HOME when available, otherwise Codex's standard local root."""
    values = os.environ if environ is None else environ
    raw = values.get("CODEX_HOME", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_CODEX_HOME


def registered_ids(plan_path: Path = PLAN_PATH) -> set[str]:
    plan = tomllib.loads(plan_path.read_text())
    return {str(entry["id"]) for entry in plan.get("automation", [])}


def memory_path(
    automation_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    plan_path: Path = PLAN_PATH,
) -> Path:
    if automation_id not in registered_ids(plan_path):
        raise ValueError(f"unknown automation id: {automation_id}")
    return codex_home(environ) / "automations" / automation_id / "memory.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("automation_id", help="registered id from AGENT-TEAM/automations.toml")
    args = parser.parse_args()
    try:
        print(memory_path(args.automation_id))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
