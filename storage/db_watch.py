"""Write-lock instrumentation and the stall watchdog.

SQLite has one writer. When something holds that writer open, everything else in
the process queues behind it and then fails on busy_timeout — which is how the
bot spent two mornings at 100% CPU with its engine tick reporting
`database is locked`. A restart cleared it both times and told us nothing.

Two pieces here:

`InstrumentedConnection` tracks, per connection, when a write transaction opened
and how long it was held. That is the metric that matters. Query duration is the
obvious thing to measure and the least useful one here: nothing is slow, things
*hold*.

`start_watchdog()` runs a thread that notices a write transaction open longer
than the threshold and dumps every thread's stack. One occurrence names the
culprit. Weeks of aggregate telemetry would only re-confirm what we already know
— something holds the lock — without ever saying what.

Everything degrades silently. A watchdog that can break the bot is worse than a
stall you have to restart.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
import traceback

log = logging.getLogger("elixir")

# A write transaction open longer than this is not working, it is stuck. The
# engine tick's own generation transaction is the longest legitimate holder and
# runs in low single-digit seconds.
STALL_SECONDS = float(os.getenv("ELIXIR_DB_STALL_SECONDS", "45"))
WATCH_INTERVAL_SECONDS = 5.0
# Report a transaction to telemetry only if it held the lock at least this long;
# below it the row is noise and the write itself would dominate the measurement.
REPORT_MS = int(os.getenv("ELIXIR_DB_REPORT_MS", "250"))

_WRITE_PREFIXES = ("insert", "update", "delete", "replace", "create", "drop", "alter", "begin")

_open_writes: dict[int, dict] = {}
_lock = threading.Lock()
_watchdog: threading.Thread | None = None
_reported: set[int] = set()


def _call_site() -> str:
    """The first frame outside the storage/db layer — who actually asked."""
    try:
        for frame in traceback.extract_stack()[::-1]:
            name = frame.filename.replace("\\", "/")
            if "/storage/db_watch" in name or "/db/__init__" in name or "sqlite3" in name:
                continue
            if "/elixir-bot/" not in name:
                continue
            short = name.split("/elixir-bot/")[-1]
            return f"{short}:{frame.lineno}"
    except Exception:  # noqa: BLE001
        # hygiene: call-site attribution is a label on a metric. If walking the
        # stack fails, the metric still gets recorded as "unknown" — logging here
        # would fire on every sample of a hot path.
        return "unattributed"
    return "unknown"


class InstrumentedConnection(sqlite3.Connection):
    """A connection that reports how long it holds the write lock.

    Subclassing rather than wrapping so it stays a real sqlite3.Connection for
    every caller (row_factory, context manager, cursors) — this sits under the
    whole codebase and must be invisible.
    """

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        stripped = sql.lstrip()[:8].lower()
        if stripped.startswith(_WRITE_PREFIXES):
            self._note_write()
        return super().execute(sql, parameters)

    def _note_write(self) -> None:
        key = id(self)
        with _lock:
            entry = _open_writes.get(key)
            if entry is None:
                _open_writes[key] = {
                    "started": time.monotonic(),
                    "call_site": _call_site(),
                    "statements": 1,
                    "thread": threading.current_thread().name,
                }
            else:
                entry["statements"] += 1

    def _close_txn(self, outcome: str) -> None:
        key = id(self)
        with _lock:
            entry = _open_writes.pop(key, None)
            _reported.discard(key)
        if not entry:
            return
        held_ms = int((time.monotonic() - entry["started"]) * 1000)
        if held_ms < REPORT_MS:
            return
        try:
            from storage import telemetry

            telemetry.record_transaction(
                entry["call_site"], held_ms, statements=entry["statements"], outcome=outcome
            )
        except Exception:  # noqa: BLE001 - telemetry never breaks the caller
            log.debug("db_watch: transaction record failed", exc_info=True)

    def commit(self):  # type: ignore[override]
        try:
            return super().commit()
        finally:
            self._close_txn("commit")

    def rollback(self):  # type: ignore[override]
        try:
            return super().rollback()
        finally:
            self._close_txn("rollback")

    def close(self):  # type: ignore[override]
        try:
            return super().close()
        finally:
            self._close_txn("close")


def _dump_threads() -> str:
    lines = []
    frames = sys._current_frames()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        lines.append(f"--- thread {thread.name} (daemon={thread.daemon}) ---")
        if frame is None:
            lines.append("    <no frame>")
            continue
        lines.extend("    " + s.rstrip() for s in traceback.format_stack(frame))
    return "\n".join(lines)


def _watch_once() -> None:
    now = time.monotonic()
    stalled = []
    with _lock:
        for key, entry in _open_writes.items():
            if now - entry["started"] >= STALL_SECONDS and key not in _reported:
                _reported.add(key)
                stalled.append((key, dict(entry)))
    for _key, entry in stalled:
        open_ms = int((now - entry["started"]) * 1000)
        dump = _dump_threads()
        log.error(
            "DB STALL: write transaction open %.1fs from %s (thread %s, %d statements). "
            "Thread dump follows.\n%s",
            open_ms / 1000,
            entry["call_site"],
            entry["thread"],
            entry["statements"],
            dump,
        )
        try:
            from storage import telemetry

            telemetry.record_stall(entry["call_site"], open_ms, dump)
        except Exception:  # noqa: BLE001
            log.debug("db_watch: stall record failed", exc_info=True)


def _loop() -> None:
    while True:
        try:
            _watch_once()
        except Exception:  # noqa: BLE001 - the watchdog must outlive its own bugs
            log.debug("db_watch: watch pass failed", exc_info=True)
        time.sleep(WATCH_INTERVAL_SECONDS)


def start_watchdog() -> bool:
    """Start the stall watchdog. Idempotent; safe to call on every startup."""
    global _watchdog
    if _watchdog is not None and _watchdog.is_alive():
        return False
    _watchdog = threading.Thread(target=_loop, name="db-stall-watchdog", daemon=True)
    _watchdog.start()
    log.info("db stall watchdog started (threshold %.0fs)", STALL_SECONDS)
    return True


def open_write_transactions() -> list[dict]:
    """Snapshot for the Observatory / diagnostics."""
    now = time.monotonic()
    with _lock:
        return [
            {
                "call_site": e["call_site"],
                "thread": e["thread"],
                "open_ms": int((now - e["started"]) * 1000),
                "statements": e["statements"],
            }
            for e in _open_writes.values()
        ]


__all__ = [
    "InstrumentedConnection",
    "open_write_transactions",
    "start_watchdog",
]
