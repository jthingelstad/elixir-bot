"""PID file guard for the bot runtime.

launchd (KeepAlive=true) is the real process manager — see scripts/admin.sh.
This guard exists so an accidental manual `python elixir.py` cannot fight the
launchd instance: a pid file pointing at a live Elixir process means refuse to
start (the old SIGTERM-the-other-guy behavior just triggered a kill loop under
KeepAlive). A pid that is dead, or was recycled by some unrelated process, is
stale — overwrite it and start normally.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

log = logging.getLogger("elixir")
PID_FILE = os.path.join(os.path.dirname(__file__), "elixir.pid")


def _read_pid_file(pid_file: str | None = None) -> int | None:
    path = pid_file or PID_FILE
    try:
        with open(path) as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    if isinstance(payload, dict):
        pid = payload.get("pid")
    else:
        pid = payload
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _write_pid_file(pid_file: str | None = None) -> None:
    path = pid_file or PID_FILE
    payload = {
        "pid": os.getpid(),
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cwd": os.getcwd(),
        "entrypoint": "elixir.py",
    }
    with open(path, "w") as f:
        json.dump(payload, f)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _pid_looks_like_elixir(pid: int) -> bool:
    command = _process_command(pid).lower()
    if not command:
        return False
    markers = {
        "elixir.py",
        "runtime.app",
        os.path.basename(os.path.dirname(__file__)).lower(),
    }
    return any(marker and marker in command for marker in markers)


def _acquire_pid_file(pid_file: str | None = None) -> None:
    path = pid_file or PID_FILE
    old_pid = _read_pid_file(path)
    if old_pid and old_pid != os.getpid() and _process_exists(old_pid):
        if _pid_looks_like_elixir(old_pid):
            raise RuntimeError(
                f"Another Elixir process (pid {old_pid}) is already running per {path}. "
                "Stop it first (scripts/admin.sh stop) instead of starting a second copy."
            )
        log.warning(
            "Ignoring stale pid file %s pointing to non-Elixir process %d",
            path,
            old_pid,
        )
    _write_pid_file(path)


def _cleanup_pid_file(pid_file: str | None = None) -> None:
    path = pid_file or PID_FILE
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def main(token, bot):
    if not token:
        raise ValueError("DISCORD_TOKEN not set in .env")
    _acquire_pid_file()
    atexit.register(_cleanup_pid_file)
    bot.run(token, log_handler=None)
