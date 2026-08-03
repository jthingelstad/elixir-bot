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

Two things this got wrong the first time, both found by probing rather than by
reading the code — a partial instrument reports zero rows and looks exactly like
a healthy system:

1. Instrumentation must sit on the CURSOR, not the connection. `conn.execute()`
   is only one of the ways this codebase writes; `conn.executemany()`,
   `cur.execute()` and `cur.executemany()` all bypassed a connection-level
   override entirely, so those writes opened no record and were invisible to the
   watchdog. `Connection.execute` is documented as a shortcut for
   `self.cursor().execute(...)`, so routing it through our cursor makes the
   cursor the single observation point.
2. `with conn:` commits inside CPython's C `__exit__`, which does NOT call a
   Python-level `commit()` override. Records stayed open and were closed by
   whatever committed next, reporting one transaction's hold time under another
   transaction's call site — and left open long enough, they would have tripped
   the watchdog for a stall that was not happening.

Whether a transaction is actually open is answered by `sqlite3`'s own
`in_transaction`, not by pattern-matching the SQL. The prefix list decides only
whether an *opening* statement is a write; closing follows `in_transaction`,
which is what makes implicit commits (`executescript`) and autocommit DDL — a
`CREATE TABLE` outside a transaction — stop leaking permanently-open records.

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


def _is_write(sql: str) -> bool:
    return sql.lstrip()[:8].lower().startswith(_WRITE_PREFIXES)


class InstrumentedCursor(sqlite3.Cursor):
    """The single observation point. Every execute path lands here."""

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        try:
            return super().execute(sql, parameters)
        finally:
            self.connection._observe(sql)

    def executemany(self, sql, parameters, /):  # type: ignore[override]
        try:
            return super().executemany(sql, parameters)
        finally:
            self.connection._observe(sql)

    def executescript(self, sql_script, /):  # type: ignore[override]
        try:
            return super().executescript(sql_script)
        finally:
            # executescript issues an implicit COMMIT first and leaves autocommit
            # state behind it; _observe closes any record it finds open.
            self.connection._observe(sql_script)


class InstrumentedConnection(sqlite3.Connection):
    """A connection that reports how long it holds the write lock.

    Subclassing rather than wrapping so it stays a real sqlite3.Connection for
    every caller (row_factory, context manager, cursors) — this sits under the
    whole codebase and must be invisible.
    """

    def cursor(self, factory=InstrumentedCursor):  # type: ignore[override]
        return super().cursor(factory)

    # The connection-level shortcuts are re-expressed as what the docs say they
    # already are — cursor().execute(...) — so the cursor stays the ONLY place
    # that observes, and nothing can be counted twice.
    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):  # type: ignore[override]
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script, /):  # type: ignore[override]
        return self.cursor().executescript(sql_script)

    def _observe(self, sql: str) -> None:
        """Reconcile our record of this connection against sqlite's own state."""
        try:
            live = self.in_transaction
        except sqlite3.ProgrammingError:
            # hygiene: not an outage. `in_transaction` raises only on a closed
            # connection, and close() already flushed the record — there is
            # nothing left to observe and nothing to report.
            return
        if live:
            if _is_write(sql) or id(self) in _open_writes:
                self._note_write()
        elif id(self) in _open_writes:
            # No transaction is open, so nothing is holding the write lock —
            # whatever we were tracking ended (implicit or external commit). The
            # membership test keeps the common case (a read in autocommit) off
            # the mutex; _close_txn re-checks under it.
            self._close_txn("autocommit")

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

    def __exit__(self, exc_type, exc_value, tb):  # type: ignore[override]
        # CPython commits/rolls back inside the C-level __exit__ WITHOUT calling
        # a Python commit() override, so `with conn:` is a closing path in its
        # own right and has to be caught here.
        try:
            return super().__exit__(exc_type, exc_value, tb)
        finally:
            self._close_txn("rollback" if exc_type is not None else "commit")


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
