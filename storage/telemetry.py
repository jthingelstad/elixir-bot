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
  - `db_stalls`      — a stack dump per detected stall (see watchdog.py)
  - `wake_observations` / `wake_episodes` — what the wake evaluator saw and did
"""

from __future__ import annotations

import json
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
OWNER_ONLY_MODE = 0o600

_local = threading.local()
_schema_lock = threading.Lock()
_schema_ready = False


def telemetry_path() -> str:
    return os.getenv("ELIXIR_TELEMETRY_DB_PATH") or DEFAULT_PATH


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _narrow_owner_only_family(path: str) -> None:
    """Create/narrow the telemetry SQLite family without relying on umask.

    ``os.open(..., 0o600)`` can only be narrowed further by a restrictive
    umask, never widened by a permissive one. Pre-creating the database this
    way closes the creation-time exposure in ``sqlite3.connect``. Existing
    database/WAL/SHM files are reconciled before open; the second call after
    WAL activation catches sidecars SQLite created during this connection.

    Permission hardening remains fail-soft like the rest of this store. A
    read-only or otherwise unfixable telemetry file must be visible to an
    operator, but must not break the clan workload.
    """
    if path == ":memory:":
        return
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, OWNER_ONLY_MODE)
    except FileExistsError:
        pass
    except OSError:
        log.warning("telemetry: could not securely pre-create database", exc_info=True)
    else:
        os.close(fd)

    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.chmod(candidate, OWNER_ONLY_MODE)
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("telemetry: could not narrow permissions for %s", candidate, exc_info=True)


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
    # There is no `db_lock_waits` table. One existed from the 2026-08-03 split
    # until 2026-08-06 and recorded zero rows the whole time, because nothing
    # ever called its writer; it was dropped from the live file too, so this is
    # not a table waiting to be refilled. It cannot be revived as written: `PRAGMA
    # busy_timeout` (db/__init__.py) makes SQLite block inside the C layer, and
    # Python's sqlite3 exposes no `sqlite3_busy_handler`, so there is no wait
    # boundary to observe and no retry loop to instrument. Measuring waits would
    # mean hand-rolling retries around every write in the bot. Hold time is the
    # cause and wait time only the symptom, so db_transactions/db_stalls below
    # are the instrument that matters.
    """CREATE TABLE IF NOT EXISTS db_stalls (
        stall_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        call_site TEXT,
        open_ms INTEGER NOT NULL,
        thread_dump TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_db_stall_recorded ON db_stalls(recorded_at)",
    # Agentic Loop v2 Phase 0: what the wake evaluator WOULD have fired. Lives
    # here and not in the clan DB because a shadow run is disposable
    # measurement, and Phase 0 is explicitly a no-migration phase.
    """CREATE TABLE IF NOT EXISTS wake_observations (
        observation_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        mode TEXT NOT NULL,
        wake_class TEXT NOT NULL,
        wake_model TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        signal_keys_json TEXT NOT NULL,
        event_types_json TEXT NOT NULL,
        oldest_observed_at TEXT,
        reason TEXT,
        fired INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wake_obs_recorded ON wake_observations(recorded_at)",
    # Agentic Loop v2 Phase 1: what a chassis turn thought and did. This is the
    # substrate the nightly reflection loop will read — trigger, tool trace,
    # posts, rejections, cost — and it lives here rather than the clan DB
    # because an episode is observation, not clan state.
    """CREATE TABLE IF NOT EXISTS wake_episodes (
        episode_id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        job TEXT,
        workflow TEXT,
        tier TEXT,
        handled INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        episode_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wake_episodes_recorded ON wake_episodes(recorded_at)",
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
    path = telemetry_path()
    _narrow_owner_only_family(path)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    _narrow_owner_only_family(path)
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
_ADDED_COLUMNS = (
    ("db_transactions", "sites_json", "TEXT"),
    # ── Call configuration (2026-08-09) ──
    # What the call was ASKED to do. Every one of these was needed to explain an
    # incident this week and none of them were recorded, so each answer came
    # from re-running the workflow or wrapping the client by hand:
    #   effort      — the thinking lever; introduced 2026-08-08 with no visibility
    #                 at all, so there was no way to relate depth to cost.
    #   max_tokens  — a stop_reason of max_tokens is uninterpretable without the
    #                 ceiling it hit; it previously appeared only inside an error
    #                 string, and only when the caller asked for errors.
    #   timeout_s   — the 60s default was inferred from a cluster of failures at
    #                 181.7s (60 x 3 retries) rather than read off a column.
    ("llm_calls", "effort", "TEXT"),
    ("llm_calls", "max_tokens", "INTEGER"),
    ("llm_calls", "timeout_s", "INTEGER"),
    # ── Call outcome (2026-08-09) ──
    #   stop_reason    — promoted OUT of response_json, which is pruned at 14
    #                    days while the row lives 90. Truncation history was
    #                    readable for 2,092 of 9,297 rows; the outcome that
    #                    matters most was the first thing thrown away.
    #   block_census   — same promotion: per-type block counts, which is how you
    #                    tell "the model returned nothing" from "the model spent
    #                    the whole budget thinking". Store the counts, NOT a
    #                    character length: `thinking.display` defaults to
    #                    "omitted", so a thinking block's text is always empty
    #                    and any char count of it reads as 0 whether or not
    #                    thinking happened. The block being PRESENT is the
    #                    signal; pair it with completion_tokens for the size.
    #   attempts       — API round trips. Every claude-5 call was making two (a
    #                    guaranteed 400 on temperature, then the real request)
    #                    and nothing recorded it; it took live instrumentation
    #                    to see. A retry after a timeout is equally invisible.
    #   cost_usd       — already computed on every call for the spend budget and
    #                    then discarded, so reports recomputed it from a second
    #                    pricing table. Priced at write time, history stays
    #                    correct when rates change.
    ("llm_calls", "stop_reason", "TEXT"),
    ("llm_calls", "block_census", "TEXT"),
    ("llm_calls", "attempts", "INTEGER"),
    ("llm_calls", "cost_usd", "REAL"),
    # Correlates the N calls of one agent turn. A tool-using workflow makes
    # several calls per turn, so per-workflow totals could never answer "what
    # did one awareness tick cost?" — only "what did awareness cost all week".
    ("llm_calls", "turn_id", "TEXT"),
)


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
    effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
    timeout_s: Optional[int] = None,
    stop_reason: Optional[str] = None,
    block_census: Optional[str] = None,
    attempts: Optional[int] = None,
    cost_usd: Optional[float] = None,
    turn_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record one model call. Signature matches the clan-DB version it replaced,
    including the ignored ``conn`` — callers used to hand it the clan connection
    and that is exactly the coupling this move removes.

    Everything after ``response_json`` is the call's configuration and outcome as
    first-class columns rather than fields buried in the two blobs, which are
    pruned at 14 days while the row itself lives 90. See ``_ADDED_COLUMNS``."""
    try:
        telemetry = connect()
        telemetry.execute(
            "INSERT INTO llm_calls (recorded_at, workflow, model, ok, error, duration_ms, "
            "prompt_tokens, completion_tokens, total_tokens, cache_creation_tokens, "
            "cache_read_tokens, prompt_json, response_json, effort, max_tokens, timeout_s, "
            "stop_reason, block_census, attempts, cost_usd, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                effort,
                max_tokens,
                timeout_s,
                stop_reason,
                block_census,
                attempts,
                cost_usd,
                turn_id,
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
    txn_id: Optional[int] = None,
) -> Optional[int]:
    """Record one write transaction; returns its row id.

    ``txn_id`` UPDATES an existing row instead of inserting. The watchdog writes a
    provisional ``outcome='stalled'`` row as soon as a transaction crosses the
    threshold, because a transaction that hangs and dies with its process would
    otherwise never be recorded at all — and those are the ones that matter.
    Finalizing in place keeps it to one row per transaction.
    """
    try:
        conn = connect()
        if txn_id is not None:
            conn.execute(
                "UPDATE db_transactions SET held_ms = ?, statements = ?, outcome = ?, "
                "sites_json = COALESCE(?, sites_json) WHERE txn_id = ?",
                (int(held_ms), int(statements), outcome, sites_json, int(txn_id)),
            )
            conn.commit()
            return txn_id
        cur = conn.execute(
            "INSERT INTO db_transactions "
            "(recorded_at, call_site, held_ms, statements, outcome, sites_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_utcnow(), call_site, int(held_ms), int(statements), outcome, sites_json),
        )
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        log.debug("telemetry: transaction record failed", exc_info=True)
        return None


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


def record_wake_observation(
    *,
    mode: str,
    wake_class: str,
    wake_model: str,
    signal_keys: list,
    event_types: list,
    oldest_observed_at: str | None = None,
    reason: str = "",
    fired: bool = True,
) -> None:
    """One wake decision, fired or held. ``mode`` is ``shadow`` or ``live``.

    Held decisions are recorded too (``fired=0``): "we would have fired but the
    daily budget was spent" is the observation that tells us the budget is
    wrong, and it is invisible if only fires are stored.
    """
    try:
        conn = connect()
        conn.execute(
            "INSERT INTO wake_observations (recorded_at, mode, wake_class, wake_model, "
            "event_count, signal_keys_json, event_types_json, oldest_observed_at, reason, fired) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utcnow(),
                mode,
                wake_class,
                wake_model,
                len(signal_keys or []),
                json.dumps(list(signal_keys or [])),
                json.dumps(list(event_types or [])),
                oldest_observed_at,
                reason[:400],
                1 if fired else 0,
            ),
        )
        conn.commit()
    except Exception:
        log.debug("telemetry: wake observation record failed", exc_info=True)


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
            ("db_stalls", "recorded_at", DB_METRIC_RETENTION_DAYS),
            ("wake_observations", "recorded_at", DB_METRIC_RETENTION_DAYS),
            ("wake_episodes", "recorded_at", DB_METRIC_RETENTION_DAYS),
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
    "record_stall",
    "record_transaction",
    "record_wake_observation",
    "telemetry_path",
]
