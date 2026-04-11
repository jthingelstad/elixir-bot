"""db — SQLite storage layer for Elixir bot.

The current schema centers on stable member identity, Discord identity, raw API
payloads, war history, battle facts, and conversational memory.

The module exposes Elixir's identity, memory, roster, battle, and war query layer.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("elixir_db")

PACKAGE_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

DB_PATH = os.getenv("ELIXIR_DB_PATH", os.path.join(PROJECT_ROOT, "elixir.db"))
CHICAGO_TZ = ZoneInfo("America/Chicago")

SNAPSHOT_RETENTION_DAYS = 90
PLAYER_PROFILE_RETENTION_DAYS = 45
CARD_COLLECTION_RETENTION_DAYS = 30
WAR_RETENTION_DAYS = 180
RAW_PAYLOAD_RETENTION_DAYS = 30
SIGNAL_OUTCOME_RETENTION_DAYS = 90
CONVERSATION_RETENTION_DAYS = 30
TOURNAMENT_RETENTION_DAYS = 365
CONVERSATION_MAX_PER_SCOPE = 20

_V2_SCHEMA_CORE = {
    "members": {
        "member_id",
        "player_tag",
        "current_name",
        "status",
        "first_seen_at",
        "last_seen_at",
    },
    "discord_users": {
        "discord_user_id",
        "username",
        "global_name",
        "display_name",
        "first_seen_at",
        "last_seen_at",
    },
    "discord_links": {
        "discord_link_id",
        "discord_user_id",
        "member_id",
        "linked_at",
        "source",
        "confidence",
        "is_primary",
    },
    "discord_channels": {
        "channel_id",
        "channel_name",
        "channel_kind",
        "first_seen_at",
        "last_seen_at",
    },
    "conversation_threads": {
        "thread_id",
        "scope_type",
        "scope_key",
        "channel_id",
        "discord_user_id",
        "member_id",
        "created_at",
        "last_active_at",
    },
    "messages": {
        "message_id",
        "thread_id",
        "channel_id",
        "discord_user_id",
        "member_id",
        "author_type",
        "workflow",
        "event_type",
        "content",
        "summary",
        "created_at",
        "raw_json",
    },
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def chicago_today(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(CHICAGO_TZ).date().isoformat()


def chicago_date_for_utc_timestamp(value: Optional[str]) -> Optional[str]:
    dt = _parse_iso_time(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CHICAGO_TZ).date().isoformat()


def chicago_date_for_cr_timestamp(value: Optional[str]) -> Optional[str]:
    dt = _parse_cr_time(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CHICAGO_TZ).date().isoformat()


def chicago_day_bounds_utc(metric_date: str) -> tuple[str, str]:
    local_start = datetime.strptime(metric_date, "%Y-%m-%d").replace(tzinfo=CHICAGO_TZ)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = (local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return (
        utc_start.strftime("%Y-%m-%dT%H:%M:%S"),
        utc_end.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _canon_tag(tag: Optional[str]) -> str:
    tag = (tag or "").strip().upper()
    if not tag:
        return ""
    return tag if tag.startswith("#") else f"#{tag}"


def _tag_key(tag: Optional[str]) -> str:
    return _canon_tag(tag).lstrip("#")


def _rowdicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _hash_payload(payload) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _parse_cr_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        clean = value.split(".")[0]
        return datetime.strptime(clean, "%Y%m%dT%H%M%S")
    except (ValueError, TypeError):
        return None


def _parse_iso_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def _normalize_date_string(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    if "T" in value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            pass
    if _parse_iso_time(value):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"invalid date value: {value}") from exc


def _normalize_scope(scope: str) -> tuple[str, str]:
    if not scope:
        return "generic", "generic"
    if ":" in scope:
        scope_type, scope_key = scope.split(":", 1)
        return scope_type, scope_key
    return "generic", scope


def _json_or_none(data) -> Optional[str]:
    if data is None:
        return None
    return json.dumps(data, default=str, ensure_ascii=False)


def _build_form_label(wins: int, losses: int, sample_size: int) -> str:
    if sample_size == 0:
        return "inactive"
    win_rate = wins / sample_size
    if sample_size >= 5 and wins >= sample_size - 2:
        return "hot"
    if win_rate >= 0.6:
        return "strong"
    if win_rate <= 0.3 and losses >= 4:
        return "cold"
    if losses > wins:
        return "slumping"
    return "mixed"


def _build_form_summary(wins: int, losses: int, draws: int, sample_size: int, label: str) -> str:
    if sample_size == 0:
        return "No recent battles recorded."
    return f"{wins}-{losses}-{draws} over the last {sample_size} battles ({label})."


def _card_level(card: dict) -> Optional[int]:
    level = card.get("level")
    if not isinstance(level, int):
        return None
    max_level = card.get("maxLevel")
    if not isinstance(max_level, int) or max_level <= 0 or max_level > 16:
        return level
    return level + max(0, 16 - max_level)


def _aggregate_card_usage_from_battle_facts(rows: Iterable[sqlite3.Row]) -> tuple[int, list[dict]]:
    counts = {}
    icons = {}
    total = 0
    for row in rows:
        cards = json.loads(row["deck_json"] or "[]")
        if len(cards) != 8:
            continue
        total += 1
        for card in cards:
            name = card.get("name")
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
            icon = (card.get("iconUrls") or {}).get("medium")
            if icon:
                icons[name] = icon
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
    summary = [
        {
            "name": name,
            "icon_url": icons.get(name, ""),
            "usage_pct": round(count / total * 100) if total else 0,
        }
        for name, count in ordered
    ]
    return total, summary


def _ensure_member(conn: sqlite3.Connection, tag: str, name: Optional[str] = None, status: Optional[str] = "active") -> int:
    tag = _canon_tag(tag)
    if not tag:
        raise ValueError("member tag is required")
    row = conn.execute(
        "SELECT member_id FROM members WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    now = _utcnow()
    if row:
        conn.execute(
            "UPDATE members SET current_name = COALESCE(?, current_name), status = COALESCE(?, status), last_seen_at = ? WHERE member_id = ?",
            (name, status, now, row["member_id"]),
        )
        if name:
            conn.execute(
                "INSERT INTO member_aliases (member_id, alias, source, observed_at) VALUES (?, ?, 'clan_api', ?) ON CONFLICT(member_id, alias) DO UPDATE SET observed_at = excluded.observed_at",
                (row["member_id"], name, now),
            )
        return row["member_id"]

    cur = conn.execute(
        "INSERT INTO members (player_tag, current_name, status, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
        (tag, name, status or "observed", now, now),
    )
    member_id = cur.lastrowid
    if name:
        conn.execute(
            "INSERT INTO member_aliases (member_id, alias, source, observed_at) VALUES (?, ?, 'clan_api', ?)",
            (member_id, name, now),
        )
    return member_id


def _ensure_thread(conn: sqlite3.Connection, scope: str, channel_id=None, discord_user_id=None, member_id=None) -> int:
    scope_type, scope_key = _normalize_scope(scope)
    row = conn.execute(
        "SELECT thread_id FROM conversation_threads WHERE scope_type = ? AND scope_key = ?",
        (scope_type, scope_key),
    ).fetchone()
    now = _utcnow()
    if row:
        conn.execute(
            "UPDATE conversation_threads SET channel_id = COALESCE(?, channel_id), discord_user_id = COALESCE(?, discord_user_id), member_id = COALESCE(?, member_id), last_active_at = ? WHERE thread_id = ?",
            (channel_id, discord_user_id, member_id, now, row["thread_id"]),
        )
        return row["thread_id"]

    cur = conn.execute(
        "INSERT INTO conversation_threads (scope_type, scope_key, channel_id, discord_user_id, member_id, created_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (scope_type, scope_key, channel_id, discord_user_id, member_id, now, now),
    )
    return cur.lastrowid


def _get_current_membership(conn: sqlite3.Connection, member_id: int):
    return conn.execute(
        "SELECT membership_id, joined_at, join_source FROM clan_memberships "
        "WHERE member_id = ? AND left_at IS NULL "
        "ORDER BY CASE join_source "
        "WHEN 'manual_record' THEN 1 "
        "WHEN 'observed_join' THEN 2 "
        "WHEN 'clan_api_snapshot' THEN 3 "
        "WHEN 'backfill' THEN 4 "
        "WHEN 'bootstrap_seed' THEN 5 "
        "ELSE 99 END, joined_at DESC, membership_id DESC LIMIT 1",
        (member_id,),
    ).fetchone()


def _trusted_current_joined_at(conn: sqlite3.Connection, member_id: int) -> Optional[str]:
    membership = conn.execute(
        "SELECT joined_at FROM clan_memberships "
        "WHERE member_id = ? AND left_at IS NULL AND join_source IN ('manual_record', 'observed_join', 'clan_api_snapshot') "
        "ORDER BY CASE join_source "
        "WHEN 'manual_record' THEN 1 "
        "WHEN 'observed_join' THEN 2 "
        "WHEN 'clan_api_snapshot' THEN 3 "
        "ELSE 99 END, joined_at DESC, membership_id DESC LIMIT 1",
        (member_id,),
    ).fetchone()
    return membership["joined_at"] if membership else None


def _current_joined_at(conn: sqlite3.Connection, member_id: int) -> Optional[str]:
    meta = conn.execute(
        "SELECT joined_at FROM member_metadata WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    if meta and meta["joined_at"]:
        return meta["joined_at"]
    return None


_MEMBER_METADATA_COLUMNS = frozenset({
    "joined_at", "birth_month", "birth_day", "profile_url", "poap_address", "note",
    "generated_bio", "generated_highlight", "generated_profile_updated_at",
    "cr_account_age_days", "cr_account_age_years", "cr_account_age_updated_at",
    "years_played", "years_played_updated_at",
    "games_per_day", "games_per_day_updated_at",
    "cr_games_per_day", "cr_games_per_day_window_days", "cr_games_per_day_updated_at",
})


def _upsert_member_metadata(conn: sqlite3.Connection, member_id: int, **fields) -> None:
    bad = set(fields) - _MEMBER_METADATA_COLUMNS
    if bad:
        raise ValueError(f"Invalid member_metadata columns: {bad}")
    row = conn.execute("SELECT member_id FROM member_metadata WHERE member_id = ?", (member_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO member_metadata (member_id) VALUES (?)", (member_id,))
    updates = []
    values = []
    for key, value in fields.items():
        updates.append(f"{key} = ?")
        values.append(value)
    if updates:
        values.append(member_id)
        conn.execute(f"UPDATE member_metadata SET {', '.join(updates)} WHERE member_id = ?", values)


def _parse_optional_int(value: Optional[str], *, field_name: str, minimum: int, maximum: int) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not (minimum <= parsed <= maximum):
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


def _member_reference_fields(conn: sqlite3.Connection, member_id: int, item: dict) -> dict:
    tag = item.get("player_tag") or item.get("tag")
    if not tag:
        row = conn.execute(
            "SELECT player_tag FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        tag = row["player_tag"] if row else None
    if not tag:
        return item
    item["member_ref"] = format_member_reference(tag, conn=conn)
    return item


def _ensure_channel(conn: sqlite3.Connection, channel_id, channel_name=None, channel_kind=None) -> None:
    if channel_id is None:
        return
    channel_id = str(channel_id)
    now = _utcnow()
    row = conn.execute("SELECT channel_id FROM discord_channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE discord_channels SET channel_name = COALESCE(?, channel_name), channel_kind = COALESCE(?, channel_kind), last_seen_at = ? WHERE channel_id = ?",
            (channel_name, channel_kind, now, channel_id),
        )
    else:
        conn.execute(
            "INSERT INTO discord_channels (channel_id, channel_name, channel_kind, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (channel_id, channel_name, channel_kind, now, now),
        )


def _store_raw_payload(conn: sqlite3.Connection, endpoint: str, entity_key: str, payload) -> None:
    payload_json = _json_or_none(payload)
    if payload_json is None:
        return
    payload_hash = _hash_payload(payload_json)
    existing = conn.execute(
        "SELECT payload_id FROM raw_api_payloads WHERE endpoint = ? AND entity_key = ? AND payload_hash = ?",
        (endpoint, entity_key, payload_hash),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO raw_api_payloads (endpoint, entity_key, fetched_at, payload_hash, payload_json) VALUES (?, ?, ?, ?, ?)",
        (endpoint, entity_key, _utcnow(), payload_hash, payload_json),
    )


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _schema_is_compatible(conn: sqlite3.Connection) -> bool:
    tables = _existing_tables(conn)
    if not tables:
        return True

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(_MIGRATIONS):
        return False

    for table_name, expected_columns in _V2_SCHEMA_CORE.items():
        if table_name not in tables:
            return False
        if not expected_columns.issubset(_table_columns(conn, table_name)):
            return False
    return True


def _legacy_backup_path(path: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{path}.legacy-v2-backup-{stamp}"


from db._migrations import _MIGRATIONS, _run_migrations  # noqa: E402


def _enable_sqlite_vec(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:
        raise RuntimeError(
            "sqlite-vec is required but not installed. Run `venv/bin/python -m pip install -r requirements.txt`."
        ) from exc

    try:
        conn.enable_load_extension(True)
    except Exception:
        log.debug("sqlite enable_load_extension not available, sqlite_vec.load() may still work")

    try:
        sqlite_vec.load(conn)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS clan_memory_vec USING vec0(memory_id INTEGER PRIMARY KEY, embedding float[1536])"
        )
        if "clan_memory_index_status" in _existing_tables(conn):
            conn.execute(
                "UPDATE clan_memory_index_status SET value = '1' WHERE key = 'sqlite_vec_enabled'"
            )
            conn.commit()
    except Exception as exc:
        raise RuntimeError(f"sqlite-vec is required but failed to load: {exc}") from exc
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = os.fspath(db_path or DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _enable_sqlite_vec(conn)
    if path != ":memory:" and not _schema_is_compatible(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = sorted(_existing_tables(conn))
        backup_path = _legacy_backup_path(path)
        conn.close()
        os.replace(path, backup_path)
        log.warning(
            "Detected incompatible database schema at %s (user_version=%s, tables=%s); moved it to %s and rebuilding baseline schema",
            path,
            version,
            ", ".join(tables) or "<none>",
            backup_path,
        )
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _enable_sqlite_vec(conn)
    _run_migrations(conn)
    _enable_sqlite_vec(conn)
    return conn


def managed_connection(fn: Callable) -> Callable:
    """Decorator that manages the conn=None lifecycle pattern.

    If the caller passes conn=None (the default), a new connection is opened
    and closed after the call. If a connection is provided, it is passed
    through and left open for the caller to manage.
    """
    @functools.wraps(fn)
    def wrapper(*args, conn=None, **kwargs):
        close = conn is None
        conn = conn or get_connection()
        try:
            return fn(*args, conn=conn, **kwargs)
        finally:
            if close:
                conn.close()
    return wrapper


# Allow storage submodules to import db's internal helpers during package init.
def __export_public(module):
    names = getattr(module, "__all__", None) or [
        name for name in vars(module) if not name.startswith("__")
    ]
    for name in names:
        globals()[name] = getattr(module, name)
    return names


from storage import identity as _identity_module
from storage import war as _war_module
from storage import roster as _roster_module
from storage import player as _player_module
from storage import trends as _trends_module
from storage import messages as _messages_module
from storage import metadata as _metadata_module
from storage import tournament as _tournament_module
from storage import card_catalog as _card_catalog_module

__all__ = [name for name in globals() if not name.startswith("__")]
for _module in (
    _identity_module,
    _war_module,
    _roster_module,
    _player_module,
    _trends_module,
    _messages_module,
    _metadata_module,
    _tournament_module,
    _card_catalog_module,
):
    __export_public(_module)

__all__ = [name for name in globals() if not name.startswith("__")]
