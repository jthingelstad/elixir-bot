"""The incident ledger — fail-soft, but never fail-SILENT (confidence plan §1).

Every best-effort / swallowing except in the codebase (thread ops, delivery
audit, enrichment, poison-skip, backup, …) records here before it passes or
logs. The point is visibility: a failure that used to vanish into a bare
`except` now leaves a queryable row with a traceback, so an external agent
(Claude Code) can answer "what's failing?" in one query:

    sqlite3 elixir-v51.db "SELECT at, component, summary FROM runtime_incidents
                           WHERE resolved_at IS NULL ORDER BY at DESC LIMIT 50"

record_incident NEVER raises — an observability layer that can crash the thing
it observes is worse than useless. It swallows its own errors to the log.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import traceback as _tb
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("elixir.incidents")

INCIDENTS_DDL = """
CREATE TABLE IF NOT EXISTS runtime_incidents (
    incident_id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    component TEXT NOT NULL,          -- where it happened (e.g. 'threads', 'delivery.audit')
    severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('warn','error')),
    summary TEXT NOT NULL,            -- one-line human summary (the exception message)
    detail TEXT,                      -- full traceback when an exception was caught
    context_json TEXT,               -- the data being processed (tags, ids) for triage
    resolved_at TEXT                 -- set when acknowledged/fixed; NULL = open
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON runtime_incidents(resolved_at, at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_component ON runtime_incidents(component, at DESC);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_incidents_schema(conn: sqlite3.Connection) -> None:
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_incidents'"
    ).fetchone()
    if not have:
        conn.executescript(INCIDENTS_DDL)
        conn.commit()


def record_incident(
    component: str,
    error: Any,
    *,
    context: Optional[dict] = None,
    severity: str = "error",
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record a fail-soft failure. `error` may be an Exception (traceback is
    captured) or a string. NEVER raises."""
    try:
        if isinstance(error, BaseException):
            summary = f"{type(error).__name__}: {error}"
            detail = "".join(_tb.format_exception(type(error), error, error.__traceback__))
        else:
            summary = str(error)
            detail = "".join(_tb.format_stack()[:-1])  # caller stack, sans this frame
        ctx = None
        if context:
            try:
                ctx = json.dumps(context, default=str, ensure_ascii=False)[:4000]
            except (TypeError, ValueError):
                ctx = json.dumps({"repr": repr(context)[:2000]})

        own = conn is None
        if own:
            import db as _db
            conn = _db.get_connection()
        try:
            ensure_incidents_schema(conn)
            conn.execute(
                "INSERT INTO runtime_incidents (at, component, severity, summary, "
                "detail, context_json) VALUES (?, ?, ?, ?, ?, ?)",
                (_utcnow(), component, severity if severity in ("warn", "error") else "error",
                 summary[:500], detail, ctx),
            )
            conn.commit()
        finally:
            if own:
                conn.close()
    except Exception:  # observability must never crash its host
        log.exception("record_incident failed for component=%s", component)


def open_incidents(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    """Unresolved incidents, newest first (for the Observatory + health check)."""
    ensure_incidents_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT incident_id, at, component, severity, summary, detail, context_json "
        "FROM runtime_incidents WHERE resolved_at IS NULL ORDER BY at DESC LIMIT ?",
        (limit,),
    ).fetchall()]


def count_open_since(conn: sqlite3.Connection, *, hours: int = 24) -> int:
    ensure_incidents_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM runtime_incidents WHERE resolved_at IS NULL "
        "AND severity = 'error' AND at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
        (f"-{int(hours)} hours",),
    ).fetchone()
    return int(row[0]) if row else 0
