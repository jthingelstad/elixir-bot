"""db — SQLite storage layer for Elixir bot.

The current schema centers on stable member identity, Discord identity, raw API
payloads, war history, battle facts, and conversational memory.

The module exposes Elixir's identity, memory, roster, battle, and war query layer.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("elixir_db")

PACKAGE_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

_DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "elixir-v5.db")


def _resolve_db_path() -> str:
    """The operational DB path, resolved from the environment at call time.

    Lazy resolution avoids the import-order trap: ELIXIR_DB_PATH is set by
    load_dotenv(), which can run AFTER `import db`. A frozen module constant
    would capture the pre-dotenv default. The post-consolidation default is
    elixir-v5.db (the single operational DB); elixir.db is retired.
    """
    return os.getenv("ELIXIR_DB_PATH", _DEFAULT_DB_PATH)


CHICAGO_TZ = ZoneInfo("America/Chicago")

# v5.1 retention (docs/v5.1/schema.md §1). Names kept where semantics carried.
RAW_PAYLOAD_RETENTION_DAYS = 14
BATTLE_EVENT_RETENTION_DAYS = 180
PLAYER_EVENT_RETENTION_DAYS = 180
CLAN_EVENT_RETENTION_DAYS = 365
WAR_RETENTION_DAYS = 365
CONVERSATION_RETENTION_DAYS = 30
TOURNAMENT_RETENTION_DAYS = 365
CONVERSATION_MAX_PER_SCOPE = 20

# The v5.1 spine — used only to REFUSE a wrong database, never to rebuild one
# (docs/v5.1/migration.md: the old truth is archived, not destroyed).
_V51_SCHEMA_CORE = {
    "players": {
        "player_tag",
        "current_name",
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
        "player_tag",
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
    "battle_events": {"dedup_key", "player_tag", "battle_time"},
    "recognition_ledger": {"recognition_key", "stream", "claimed_at"},
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


def _played_as(card: dict) -> Optional[str]:
    """Translate the deployment-encoded `evolutionLevel` on a battle-log or
    `currentDeck` card to a stable string: 'evo', 'hero', or None when the
    card was not played as an alternate mode. Not applicable to full-collection
    `cards[]` arrays where evolutionLevel means ownership, not deployment.
    """
    ev = card.get("evolutionLevel")
    if ev == 1:
        return "evo"
    if ev == 2:
        return "hero"
    return None


def _aggregate_card_usage_from_battle_facts(rows: Iterable[sqlite3.Row]) -> tuple[int, list[dict]]:
    counts: dict[tuple[str, Optional[str]], int] = {}
    icons: dict[str, str] = {}
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
            played_as = _played_as(card)
            counts[(name, played_as)] = counts.get((name, played_as), 0) + 1
            icon = (card.get("iconUrls") or {}).get("medium")
            if icon and name not in icons:
                icons[name] = icon
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
    summary = []
    for (name, played_as), count in ordered:
        entry = {
            "name": name,
            "icon_url": icons.get(name, ""),
            "usage_pct": round(count / total * 100) if total else 0,
        }
        if played_as:
            entry["played_as"] = played_as
        summary.append(entry)
    return total, summary


def _ensure_member(conn: sqlite3.Connection, tag: str, name: Optional[str] = None, status: Optional[str] = None) -> str:
    """Upsert the player identity row; returns the canonical tag (§7: the tag
    IS the key — pre-v5.1 this returned a synthetic member_id). The `status`
    parameter is accepted-and-ignored: membership is an open clan_memberships
    row now, not a column."""
    tag = _canon_tag(tag)
    if not tag:
        raise ValueError("player tag is required")
    now = _utcnow()
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(player_tag) DO UPDATE SET "
        "current_name = COALESCE(excluded.current_name, players.current_name), "
        "last_seen_at = excluded.last_seen_at",
        (tag, name, now, now),
    )
    if name:
        conn.execute(
            "INSERT INTO player_aliases (player_tag, alias, source, observed_at) "
            "VALUES (?, ?, 'clan_api', ?) "
            "ON CONFLICT(player_tag, alias) DO UPDATE SET observed_at = excluded.observed_at",
            (tag, name, now),
        )
    return tag


_ensure_player = _ensure_member  # v5.1 name; same function


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


def _get_current_membership(conn: sqlite3.Connection, player_tag: str):
    return conn.execute(
        "SELECT membership_id, joined_at, join_source FROM clan_memberships "
        "WHERE player_tag = ? AND left_at IS NULL "
        "ORDER BY CASE join_source "
        "WHEN 'manual_record' THEN 1 "
        "WHEN 'observed_join' THEN 2 "
        "WHEN 'clan_api_snapshot' THEN 3 "
        "WHEN 'backfill' THEN 4 "
        "WHEN 'bootstrap_seed' THEN 5 "
        "ELSE 99 END, joined_at DESC, membership_id DESC LIMIT 1",
        (_canon_tag(player_tag),),
    ).fetchone()


def _trusted_current_joined_at(conn: sqlite3.Connection, player_tag: str) -> Optional[str]:
    membership = conn.execute(
        "SELECT joined_at FROM clan_memberships "
        "WHERE player_tag = ? AND left_at IS NULL AND join_source IN ('manual_record', 'observed_join', 'clan_api_snapshot') "
        "ORDER BY CASE join_source "
        "WHEN 'manual_record' THEN 1 "
        "WHEN 'observed_join' THEN 2 "
        "WHEN 'clan_api_snapshot' THEN 3 "
        "ELSE 99 END, joined_at DESC, membership_id DESC LIMIT 1",
        (_canon_tag(player_tag),),
    ).fetchone()
    return membership["joined_at"] if membership else None


def _current_joined_at(conn: sqlite3.Connection, player_tag: str) -> Optional[str]:
    meta = conn.execute(
        "SELECT joined_at FROM player_metadata WHERE player_tag = ?",
        (_canon_tag(player_tag),),
    ).fetchone()
    if meta and meta["joined_at"]:
        return meta["joined_at"]
    return None


_MEMBER_METADATA_COLUMNS = frozenset({
    # poap_address dropped (Q4: POAP paused; archive keeps historical values)
    "joined_at", "birth_month", "birth_day", "profile_url", "note",
    "generated_bio", "generated_highlight", "generated_profile_updated_at",
    "cr_account_age_days", "cr_account_age_years", "cr_account_age_updated_at",
    "cr_games_per_day", "cr_games_per_day_window_days", "cr_games_per_day_updated_at",
    "cr_collection_level", "cr_collection_level_badge_tier", "cr_collection_level_badge_max_tier",
    "cr_collection_level_updated_at", "cr_clan_war_wins", "cr_battle_wins",
    "cr_clan_donations", "cr_banner_count", "cr_emote_count", "cr_profile_badges_updated_at",
})


def _upsert_member_metadata(conn: sqlite3.Connection, player_tag: str, **fields) -> None:
    bad = set(fields) - _MEMBER_METADATA_COLUMNS
    if bad:
        raise ValueError(f"Invalid player_metadata columns: {bad}")
    tag = _canon_tag(player_tag)
    row = conn.execute("SELECT player_tag FROM player_metadata WHERE player_tag = ?", (tag,)).fetchone()
    if not row:
        conn.execute("INSERT INTO player_metadata (player_tag) VALUES (?)", (tag,))
    updates = []
    values = []
    for key, value in fields.items():
        updates.append(f"{key} = ?")
        values.append(value)
    if updates:
        values.append(tag)
        conn.execute(f"UPDATE player_metadata SET {', '.join(updates)} WHERE player_tag = ?", values)


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
    conn.execute(
        "INSERT OR IGNORE INTO raw_api_payloads (endpoint, entity_key, fetched_at, payload_hash, payload_json) VALUES (?, ?, ?, ?, ?)",
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
    """True when the DB carries the v5.1 spine (or is empty — tests own their
    schema). Incompatibility is handled by REFUSING to start, never by
    rebuilding: the pre-v5.1 move-aside-and-rebuild behavior is gone (the old
    truth lives in the read-only archive; destroying a mispointed DB is the
    exact footgun that wiped prod once)."""
    tables = _existing_tables(conn)
    if not tables:
        return True

    for table_name, expected_columns in _V51_SCHEMA_CORE.items():
        if table_name not in tables:
            return False
        if not expected_columns.issubset(_table_columns(conn, table_name)):
            return False
    return True


# Legacy migrations (db/_migrations.py) no longer run: the v5.1 baseline is
# scripts/migrate_v51/schema_v51.py and the connection layer refuses non-v5.1
# databases instead of migrating in place (docs/v5.1/migration.md Phase 2).


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


def _configure_connection(conn: sqlite3.Connection, path: str) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets reads run concurrently with writes — important because the
    # heartbeat job snapshot-inserts while interactive channel handlers read.
    # :memory: databases can't use WAL (each connection is isolated).
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    _enable_sqlite_vec(conn)


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = os.fspath(db_path or _resolve_db_path())
    conn = sqlite3.connect(path)
    _configure_connection(conn, path)
    if not _schema_is_compatible(conn):
        tables = sorted(_existing_tables(conn))
        conn.close()
        raise RuntimeError(
            f"Database at {path} does not carry the v5.1 spine (tables: "
            f"{', '.join(tables[:12]) or '<none>'}…). Refusing to start — this build "
            f"never rebuilds or migrates a database in place. Point ELIXIR_DB_PATH at "
            f"the v5.1 database (elixir-v51.db) or restore it from the archive per "
            f"docs/v5.1/migration.md."
        )
    if not _existing_tables(conn):
        # Empty DB (tests, scratch work): lay down the v5.1 engine spine.
        # Carried-table DDL comes from the archive at migration time; fixtures
        # that need those tables create them explicitly (Phase 8 convention).
        from scripts.migrate_v51.schema_v51 import NEW_DDL

        conn.executescript(NEW_DDL)
        conn.commit()
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


# ---------------------------------------------------------------------------
# Storage facade
#
# db re-exports every public storage function so callers can write
# db.snapshot_members(...) etc. The storage modules are imported lazily, on
# first attribute access (PEP 562), NOT at import time. This matters: storage
# modules import db's core helpers above, so an eager import here would make
# db <-> storage a bidirectional import-time cycle whose behavior depends on
# which side happens to be imported first (a storage module imported before
# db would be only partially initialized when __export_public scanned it,
# silently dropping names from the facade). Lazy loading guarantees every
# storage module is fully initialized before its names are merged.
# ---------------------------------------------------------------------------

def __export_public(module):
    names = getattr(module, "__all__", None) or [
        name for name in vars(module) if not name.startswith("__")
    ]
    for name in names:
        globals()[name] = getattr(module, name)
    return names


_STORAGE_FACADE_MODULES = (
    # v5.1: storage.projects / storage.decision_cases /
    # storage.communication_intents / storage.clan_voyages retired with
    # Gens A–C (schema.md §8, C6); storage.cases carries the decision-case
    # tool writes onto the renamed columns.
    "storage.identity",
    "storage.war",
    "storage.roster",
    "storage.cards",
    "storage.game_modes",
    "storage.player",
    "storage.trends",
    "storage.messages",
    "storage.cases",
    "storage.events_read",
    "storage.api_sentinel",
    "storage.game_mode_contexts",
    "storage.metadata",
    "storage.tournament",
    "storage.card_catalog",
    "storage.revisits",
    "storage.awards",
    "storage.member_ranks",
    "storage.leader_actions",
    "storage.improvements",
    "storage.runtime_status",
    "storage.screenshot_observations",
)

_facade_lock = threading.RLock()
_facade_loaded = False


def _load_storage_facade() -> None:
    global _facade_loaded
    with _facade_lock:
        if _facade_loaded:
            return
        for module_name in _STORAGE_FACADE_MODULES:
            __export_public(importlib.import_module(module_name))
        globals()["__all__"] = [name for name in globals() if not name.startswith("__")]
        _facade_loaded = True


def __getattr__(name):
    # Lazy operational DB path: always reflects the current environment, so
    # import order vs load_dotenv() can't freeze a stale value. A test
    # monkeypatch.setattr(db, "DB_PATH", ...) creates a real attribute that
    # shadows this resolver.
    if name == "DB_PATH":
        return _resolve_db_path()
    if not _facade_loaded:
        _load_storage_facade()
        if name in globals():
            return globals()[name]
    raise AttributeError(f"module 'db' has no attribute {name!r}")
