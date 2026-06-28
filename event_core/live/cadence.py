"""Cadence reflections — the small scheduled set over projections.

The deliberately-small time-triggered remainder of awareness (the reactive policy
handles the rest). Pure projection queries; returns structured facts the agent
composes from. No copy here.
"""
from __future__ import annotations

import sqlite3

from event_core.read.timestamps import cr_comparable_expr
from event_core.timeutil import cr_utc_timestamp


def clan_activity_24h(conn: sqlite3.Connection, since_iso: str) -> dict:
    """Battles + detections in the window [since_iso, now]."""
    since_cr = cr_utc_timestamp(since_iso) or since_iso
    battles = conn.execute(
        f"SELECT COUNT(*) FROM battle_telemetry WHERE {cr_comparable_expr('battle_time')} >= ?",
        (since_cr,),
    ).fetchone()[0]
    active_players = conn.execute(
        f"SELECT COUNT(DISTINCT player_tag) FROM battle_telemetry WHERE {cr_comparable_expr('battle_time')} >= ?",
        (since_cr,),
    ).fetchone()[0]
    dets = conn.execute(
        f"SELECT detection_type, COUNT(*) FROM detections WHERE {cr_comparable_expr('occurred_at')} >= ? "
        "GROUP BY detection_type ORDER BY 2 DESC",
        (since_cr,),
    ).fetchall()
    return {
        "window_start": since_iso,
        "battles": battles,
        "active_players": active_players,
        "detections": {r[0]: r[1] for r in dets},
    }
