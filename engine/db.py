"""Engine DB access — one connection discipline for every engine module.

The engine is single-process, single-writer (runtime.md §2). Modules receive a
connection; they never open their own. Shared helpers re-export from the
existing db module so tag/time canonicalization stays identical across the
read layer and the engine.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

from db import _canon_tag as canon_tag  # noqa: F401  (shared canonicalization)
from db import chicago_today  # noqa: F401

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "elixir-v51.db")


def db_path() -> str:
    return os.getenv("ELIXIR_DB_PATH", _DEFAULT_DB)


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def payload_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def ensure_player(conn, player_tag: str, name: str | None = None,
                  observed_at: str | None = None) -> None:
    """§7: any tag the engine observes is a player — upsert its identity row.

    Roster diffs maintain members' rows; this covers deep-polled tags that
    never appeared in a roster diff (departed members, replay ordering).
    """
    at = observed_at or utcnow()
    conn.execute(
        """INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(player_tag) DO UPDATE SET
             last_seen_at = excluded.last_seen_at,
             current_name = COALESCE(excluded.current_name, players.current_name)""",
        (player_tag, name, at, at),
    )


def ensure_clan(conn, clan_tag: str, name: str | None = None,
                observed_at: str | None = None, is_home: bool = False) -> None:
    """clans-row upsert (clan_memberships FKs onto it). Live DBs carry
    migrated rows; a fresh DB (cold start, simulator) bootstraps here —
    without it the first roster diff dies on the membership FK and the
    unrolled baseline re-emits member_joined every tick (flood class)."""
    at = observed_at or utcnow()
    conn.execute(
        """INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(clan_tag) DO UPDATE SET
             last_seen_at = excluded.last_seen_at,
             name = COALESCE(excluded.name, clans.name)""",
        (clan_tag, name, at, at, 1 if is_home else 0),
    )


# ---------------------------------------------------------------- cursors

def cursor_get(conn, consumer_key: str, scope_key: str = "") -> int:
    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (consumer_key, scope_key),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def cursor_set(conn, consumer_key: str, value: int, scope_key: str = "") -> None:
    conn.execute(
        """INSERT INTO stream_cursors (consumer_key, scope_key, cursor_int, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(consumer_key, scope_key)
           DO UPDATE SET cursor_int = excluded.cursor_int, updated_at = excluded.updated_at""",
        (consumer_key, scope_key, value, utcnow()),
    )


def cursor_note_failure(
    conn,
    consumer_key: str,
    failed_cursor: int,
    error: BaseException | str,
    scope_key: str = "",
) -> int:
    """Track repeated failure of the same next stream row in cursor metadata.
    Returns the consecutive failure count for that row.
    """
    row = conn.execute(
        """SELECT cursor_int, metadata_json
           FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?""",
        (consumer_key, scope_key),
    ).fetchone()
    cursor_int = int(row["cursor_int"] or 0) if row else 0
    try:
        metadata = json.loads(row["metadata_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        metadata = {}
    poison = metadata.get("poison_event") if isinstance(metadata.get("poison_event"), dict) else {}
    if int(poison.get("cursor_int") or -1) != int(failed_cursor):
        poison = {"cursor_int": int(failed_cursor), "count": 0}
    poison["count"] = int(poison.get("count") or 0) + 1
    poison["last_error"] = repr(error)[:500]
    poison["last_failed_at"] = utcnow()
    metadata["poison_event"] = poison
    conn.execute(
        """INSERT INTO stream_cursors
              (consumer_key, scope_key, cursor_int, updated_at, metadata_json)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(consumer_key, scope_key) DO UPDATE SET
             metadata_json = excluded.metadata_json,
             updated_at = excluded.updated_at""",
        (consumer_key, scope_key, cursor_int, utcnow(), json.dumps(metadata)),
    )
    return int(poison["count"])


def cursor_clear_failure(conn, consumer_key: str, scope_key: str = "") -> None:
    row = conn.execute(
        "SELECT metadata_json FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (consumer_key, scope_key),
    ).fetchone()
    if not row:
        return
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not metadata.pop("poison_event", None):
        return
    conn.execute(
        """UPDATE stream_cursors
           SET metadata_json = ?, updated_at = ?
           WHERE consumer_key = ? AND scope_key = ?""",
        (json.dumps(metadata) if metadata else None, utcnow(), consumer_key, scope_key),
    )


def stream_head(conn, table: str) -> int:
    """Current insertion head for a stream (runtime.md §6: init-at-head)."""
    col = "rowid" if table == "battle_events" else "event_id"
    row = conn.execute(f"SELECT COALESCE(MAX({col}), 0) FROM {table}").fetchone()
    return int(row[0])
