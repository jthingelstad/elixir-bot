"""Engine-tick history for the /ticks page.

Persisted since 2026-07-04 (Jamie: "let's for sure persist tick history"):
every tick's full counter dict lands in `tick_history` (30-day retention,
self-pruning), with the in-memory ring kept as a zero-IO fast path. Before
this, history died with the process (6 restarts on go-live night alone).
"""

from __future__ import annotations

import collections
import json
from datetime import datetime, timezone

_TICKS: collections.deque = collections.deque(maxlen=288)  # ~48h at 10-min ticks

_DDL = """CREATE TABLE IF NOT EXISTS tick_history (
    tick_id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    counters_json TEXT NOT NULL
)"""
_RETENTION_DAYS = 30


def record_tick(counters: dict) -> None:
    entry = dict(counters or {})
    entry.setdefault(
        "recorded_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    _TICKS.appendleft(entry)
    try:
        import db

        conn = db.get_connection()
        try:
            conn.execute(_DDL)
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
    except Exception:  # persistence must never fail the tick (guard mirrors caller)
        pass


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
        pass
    return [dict(t) for t in list(_TICKS)[:limit]]
