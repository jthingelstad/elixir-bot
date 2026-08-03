"""Elixir's telemetry store — a SEPARATE SQLite file from the clan database.

Why a second file rather than more tables in `elixir-v51.db`:

SQLite has one writer. Telemetry written into the operational database takes the
same write lock as the work it is measuring, so the instrument contends with its
subject — and on the day this was written that subject was *lock contention*,
which makes same-file telemetry actively counterproductive. A second file has its
own lock, its own WAL and its own retention, and costs nothing the first file
cares about.

It is a database rather than a log file on purpose. The first question anyone
asks of this data is "p95 transaction hold time by call site this week", which is
a GROUP BY. JSONL would mean writing a parser, and then maintaining a worse
database. (If volume ever makes the direct writes hurt, the upgrade is to append
JSONL on the hot path and roll it in here on a schedule — but measure first.)

**Nothing in this module may raise into a caller.** Telemetry that can break the
workload is worse than no telemetry, so every public function swallows and logs.

The primary database stays about the clan. This one holds:
  - `llm_calls`      — every model call (moved out of the clan DB, 2026-08-03)
  - `db_transactions`— write-lock hold time by call site
  - `db_lock_waits`  — SQLITE_BUSY events, who waited and how long
  - `db_stalls`      — a stack dump per detected stall (see watchdog.py)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("elixir")

DEFAULT_PATH = "elixir-telemetry.db"
LLM_CALL_RETENTION_DAYS = 90
LLM_BLOB_RETENTION_DAYS = 14
DB_METRIC_RETENTION_DAYS = 30

_local = threading.local()
_schema_lock = threading.Lock()
_schema_ready = False


def telemetry_path() -> str:
    return os.getenv("ELIXIR_TELEMETRY_DB_PATH") or DEFAULT_PATH


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS llm_calls (
        call_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        workflow TEXT,
        model TEXT,
        ok INTEGER NOT NULL DEFAULT 1,
        error TEXT,
        duration_ms INTEGER,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        total_tokens INTEGER,
        cache_creation_tokens INTEGER,
        cache_read_tokens INTEGER,
        prompt_json TEXT,
        response_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_recorded ON llm_calls(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_workflow ON llm_calls(workflow)",
    # Hold time, not query time. Nothing here is slow; things hold the lock.
    """CREATE TABLE IF NOT EXISTS db_transactions (
        txn_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        call_site TEXT,
        held_ms INTEGER NOT NULL,
        statements INTEGER,
        outcome TEXT NOT NULL,
        -- Per-site breakdown WITHIN this transaction, heaviest first:
        -- [{"site": "engine/x.py:12", "n": 3969, "ms": 118.4}, ...]
        -- `call_site` above names only whoever OPENED the transaction, which is
        -- not usually who spent the time. Read this column before blaming a line.
        sites_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_db_txn_recorded ON db_transactions(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_db_txn_held ON db_transactions(held_ms)",
    """CREATE TABLE IF NOT EXISTS db_lock_waits (
        wait_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        call_site TEXT,
        waited_ms INTEGER NOT NULL,
        resolved INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_db_wait_recorded ON db_lock_waits(recorded_at)",
    """CREATE TABLE IF NOT EXISTS db_stalls (
        stall_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        call_site TEXT,
        open_ms INTEGER NOT NULL,
        thread_dump TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_db_stall_recorded ON db_stalls(recorded_at)",
)


def connect() -> sqlite3.Connection:
    """A per-thread connection to the telemetry file.

    Per-thread because this is written from the engine tick, the awareness worker
    and the scheduler concurrently, and sqlite3 connections are not shareable
    across threads by default.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            # hygiene: not an outage — this is the "somebody closed our cached
            # handle" case, and reopening on the next line IS the handling.
            # Somebody closed it. A cached handle that outlives its connection
            # turns every later write into a hard error, and telemetry must never
            # be the thing that breaks — reopen instead.
            _local.conn = None
    conn = sqlite3.connect(telemetry_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    # Durability is not worth a fsync per row for telemetry: losing the last few
    # observations to a hard crash costs nothing, and the writes are frequent.
    conn.execute("PRAGMA synchronous = NORMAL")
    _local.conn = conn
    _ensure_schema(conn)
    return conn


# Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` is a
# no-op against an existing file, so a new column needs an explicit ALTER. This
# file is disposable telemetry, not clan data — no version counter, just make the
# shape current and move on.
_ADDED_COLUMNS = (("db_transactions", "sites_json", "TEXT"),)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        for statement in _SCHEMA:
            conn.execute(statement)
        for table, column, decl in _ADDED_COLUMNS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
        _schema_ready = True


def record_llm_call(
    workflow: str,
    model: str,
    *,
    ok: bool = True,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    prompt_json: Optional[str] = None,
    response_json: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record one model call. Signature matches the clan-DB version it replaced,
    including the ignored ``conn`` — callers used to hand it the clan connection
    and that is exactly the coupling this move removes."""
    try:
        telemetry = connect()
        telemetry.execute(
            "INSERT INTO llm_calls (recorded_at, workflow, model, ok, error, duration_ms, "
            "prompt_tokens, completion_tokens, total_tokens, cache_creation_tokens, "
            "cache_read_tokens, prompt_json, response_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utcnow(),
                workflow,
                model,
                1 if ok else 0,
                str(error) if error else None,
                duration_ms,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                prompt_json,
                response_json,
            ),
        )
        telemetry.commit()
    except Exception:
        log.debug("telemetry: llm call record failed", exc_info=True)


def record_transaction(
    call_site: str,
    held_ms: int,
    *,
    statements: int = 0,
    outcome: str,
    sites_json: Optional[str] = None,
) -> None:
    try:
        conn = connect()
        conn.execute(
            "INSERT INTO db_transactions "
            "(recorded_at, call_site, held_ms, statements, outcome, sites_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_utcnow(), call_site, int(held_ms), int(statements), outcome, sites_json),
        )
        conn.commit()
    except Exception:
        log.debug("telemetry: transaction record failed", exc_info=True)


def record_lock_wait(call_site: str, waited_ms: int, *, resolved: bool) -> None:
    try:
        conn = connect()
        conn.execute(
            "INSERT INTO db_lock_waits (recorded_at, call_site, waited_ms, resolved) "
            "VALUES (?, ?, ?, ?)",
            (_utcnow(), call_site, int(waited_ms), 1 if resolved else 0),
        )
        conn.commit()
    except Exception:
        log.debug("telemetry: lock wait record failed", exc_info=True)


def record_stall(call_site: str, open_ms: int, thread_dump: str) -> None:
    try:
        conn = connect()
        conn.execute(
            "INSERT INTO db_stalls (recorded_at, call_site, open_ms, thread_dump) "
            "VALUES (?, ?, ?, ?)",
            (_utcnow(), call_site, int(open_ms), thread_dump),
        )
        conn.commit()
    except Exception:
        log.debug("telemetry: stall record failed", exc_info=True)


def purge_old(now: datetime | None = None) -> dict:
    """Retention for the telemetry file. Runs on its own connection, so it can
    never block the clan database."""
    now = now or datetime.now(timezone.utc)

    def cutoff(days: int) -> str:
        return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    stats: dict = {}
    try:
        conn = connect()
        cur = conn.execute(
            "UPDATE llm_calls SET prompt_json = NULL, response_json = NULL "
            "WHERE recorded_at < ? AND (prompt_json IS NOT NULL OR response_json IS NOT NULL)",
            (cutoff(LLM_BLOB_RETENTION_DAYS),),
        )
        stats["llm_calls_blobs_pruned"] = cur.rowcount
        for table, column, days in (
            ("llm_calls", "recorded_at", LLM_CALL_RETENTION_DAYS),
            ("db_transactions", "recorded_at", DB_METRIC_RETENTION_DAYS),
            ("db_lock_waits", "recorded_at", DB_METRIC_RETENTION_DAYS),
            ("db_stalls", "recorded_at", DB_METRIC_RETENTION_DAYS),
        ):
            cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff(days),))
            stats[f"{table}_purged"] = cur.rowcount
        conn.commit()
    except Exception:
        log.warning("telemetry: purge failed", exc_info=True)
    return stats


__all__ = [
    "connect",
    "purge_old",
    "record_llm_call",
    "record_lock_wait",
    "record_stall",
    "record_transaction",
    "telemetry_path",
]
