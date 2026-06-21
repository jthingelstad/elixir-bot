"""Backfill — replay archived raw_api_payloads through the live ingest path.

The fixture. Backfill is just the ingest path fed historical input, so it
exercises exactly the code live polling will use. The raw archive spans ~2 weeks
(high fidelity); deeper history would come from derived tables (later slices).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from event_core import config, db
from event_core.ingest.profile import ingest_player_payload
from event_core.ingest.roster import ingest_clan_payload


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


def backfill_players(app, legacy_path: str | None = None) -> dict:
    """Replay archived /players payloads in observation order, exactly once.

    Idempotent across runs via an ingest cursor (high-water on payload_id). A clean
    rebuild resets the cursor (it lives in elixir-v5.db, which the build wipes).
    """
    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "player")
        rows = legacy.execute(
            "SELECT payload_id, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='player' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()

        changed = 0
        max_id = start_id
        for r in rows:
            payload = json.loads(r["payload_json"])
            if ingest_player_payload(app, payload, r["fetched_at"]):
                changed += 1
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "player", max_id)
        return {"payloads": len(rows), "events_emitted": changed}
    finally:
        legacy.close()
        cursor_conn.close()


def backfill_clans(app, legacy_path: str | None = None) -> dict:
    """Replay archived /clans payloads (rosters) in observation order, once each."""
    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "clan")
        rows = legacy.execute(
            "SELECT payload_id, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='clan' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()

        member_changes = 0
        max_id = start_id
        for r in rows:
            payload = json.loads(r["payload_json"])
            member_changes += ingest_clan_payload(app, payload, r["fetched_at"])
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "clan", max_id)
        return {"payloads": len(rows), "member_observations_changed": member_changes}
    finally:
        legacy.close()
        cursor_conn.close()
