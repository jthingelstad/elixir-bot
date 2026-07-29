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
    """Open through the sole initializer so schema evolution cannot be bypassed."""
    import db

    return db.get_connection(path or db_path())


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def payload_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ensure_display_name_column(conn) -> None:
    """Compatibility assertion; db.schema owns the column migration."""
    from db.schema import require_columns

    require_columns(conn, "players", {"display_name"})


def refresh_display_name(conn, player_tag: str, name: str | None = None) -> str:
    """Recompute + persist players.display_name — the single materialized,
    LLM-safe name (normalize-at-source). Call after a name change, nickname
    generation, or leader override. Reads current state when ``name`` is None."""
    from storage._formatting import compute_display_name

    _ensure_display_name_column(conn)
    display = compute_display_name(conn, player_tag, name)
    conn.execute(
        "UPDATE players SET display_name = ? WHERE player_tag = ?",
        (display, player_tag),
    )
    return display


def ensure_player(
    conn, player_tag: str, name: str | None = None, observed_at: str | None = None
) -> None:
    """§7: any tag the engine observes is a player — upsert its identity row,
    materializing the injection-safe players.display_name in the same write
    (normalize-at-source: nothing downstream ever sees the raw name).

    Roster diffs maintain members' rows; this covers deep-polled tags that
    never appeared in a roster diff (departed members, replay ordering).
    """
    from storage._formatting import compute_display_name

    at = observed_at or utcnow()
    _ensure_display_name_column(conn)
    # None when no incoming name → COALESCE keeps the existing display_name.
    display = compute_display_name(conn, player_tag, name) if name else None
    conn.execute(
        """INSERT INTO players (player_tag, current_name, display_name, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(player_tag) DO UPDATE SET
             last_seen_at = excluded.last_seen_at,
             current_name = COALESCE(excluded.current_name, players.current_name),
             display_name = COALESCE(excluded.display_name, players.display_name)""",
        (player_tag, name, display, at, at),
    )


def ensure_clan(
    conn,
    clan_tag: str,
    name: str | None = None,
    observed_at: str | None = None,
    is_home: bool = False,
) -> None:
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


def stream_head(conn, table: str) -> int:
    """Current insertion head for a stream (runtime.md §6: init-at-head)."""
    col = "rowid" if table == "battle_events" else "event_id"
    row = conn.execute(f"SELECT COALESCE(MAX({col}), 0) FROM {table}").fetchone()
    return int(row[0])
