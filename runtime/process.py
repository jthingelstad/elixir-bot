"""PID file and process helpers for the bot runtime."""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import subprocess
import time
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


def _write_pid_file(pid_file: str | None = None, *, os_module=os) -> None:
    path = pid_file or PID_FILE
    payload = {
        "pid": os_module.getpid(),
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cwd": os_module.getcwd(),
        "entrypoint": "elixir.py",
    }
    with open(path, "w") as f:
        json.dump(payload, f)


def _process_exists(pid: int, *, os_module=os) -> bool:
    try:
        os_module.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int, *, subprocess_module=subprocess) -> str:
    try:
        return subprocess_module.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess_module.SubprocessError, OSError):
        return ""


def _pid_looks_like_elixir(pid: int, *, process_command=None) -> bool:
    command_fn = process_command or _process_command
    command = command_fn(pid).lower()
    if not command:
        return False
    markers = {
        "elixir.py",
        "runtime.app",
        os.path.basename(os.path.dirname(__file__)).lower(),
    }
    return any(marker and marker in command for marker in markers)


def _wait_for_process_exit(pid: int, timeout_seconds: float = 5.0, *, process_exists=None) -> bool:
    exists = process_exists or _process_exists
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not exists(pid):
            return True
        time.sleep(0.1)
    return not exists(pid)


def _acquire_pid_file(
    *,
    pid_file: str | None = None,
    read_pid_file=None,
    write_pid_file=None,
    process_exists=None,
    pid_looks_like_elixir=None,
    wait_for_process_exit=None,
    os_module=os,
    signal_module=signal,
    logger=log,
):
    path = pid_file or PID_FILE
    read_pid = read_pid_file or (lambda: _read_pid_file(path))
    write_pid = write_pid_file or (lambda: _write_pid_file(path, os_module=os_module))
    exists = process_exists or (lambda pid: _process_exists(pid, os_module=os_module))
    looks_like = pid_looks_like_elixir or _pid_looks_like_elixir
    wait_exit = wait_for_process_exit or (lambda pid: _wait_for_process_exit(pid, process_exists=exists))
    if os_module.path.exists(path):
        old_pid = read_pid()
        if old_pid and old_pid != os_module.getpid() and exists(old_pid):
            if looks_like(old_pid):
                try:
                    os_module.kill(old_pid, signal_module.SIGTERM)
                except PermissionError as exc:
                    raise RuntimeError(
                        f"Existing Elixir process {old_pid} could not be terminated."
                    ) from exc
                if not wait_exit(old_pid):
                    raise RuntimeError(
                        f"Existing Elixir process {old_pid} did not exit after SIGTERM."
                    )
                logger.info("Stopped prior Elixir process %d", old_pid)
            else:
                logger.warning(
                    "Ignoring stale pid file %s pointing to non-Elixir process %d",
                    path,
                    old_pid,
                )
    write_pid()


def _cleanup_pid_file(pid_file: str | None = None, *, os_module=os):
    path = pid_file or PID_FILE
    try:
        os_module.remove(path)
    except FileNotFoundError:
        pass


def main(token, bot, *, acquire_pid_file=None, cleanup_pid_file=None, atexit_module=atexit):
    if not token:
        raise ValueError("DISCORD_TOKEN not set in .env")
    acquire = acquire_pid_file or _acquire_pid_file
    cleanup = cleanup_pid_file or _cleanup_pid_file
    acquire()
    atexit_module.register(cleanup)
    bot.run(token, log_handler=None)
