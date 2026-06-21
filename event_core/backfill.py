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
from event_core.ingest.battles import (
    BATTLE_COLUMNS,
    BATTLE_TELEMETRY_DDL,
    extract_battles,
)
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


def backfill_collections(app, legacy_path: str | None = None) -> dict:
    """Replay /players payloads into PlayerCollections (cards/badges/achievements)."""
    from event_core.ingest.collections import ingest_player_collections

    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "player_collections")
        rows = legacy.execute(
            "SELECT payload_id, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='player' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()
        emitted = {"cards": 0, "badges": 0, "achievements": 0}
        max_id = start_id
        for r in rows:
            changed = ingest_player_collections(app, json.loads(r["payload_json"]), r["fetched_at"])
            for k in emitted:
                emitted[k] += 1 if changed.get(k) else 0
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "player_collections", max_id)
        return {"payloads": len(rows), "events_emitted": emitted}
    finally:
        legacy.close()
        cursor_conn.close()


def backfill_clan_state(app, legacy_path: str | None = None) -> dict:
    """Replay /clans payloads into the Clan aggregate (clan-level state)."""
    from event_core.ingest.clan import ingest_clan_state

    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "clan_state")
        rows = legacy.execute(
            "SELECT payload_id, entity_key, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='clan' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()
        changed = 0
        max_id = start_id
        for r in rows:
            if ingest_clan_state(app, json.loads(r["payload_json"]), r["fetched_at"], r["entity_key"]):
                changed += 1
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "clan_state", max_id)
        return {"payloads": len(rows), "events_emitted": changed}
    finally:
        legacy.close()
        cursor_conn.close()


def backfill_clan_roster(app, legacy_path: str | None = None) -> dict:
    """Replay /clans memberLists -> Clan roster lifecycle (join/leave/role)."""
    from event_core.domain.player import canon_tag

    legacy = _legacy_conn(legacy_path)
    cursor_conn = db.connect(config.PROJECTIONS_DB)
    try:
        start_id = _cursor(cursor_conn, "clan_roster")
        rows = legacy.execute(
            "SELECT payload_id, entity_key, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='clan' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()
        changes = 0
        max_id = start_id
        for r in rows:
            payload = json.loads(r["payload_json"])
            roster = {
                canon_tag(m["tag"]): (m.get("role") or "member")
                for m in (payload.get("memberList") or [])
                if m.get("tag")
            }
            clan_tag = payload.get("tag") or r["entity_key"]
            changes += app.observe_clan_roster(clan_tag, roster, r["fetched_at"])
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(cursor_conn, "clan_roster", max_id)
        return {"payloads": len(rows), "lifecycle_events": changes}
    finally:
        legacy.close()
        cursor_conn.close()


def backfill_battles(legacy_path: str | None = None) -> dict:
    """Replay archived battlelogs into the battle_telemetry table (tier 1).

    Direct to elixir-v5.db (not the event store). Idempotent twice over: the
    ingest cursor skips processed payloads, and INSERT OR IGNORE dedups on the
    battle identity key.
    """
    legacy = _legacy_conn(legacy_path)
    proj = db.connect(config.PROJECTIONS_DB)
    proj.execute(BATTLE_TELEMETRY_DDL)
    proj.commit()
    try:
        start_id = _cursor(proj, "player_battlelog")
        rows = legacy.execute(
            "SELECT payload_id, entity_key, fetched_at, payload_json FROM raw_api_payloads "
            "WHERE endpoint='player_battlelog' AND payload_id > ? ORDER BY fetched_at ASC, payload_id ASC",
            (start_id,),
        ).fetchall()

        inserted = 0
        max_id = start_id
        placeholders = ",".join("?" for _ in BATTLE_COLUMNS) + ",?"  # +observed_at
        for r in rows:
            battles = extract_battles(r["entity_key"], json.loads(r["payload_json"]))
            for bt in battles:
                cur = proj.execute(
                    f"INSERT OR IGNORE INTO battle_telemetry({','.join(BATTLE_COLUMNS)},observed_at) "
                    f"VALUES({placeholders})",
                    [bt[c] for c in BATTLE_COLUMNS] + [r["fetched_at"]],
                )
                inserted += cur.rowcount
            max_id = max(max_id, r["payload_id"])
        if max_id > start_id:
            _save_cursor(proj, "player_battlelog", max_id)
        proj.commit()
        return {"payloads": len(rows), "battles_inserted": inserted}
    finally:
        legacy.close()
        proj.close()
