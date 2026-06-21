"""War backfill — replay archived war raw_api_payloads through the ingest path.

Mirrors event_core.backfill: idempotent across runs via an ingest cursor
(high-water on payload_id) living in the projections DB.

Order matters: the riverracelog (`clan_war_log`) is backfilled BEFORE
`currentriverrace`, so live-state season inference can read prior logged races
out of the event store (matching legacy, which infers live seasonId from the
war_races table the log populated).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from event_core import config, db
from event_core.ingest.war import (
    ingest_clan_war_log_payload,
    ingest_currentriverrace_payload,
)


def _legacy_conn(legacy_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(legacy_path or config.LEGACY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _cursor(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT last_payload_id FROM ingest_cursor WHERE source=?", (source,)
    ).fetchone()
    return row["last_payload_id"] if row else 0


def _save_cursor(conn: sqlite3.Connection, source: str, payload_id: int) -> None:
    conn.execute(
        "INSERT INTO ingest_cursor(source,last_payload_id,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET last_payload_id=excluded.last_payload_id, "
        "updated_at=excluded.updated_at",
        (source, payload_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def backfill_war_log(app, legacy_path: str | None = None, projections_path: str | None = None) -> dict:
    """Replay archived clan_war_log payloads (finalized standings), once each."""
    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(projections_path or config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "clan_war_log")
        rows = legacy.execute(
            "SELECT payload_id, entity_key, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='clan_war_log' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()
        changed = 0
        max_id = start_id
        for r in rows:
            payload = json.loads(r["payload_json"])
            changed += ingest_clan_war_log_payload(
                app, r["entity_key"], payload, r["fetched_at"]
            )
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "clan_war_log", max_id)
        return {"payloads": len(rows), "races_changed": changed}
    finally:
        legacy.close()
        cursor_conn.close()


def backfill_currentriverrace(app, legacy_path: str | None = None, projections_path: str | None = None) -> dict:
    """Replay archived currentriverrace payloads (live war state), once each."""
    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(projections_path or config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "currentriverrace")
        rows = legacy.execute(
            "SELECT payload_id, entity_key, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='currentriverrace' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()
        changed = 0
        max_id = start_id
        for r in rows:
            payload = json.loads(r["payload_json"])
            if ingest_currentriverrace_payload(
                app, r["entity_key"], payload, r["fetched_at"]
            ):
                changed += 1
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "currentriverrace", max_id)
        return {"payloads": len(rows), "states_changed": changed}
    finally:
        legacy.close()
        cursor_conn.close()
