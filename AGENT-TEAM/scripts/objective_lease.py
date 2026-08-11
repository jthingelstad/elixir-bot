#!/usr/bin/env python3
"""Atomic local checkout lease for the three AGENT-TEAM objectives."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEASE_PATH = REPO / ".git" / "agent-team-objective-lease.json"
OBJECTIVES = {"run", "game", "agent"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _read() -> dict | None:
    try:
        return json.loads(LEASE_PATH.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"objective lease is unreadable: {exc}") from exc


def claim(objective: str, *, now: datetime | None = None) -> dict:
    if objective not in OBJECTIVES:
        raise SystemExit(f"unknown objective {objective!r}; choose run, game, or agent")
    payload = {
        "objective": objective,
        "claimed_at": (now or _now()).isoformat().replace("+00:00", "Z"),
    }
    LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LEASE_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        current = _read()
        raise SystemExit(
            f"checkout lease is already held: {json.dumps(current, sort_keys=True)}"
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
    return payload


def release(objective: str) -> None:
    current = _read()
    if current is None:
        return
    if current.get("objective") != objective:
        raise SystemExit(
            f"checkout lease belongs to {current.get('objective')!r}, not {objective!r}"
        )
    LEASE_PATH.unlink()


def clear_stale(*, hours: float, now: datetime | None = None) -> dict:
    current = _read()
    if current is None:
        raise SystemExit("no checkout lease exists")
    try:
        claimed = datetime.fromisoformat(str(current["claimed_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise SystemExit("objective lease has no valid claimed_at; inspect it manually") from exc
    age = (now or _now()) - claimed
    if age < timedelta(hours=hours):
        raise SystemExit(f"objective lease is only {age}; stale threshold is {hours}h")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout
    if dirty:
        raise SystemExit("refusing to clear a stale lease while the worktree is dirty")
    LEASE_PATH.unlink()
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    claim_parser = sub.add_parser("claim")
    claim_parser.add_argument("objective", choices=sorted(OBJECTIVES))
    release_parser = sub.add_parser("release")
    release_parser.add_argument("objective", choices=sorted(OBJECTIVES))
    stale_parser = sub.add_parser("clear-stale")
    stale_parser.add_argument("--hours", type=float, default=8.0)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.command == "claim":
        print(json.dumps(claim(args.objective), sort_keys=True))
    elif args.command == "release":
        release(args.objective)
        print("released")
    elif args.command == "clear-stale":
        print(json.dumps(clear_stale(hours=args.hours), sort_keys=True))
    else:
        print(json.dumps(_read(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
