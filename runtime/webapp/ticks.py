"""Engine-tick history for the /ticks page.

Persisted since 2026-07-04 (Jamie: "let's for sure persist tick history"):
every tick's full counter dict lands in `tick_history` (30-day retention,
self-pruning), with the in-memory ring kept as a zero-IO fast path. Before
this, history died with the process (6 restarts on go-live night alone).
"""

from __future__ import annotations

import collections
import json
import logging
from datetime import datetime, timezone

from storage.incidents import record_incident

_TICKS: collections.deque = collections.deque(maxlen=288)  # ~48h at 10-min ticks

_RETENTION_DAYS = 30
log = logging.getLogger("elixir.webapp.ticks")


def record_tick(counters: dict) -> None:
    entry = dict(counters or {})
    entry.setdefault(
        "recorded_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _TICKS.appendleft(entry)
    try:
        import db
        from db.schema import require_columns

        conn = db.get_connection()
        try:
            require_columns(conn, "tick_history", {"tick_id", "counters_json"})
            conn.execute(
                "INSERT INTO tick_history (recorded_at, counters_json) VALUES (?, ?)",
                (entry["recorded_at"], json.dumps(entry, default=str)),
            )
            # Self-pruning: cheap DELETE on every insert (144 rows/day).
            conn.execute(
                "DELETE FROM tick_history WHERE recorded_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                (f"-{_RETENTION_DAYS} days",),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # persistence must never fail the tick
        log.warning("tick-history persistence failed", exc_info=True)
        record_incident(
            "webapp.tick_history.persist",
            exc,
            context={"recorded_at": entry.get("recorded_at")},
            severity="warn",
        )


def recent_ticks(limit: int = 100) -> list[dict]:
    """Persisted history first (survives restarts); ring as fallback."""
    limit = max(1, int(limit))
    try:
        import db

        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT counters_json FROM tick_history ORDER BY tick_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                return [json.loads(r["counters_json"]) for r in rows]
        finally:
            conn.close()
    except Exception:
        log.debug("persisted tick history unavailable; using memory ring", exc_info=True)
    return [dict(t) for t in list(_TICKS)[:limit]]
