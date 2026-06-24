"""Durable labels for live Clash Royale game-mode surfaces."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from db import _json_or_none, _rowdicts, _utcnow, managed_connection


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
) -> None:
    now = _utcnow()
    raw_json = _json_or_none(raw)
    conn.execute(
        """
        INSERT INTO game_mode_contexts (
            context_type, source_key, display_name, game_mode_id, game_mode_name,
            event_tag, leaderboard_id, source_endpoint, first_seen_at, last_seen_at, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(context_type, source_key) DO UPDATE SET
            display_name = excluded.display_name,
            game_mode_id = excluded.game_mode_id,
            game_mode_name = excluded.game_mode_name,
            event_tag = excluded.event_tag,
            leaderboard_id = excluded.leaderboard_id,
            source_endpoint = excluded.source_endpoint,
            last_seen_at = excluded.last_seen_at,
            raw_json = excluded.raw_json
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
        ),
    )


@managed_connection
def upsert_game_mode_contexts_from_events(payload, conn: Optional[sqlite3.Connection] = None) -> int:
    items = payload if isinstance(payload, list) else (payload or {}).get("items") if isinstance(payload, dict) else []
    count = 0
    for index, event in enumerate(items or []):
        if not isinstance(event, dict):
            continue
        event_tag = event.get("eventTag")
        title = event.get("title") or event.get("name")
        game_mode = event.get("gameMode") if isinstance(event.get("gameMode"), dict) else {}
        source_key = _context_source_key(event_tag or title, f"event:{index}")
        _upsert_context(
            conn,
            context_type="event",
            source_key=source_key,
            display_name=title,
            game_mode_id=game_mode.get("id") if game_mode else None,
            game_mode_name=game_mode.get("name") if game_mode else None,
            event_tag=event_tag,
            source_endpoint="events",
            raw=event,
        )
        count += 1
    return count


@managed_connection
def upsert_game_mode_contexts_from_leaderboards(payload, conn: Optional[sqlite3.Connection] = None) -> int:
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
) -> list[dict]:
    where = []
    params = []
    if context_type:
        where.append("context_type = ?")
        params.append(context_type)
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
        except (TypeError, ValueError, json.JSONDecodeError):
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
