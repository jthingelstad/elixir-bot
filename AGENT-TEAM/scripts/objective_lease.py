#!/usr/bin/env python3
"""Atomic local checkout lease for the three AGENT-TEAM objectives."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEASE_PATH = REPO / ".git" / "agent-team-objective-lease.json"
OBJECTIVES = {"run", "game", "agent"}
LEGACY_HOLDER_ID = "legacy-unidentified"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _read() -> dict | None:
    try:
        return json.loads(LEASE_PATH.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"objective lease is unreadable: {exc}") from exc


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _checkout_is_clean() -> bool:
    return not _git("status", "--porcelain")


def claim(
    objective: str,
    *,
    now: datetime | None = None,
    holder_id: str | None = None,
    holder_pid: int | None = None,
    hostname: str | None = None,
    starting_head: str | None = None,
) -> dict:
    if objective not in OBJECTIVES:
        raise SystemExit(f"unknown objective {objective!r}; choose run, game, or agent")
    payload = {
        "objective": objective,
        "claimed_at": (now or _now()).isoformat().replace("+00:00", "Z"),
        "holder_id": holder_id or os.getenv("CODEX_THREAD_ID") or "untracked-manual-holder",
        "hostname": hostname or socket.gethostname(),
        "starting_head": starting_head or _git("rev-parse", "HEAD"),
    }
    if holder_pid is not None:
        payload["holder_pid"] = holder_pid
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


def _require_clean_checkout() -> None:
    if not _checkout_is_clean():
        raise SystemExit("refusing to clear a lease while the worktree is dirty")


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
    _require_clean_checkout()
    current_head = _git("rev-parse", "HEAD")
    if current.get("starting_head") != current_head:
        raise SystemExit(
            "refusing automatic stale clear because HEAD changed; inspect and clear manually"
        )
    if current.get("hostname") != socket.gethostname():
        raise SystemExit(
            "cannot prove a lease holder on another host is inactive; inspect and clear manually"
        )
    holder_pid = current.get("holder_pid")
    if not isinstance(holder_pid, int):
        raise SystemExit("lease has no durable holder process; inspect and clear manually")
    if _process_exists(holder_pid):
        raise SystemExit(f"lease holder process {holder_pid} is still active")
    LEASE_PATH.unlink()
    return current


def clear_manual(*, holder_id: str, confirm_inactive: bool) -> dict:
    current = _read()
    if current is None:
        raise SystemExit("no checkout lease exists")
    if not confirm_inactive:
        raise SystemExit("manual clear requires --confirm-inactive")
    recorded_holder = current.get("holder_id") or LEGACY_HOLDER_ID
    if recorded_holder != holder_id:
        raise SystemExit(f"lease holder is {recorded_holder!r}, not {holder_id!r}")
    _require_clean_checkout()
    LEASE_PATH.unlink()
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    claim_parser = sub.add_parser("claim")
    claim_parser.add_argument("objective", choices=sorted(OBJECTIVES))
    claim_parser.add_argument("--holder-id")
    claim_parser.add_argument("--holder-pid", type=int)
    release_parser = sub.add_parser("release")
    release_parser.add_argument("objective", choices=sorted(OBJECTIVES))
    stale_parser = sub.add_parser("clear-stale")
    stale_parser.add_argument("--hours", type=float, default=8.0)
    manual_parser = sub.add_parser("clear-manual")
    manual_parser.add_argument("--holder-id", required=True)
    manual_parser.add_argument("--confirm-inactive", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.command == "claim":
        print(
            json.dumps(
                claim(args.objective, holder_id=args.holder_id, holder_pid=args.holder_pid),
                sort_keys=True,
            )
        )
    elif args.command == "release":
        release(args.objective)
        print("released")
    elif args.command == "clear-stale":
        print(json.dumps(clear_stale(hours=args.hours), sort_keys=True))
    elif args.command == "clear-manual":
        print(
            json.dumps(
                clear_manual(holder_id=args.holder_id, confirm_inactive=args.confirm_inactive),
                sort_keys=True,
            )
        )
    else:
        print(json.dumps(_read(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
