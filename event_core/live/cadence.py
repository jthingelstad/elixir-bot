"""Cadence reflections — the small scheduled set over projections.

The deliberately-small time-triggered remainder of awareness (the reactive policy
handles the rest). Pure projection queries; returns structured facts the agent
composes from. No copy here.
"""
from __future__ import annotations

import sqlite3


def clan_activity_24h(conn: sqlite3.Connection, since_iso: str) -> dict:
    """Battles + detections in the window [since_iso, now]. `since_iso` is a UTC
    timestamp string comparable to battle_time / detection occurred_at."""
    battles = conn.execute(
        "SELECT COUNT(*) FROM battle_telemetry WHERE battle_time >= ?", (since_iso,)
    ).fetchone()[0]
    active_players = conn.execute(
        "SELECT COUNT(DISTINCT player_tag) FROM battle_telemetry WHERE battle_time >= ?",
        (since_iso,),
    ).fetchone()[0]
    dets = conn.execute(
        "SELECT detection_type, COUNT(*) FROM detections WHERE occurred_at >= ? "
        "GROUP BY detection_type ORDER BY 2 DESC",
        (since_iso,),
    ).fetchall()
    return {
        "window_start": since_iso,
        "battles": battles,
        "active_players": active_players,
        "detections": {r[0]: r[1] for r in dets},
    }
