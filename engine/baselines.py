"""state_baselines read/write — schema.md §4, architecture.md §8.

The single diff substrate: one row per (entity_kind, entity_tag, aspect)
holding the last known state. Emitters diff against it; it is NOT a read
model (reads go to L6).
"""

from __future__ import annotations

import json
import sqlite3

from engine.db import payload_hash


def get_baseline(conn, entity_kind: str, entity_tag: str, aspect: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM state_baselines WHERE entity_kind = ? AND entity_tag = ? AND aspect = ?",
        (entity_kind, entity_tag, aspect),
    ).fetchone()


def set_baseline(
    conn,
    entity_kind: str,
    entity_tag: str,
    aspect: str,
    payload: dict,
    observed_at: str,
) -> None:
    """Upsert the baseline, rolling the previous observed_at into
    prev_observed_at — the (prev, now] window for timing honesty (§8)."""
    conn.execute(
        """INSERT INTO state_baselines
               (entity_kind, entity_tag, aspect, payload_json, payload_hash,
                observed_at, prev_observed_at)
           VALUES (?, ?, ?, ?, ?, ?, NULL)
           ON CONFLICT(entity_kind, entity_tag, aspect) DO UPDATE SET
               prev_observed_at = state_baselines.observed_at,
               payload_json = excluded.payload_json,
               payload_hash = excluded.payload_hash,
               observed_at = excluded.observed_at""",
        (
            entity_kind,
            entity_tag,
            aspect,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            payload_hash(payload),
            observed_at,
        ),
    )


def payload_changed(conn, entity_kind: str, entity_tag: str, aspect: str, payload: dict) -> bool:
    """Cheap no-change check: hash compare against the stored baseline."""
    row = get_baseline(conn, entity_kind, entity_tag, aspect)
    if row is None:
        return True
    return row["payload_hash"] != payload_hash(payload)


def baseline_payload(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return json.loads(row["payload_json"])
