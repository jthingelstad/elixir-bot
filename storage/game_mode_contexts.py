"""Durable labels for live Clash Royale game-mode surfaces."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from db import _json_or_none, _rowdicts, _utcnow, managed_connection
from db._event_snapshot import event_snapshot_items, event_source_key


def _context_source_key(value, fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _upsert_context(
    conn: sqlite3.Connection,
    *,
    context_type: str,
    source_key: str,
    display_name: str | None = None,
    game_mode_id: int | None = None,
    game_mode_name: str | None = None,
    event_tag: str | None = None,
    leaderboard_id: int | None = None,
    source_endpoint: str | None = None,
    raw=None,
    is_current: bool = False,
) -> None:
    now = _utcnow()
    raw_json = _json_or_none(raw)
    conn.execute(
        """
        INSERT INTO game_mode_contexts (
            context_type, source_key, display_name, game_mode_id, game_mode_name,
            event_tag, leaderboard_id, source_endpoint, first_seen_at, last_seen_at, raw_json, is_current
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(context_type, source_key) DO UPDATE SET
            display_name = excluded.display_name,
            game_mode_id = excluded.game_mode_id,
            game_mode_name = excluded.game_mode_name,
            event_tag = excluded.event_tag,
            leaderboard_id = excluded.leaderboard_id,
            source_endpoint = excluded.source_endpoint,
            last_seen_at = excluded.last_seen_at,
            raw_json = excluded.raw_json,
            is_current = excluded.is_current
        """,
        (
            context_type,
            source_key,
            display_name,
            game_mode_id,
            game_mode_name,
            event_tag,
            leaderboard_id,
            source_endpoint,
            now,
            now,
            raw_json,
            int(is_current),
        ),
    )


@managed_connection
def upsert_game_mode_contexts_from_events(
    payload, conn: Optional[sqlite3.Connection] = None
) -> int:
    items = event_snapshot_items(payload)
    if items is None:
        return 0
    # Keep the caller's transaction intact, but never commit a partial snapshot
    # if one context fails after the previous current set has been cleared.
    conn.execute("SAVEPOINT event_snapshot")
    try:
        conn.execute("UPDATE game_mode_contexts SET is_current = 0 WHERE context_type = 'event'")
        for event in items:
            game_mode = event.get("gameMode") or {}
            _upsert_context(
                conn,
                context_type="event",
                source_key=event_source_key(event),
                display_name=event.get("title") or event.get("name"),
                game_mode_id=game_mode.get("id"),
                game_mode_name=game_mode.get("name"),
                event_tag=event.get("eventTag"),
                source_endpoint="events",
                raw=event,
                is_current=True,
            )
    except Exception:
        conn.execute("ROLLBACK TO event_snapshot")
        conn.execute("RELEASE event_snapshot")
        raise
    conn.execute("RELEASE event_snapshot")
    return len(items)


@managed_connection
def upsert_game_mode_contexts_from_leaderboards(
    payload, conn: Optional[sqlite3.Connection] = None
) -> int:
    items = (payload or {}).get("items") if isinstance(payload, dict) else []
    count = 0
    for index, board in enumerate(items or []):
        if not isinstance(board, dict):
            continue
        leaderboard_id = board.get("id")
        source_key = _context_source_key(leaderboard_id, f"leaderboard:{index}")
        _upsert_context(
            conn,
            context_type="leaderboard",
            source_key=source_key,
            display_name=board.get("name"),
            leaderboard_id=leaderboard_id if isinstance(leaderboard_id, int) else None,
            source_endpoint="leaderboards",
            raw=board,
        )
        count += 1
    return count


@managed_connection
def list_game_mode_contexts(
    context_type: str | None = None,
    limit: int = 25,
    conn: Optional[sqlite3.Connection] = None,
    *,
    current_only: bool = False,
) -> list[dict]:
    where = []
    params = []
    if context_type:
        where.append("context_type = ?")
        params.append(context_type)
    if current_only:
        where.append("is_current = 1")
    sql = (
        "SELECT context_type, source_key, display_name, game_mode_id, game_mode_name, "
        "event_tag, leaderboard_id, source_endpoint, first_seen_at, last_seen_at, raw_json "
        "FROM game_mode_contexts"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen_at DESC, display_name COLLATE NOCASE LIMIT ?"
    params.append(max(1, min(int(limit or 25), 100)))
    contexts = _rowdicts(conn.execute(sql, tuple(params)).fetchall())
    for context in contexts:
        raw = {}
        try:
            raw_value = json.loads(context.pop("raw_json") or "{}")
            raw = raw_value if isinstance(raw_value, dict) else {}
        except TypeError, ValueError, json.JSONDecodeError:
            raw = {}
        if context.get("context_type") == "event":
            context["event_name"] = context.get("display_name")
            context["event_description"] = raw.get("description")
    return contexts


__all__ = [
    "list_game_mode_contexts",
    "upsert_game_mode_contexts_from_events",
    "upsert_game_mode_contexts_from_leaderboards",
]
