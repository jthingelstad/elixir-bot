"""Read-only agent tools over the v5 projection DB.

Each takes a sqlite3 connection to elixir-v5.db. `scope` gates leadership data:
a public composition path passes scope='public' and cannot see leadership rows.
"""
from __future__ import annotations

import sqlite3

from event_core.domain.player import canon_tag


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_player_current(conn: sqlite3.Connection, player_tag: str) -> dict:
    """Current profile + roster state for a player (merged projections)."""
    tag = canon_tag(player_tag)
    prof = conn.execute(
        "SELECT * FROM player_current_profile WHERE player_tag=?", (tag,)
    ).fetchone()
    roster = conn.execute(
        "SELECT * FROM member_current_state_proj WHERE player_tag=?", (tag,)
    ).fetchone()
    return {"profile": dict(prof) if prof else None, "roster": dict(roster) if roster else None}


def get_player_battles(
    conn: sqlite3.Connection, player_tag: str, limit: int = 20, mode: str | None = None
) -> list[dict]:
    """Recent battle detail (opponents, decks-by-id, outcome, mode)."""
    tag = canon_tag(player_tag)
    sql = "SELECT * FROM battle_telemetry WHERE player_tag=?"
    params: list = [tag]
    if mode:
        sql += " AND mode_group=?"
        params.append(mode)
    sql += " ORDER BY battle_time DESC LIMIT ?"
    params.append(limit)
    return _rows(conn, sql, tuple(params))


def get_player_detections(
    conn: sqlite3.Connection, player_tag: str, scope: str = "public", limit: int = 50
) -> list[dict]:
    """Detections about a player, newest first. Honors scope."""
    tag = canon_tag(player_tag)
    if scope == "public":
        sql = "SELECT * FROM detections WHERE subject_tag=? AND scope='public' ORDER BY occurred_at DESC LIMIT ?"
    else:
        sql = "SELECT * FROM detections WHERE subject_tag=? ORDER BY occurred_at DESC LIMIT ?"
    return _rows(conn, sql, (tag, limit))


def resolve_evidence(conn: sqlite3.Connection, detection: dict) -> list[dict]:
    """Drill a battle detection down to its supporting battles (§8.1).

    For a streak/push detection on `subject_tag` occurring at `occurred_at`,
    return that player's competitive battles up to and including that moment so
    the agent can name opponents and cards played.
    """
    tag = detection.get("subject_tag")
    occurred_at = detection.get("occurred_at")
    if not tag or not occurred_at:
        return []
    return _rows(
        conn,
        "SELECT battle_time, battle_type, mode_group, outcome, crowns_for, crowns_against, "
        "opponent_tag, trophy_change FROM battle_telemetry "
        "WHERE player_tag=? AND is_competitive=1 AND battle_time<=? "
        "ORDER BY battle_time DESC LIMIT 10",
        (canon_tag(tag), occurred_at),
    )
