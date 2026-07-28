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

_DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "elixir-v51.db")


def _resolve_db_path() -> str:
    """The operational DB path, resolved from the environment at call time.

    Lazy resolution avoids the import-order trap: ELIXIR_DB_PATH is set by
    load_dotenv(), which can run AFTER `import db`. A frozen module constant
    would capture the pre-dotenv default. The v5.1 operational default is
    elixir-v51.db; the pre-cut elixir-v5.db is retired.
    """
    return os.getenv("ELIXIR_DB_PATH", _DEFAULT_DB_PATH)


CHICAGO_TZ = ZoneInfo("America/Chicago")

# v5.1 retention (docs/reference/v5.1/schema.md §1). Names kept where semantics carried.
RAW_PAYLOAD_RETENTION_DAYS = 14
BATTLE_EVENT_RETENTION_DAYS = 180
PLAYER_EVENT_RETENTION_DAYS = 180
CLAN_EVENT_RETENTION_DAYS = 365
WAR_RETENTION_DAYS = 365
CONVERSATION_RETENTION_DAYS = 30
TOURNAMENT_RETENTION_DAYS = 365
# LLM call telemetry: keep the full prompt/response BLOBS briefly (debugging /
# prompt-tuning window), then NULL them but keep the lightweight metadata row
# (tokens/model/latency) longer for cost analysis, then drop the row entirely.
LLM_PROMPT_RETENTION_DAYS = 14
LLM_CALL_RETENTION_DAYS = 90
CONVERSATION_MAX_PER_SCOPE = 20

# The v5.1 spine — used to refuse a wrong database before the bounded forward
# migrations in db.schema run (the pre-v5.1 truth is archived, not destroyed).
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
    """Delegates to the normalizer's single parser (engine/normalize.py).
    Now tz-aware; the only caller (chicago_date_for_cr_timestamp) already
    guards for either."""
    from engine.normalize import parse_cr_time

    return parse_cr_time(value)


def _parse_iso_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError, TypeError:
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
    """Delegates to the normalizer (engine/normalize.py) — the single home
    for the rarity-relative display-level math."""
    from engine.normalize import card_display_level

    return card_display_level(card.get("level"), card.get("maxLevel"))


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


def _aggregate_card_usage_from_battle_facts(
    rows: Iterable[sqlite3.Row],
) -> tuple[int, list[dict]]:
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


def _ensure_member(
    conn: sqlite3.Connection,
    tag: str,
    name: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
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


def _ensure_thread(
    conn: sqlite3.Connection,
    scope: str,
    channel_id=None,
    discord_user_id=None,
    member_id=None,
) -> int:
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
        # roster_diff = the v5.1 engine OBSERVED the join (most direct evidence
        # there is); its absence here made recent_joins blind to every
        # post-migration member (battery finding 2026-07-04, case 22).
        "SELECT joined_at FROM clan_memberships "
        "WHERE player_tag = ? AND left_at IS NULL AND join_source IN "
        "('manual_record', 'observed_join', 'roster_diff', 'clan_api_snapshot') "
        "ORDER BY CASE join_source "
        "WHEN 'manual_record' THEN 1 "
        "WHEN 'observed_join' THEN 2 "
        "WHEN 'roster_diff' THEN 2 "
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
    # No curated date -> the engine's membership row is the truth (new joiners
    # have no metadata row until a leader writes one; they must still count).
    membership = conn.execute(
        "SELECT joined_at FROM clan_memberships "
        "WHERE player_tag = ? AND left_at IS NULL "
        "ORDER BY joined_at DESC, membership_id DESC LIMIT 1",
        (_canon_tag(player_tag),),
    ).fetchone()
    return membership["joined_at"] if membership else None


_MEMBER_METADATA_COLUMNS = frozenset(
    {
        # poap_address dropped (Q4: POAP paused; archive keeps historical values)
        "joined_at",
        "birth_month",
        "birth_day",
        "profile_url",
        "note",
        "generated_bio",
        "generated_highlight",
        "generated_profile_updated_at",
        "cr_account_age_days",
        "cr_account_age_years",
        "cr_account_age_updated_at",
        "cr_games_per_day",
        "cr_games_per_day_window_days",
        "cr_games_per_day_updated_at",
        "cr_collection_level",
        "cr_collection_level_badge_tier",
        "cr_collection_level_badge_max_tier",
        "cr_collection_level_updated_at",
        "cr_clan_war_wins",
        "cr_battle_wins",
        "cr_clan_donations",
        "cr_banner_count",
        "cr_emote_count",
        "cr_profile_badges_updated_at",
        "preferred_nickname",
        "nickname_source",
        "nickname_updated_at",
        "email",
        "email_verified_at",
        "email_source",
    }
)


def _upsert_member_metadata(conn: sqlite3.Connection, player_tag: str, **fields) -> None:
    bad = set(fields) - _MEMBER_METADATA_COLUMNS
    if bad:
        raise ValueError(f"Invalid player_metadata columns: {bad}")
    tag = _canon_tag(player_tag)
    row = conn.execute(
        "SELECT player_tag FROM player_metadata WHERE player_tag = ?", (tag,)
    ).fetchone()
    if not row:
        conn.execute("INSERT INTO player_metadata (player_tag) VALUES (?)", (tag,))
    updates = []
    values = []
    for key, value in fields.items():
        updates.append(f"{key} = ?")
        values.append(value)
    if updates:
        values.append(tag)
        conn.execute(
            f"UPDATE player_metadata SET {', '.join(updates)} WHERE player_tag = ?",
            values,
        )


def set_member_nickname(
    player_tag: str,
    nickname: Optional[str],
    *,
    source: Optional[str] = "leader",
    observed_at: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Set (or clear, when nickname is None) a player's stored preferred
    nickname. `source` is 'leader' (hand-picked) or 'generated'/'placeholder'
    (engine/nicknames.py). Commits only when it owns the connection, so it is
    safe to call inside the engine tick with an external conn (no mid-tick
    commit of the caller's transaction)."""
    owns = conn is None
    conn = conn or get_connection()
    try:
        clean = (nickname or "").strip() or None
        _upsert_member_metadata(
            conn,
            player_tag,
            preferred_nickname=clean,
            nickname_source=(source if clean else None),
            nickname_updated_at=(observed_at or _utcnow()),
        )
        # Leader override changed tier 1 → re-materialize the player's display_name
        # (normalize-at-source). Late import: engine.db imports from this module.
        try:
            from engine.db import refresh_display_name

            refresh_display_name(conn, player_tag)
        except Exception as exc:
            # The nickname is authoritative even if its disposable projection
            # cannot refresh; record the abandoned materialization for repair.
            from storage.incidents import record_incident

            record_incident(
                "db.set_member_nickname.refresh_display_name",
                exc,
                context={"player_tag": _canon_tag(player_tag)},
                severity="warn",
                conn=conn,
            )
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _parse_optional_int(
    value: Optional[str], *, field_name: str, minimum: int, maximum: int
) -> Optional[int]:
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


def _ensure_channel(
    conn: sqlite3.Connection, channel_id, channel_name=None, channel_kind=None
) -> None:
    if channel_id is None:
        return
    channel_id = str(channel_id)
    now = _utcnow()
    row = conn.execute(
        "SELECT channel_id FROM discord_channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
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


def _store_raw_payload(
    conn: sqlite3.Connection, endpoint: str, entity_key: str, payload
) -> dict | None:
    payload_json = _json_or_none(payload)
    if payload_json is None:
        return None
    # Use the observation envelope's canonical hash, not the serialized
    # compatibility blob's whitespace-sensitive hash. This is what lets a
    # generation point back to the exact network receipt that fed it.
    from engine.db import payload_hash as canonical_payload_hash

    payload_hash = canonical_payload_hash(payload)
    fetched_at = _utcnow()
    conn.execute(
        """INSERT INTO raw_api_payloads
               (endpoint, entity_key, fetched_at, last_fetched_at,
                payload_hash, payload_json)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(endpoint, entity_key, payload_hash) DO UPDATE SET
               last_fetched_at = excluded.last_fetched_at""",
        (endpoint, entity_key, fetched_at, fetched_at, payload_hash, payload_json),
    )
    payload_row = conn.execute(
        """SELECT payload_id FROM raw_api_payloads
           WHERE endpoint = ? AND entity_key = ? AND payload_hash = ?""",
        (endpoint, entity_key, payload_hash),
    ).fetchone()
    payload_id = int(payload_row["payload_id"])
    receipt = conn.execute(
        """INSERT INTO api_observation_receipts
               (payload_id, endpoint, entity_key, fetched_at, payload_hash,
                admission_status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            payload_id,
            endpoint,
            entity_key,
            fetched_at,
            payload_hash,
            "pending"
            if endpoint in {"clan", "currentriverrace", "player", "player_battlelog"}
            else "not_applicable",
        ),
    )
    return {
        "payload_id": payload_id,
        "receipt_id": int(receipt.lastrowid),
        "payload_hash": payload_hash,
    }


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


# Pre-v5.1 migration history lives in Git and the immutable archive. The
# clean-break baseline is scripts/migrate_v51/schema_v51.py; bounded post-cut
# forward migrations live exclusively in db.schema.


def _configure_connection(conn: sqlite3.Connection, path: str) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets reads run concurrently with writes — important because the
    # heartbeat job snapshot-inserts while interactive channel handlers read.
    # :memory: databases can't use WAL (each connection is isolated).
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        # The engine tick can hold the writer slot across a step batch;
        # side-writers (cr_api raw-payload persistence, tool paths) must wait
        # instead of failing with 'database is locked'.
        conn.execute("PRAGMA busy_timeout = 30000")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = os.fspath(db_path or _resolve_db_path())
    conn = sqlite3.connect(path)
    # Inspect a possibly mispointed database before enabling WAL or changing
    # any other persistent pragma. Refusing a pre-v5.1 database must be a
    # genuinely read-only decision.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    tables = _existing_tables(conn)
    if tables and not _schema_is_compatible(conn):
        tables = sorted(_existing_tables(conn))
        conn.close()
        raise RuntimeError(
            f"Database at {path} does not carry the v5.1 spine (tables: "
            f"{', '.join(tables[:12]) or '<none>'}…). Refusing to start — this build "
            f"never upgrades a pre-v5.1 database in place. Point ELIXIR_DB_PATH at "
            f"the v5.1 database (elixir-v51.db) or restore it from the archive per "
            f"docs/reference/v5.1/migration.md."
        )
    _configure_connection(conn, path)
    if not tables:
        # Empty DB (tests, scratch work): build the complete baseline, including
        # frozen carried-table DDL, then run the same forward migrations as prod.
        from scripts.migrate_v51.schema_v51 import NEW_DDL, carried_ddl

        for statement in carried_ddl(None):
            conn.execute(statement)
        conn.executescript(NEW_DDL)
    from db.schema import apply_schema_migrations

    apply_schema_migrations(conn)
    return conn


def managed_connection(fn: Callable) -> Callable:
    """Decorator that manages the conn=None lifecycle pattern.

    If the caller passes conn=None (the default), a new connection is opened,
    COMMITTED on success (rolled back on error), and closed after the call.
    If a connection is provided, it is passed through untouched and left open
    for the caller to commit/close — the decorator never commits a borrowed
    connection.

    This is why a decorated writer must NOT call conn.commit() itself: when it
    owns the connection the decorator commits for it, and when it is handed the
    engine tick's connection an internal commit would prematurely persist that
    step's partial work and defeat the tick's per-step rollback guard. Owning
    the commit here removes that whole footgun class (the old decorator only
    opened/closed, forcing every writer to commit unconditionally).
    """

    @functools.wraps(fn)
    def wrapper(*args, conn=None, **kwargs):
        owns = conn is None
        conn = conn or get_connection()
        try:
            result = fn(*args, conn=conn, **kwargs)
            if owns:
                conn.commit()
            return result
        except Exception:
            if owns:
                conn.rollback()
            raise
        finally:
            if owns:
                conn.close()

    return wrapper


# ---------------------------------------------------------------------------
# Storage facade
#
# `db` remains the stable compatibility surface, but every re-export is named
# here deliberately. Values are resolved lazily so storage modules can keep
# importing the core connection/helpers above without an import-time cycle.
# Import order can no longer overwrite a colliding name: duplicate declarations
# fail immediately while this registry is built.
# ---------------------------------------------------------------------------

_FACADE_EXPORT_GROUPS = {
    "storage.identity": (
        "build_memory_context",
        "bump_email_challenge_attempts",
        "clear_email_challenge",
        "clear_member_discord_link",
        "clear_member_email",
        "format_member_reference",
        "get_channel_state",
        "get_database_status",
        "get_discord_link",
        "get_email_challenge",
        "get_linked_member_for_discord_user",
        "get_member_identity",
        "get_memory_episodes",
        "get_system_status",
        "is_valid_email",
        "link_discord_user_to_member",
        "list_member_emails",
        "save_memory_episode",
        "set_member_discord_identity",
        "set_member_email",
        "upsert_discord_user",
        "upsert_email_challenge",
    ),
    "storage.war_status": (
        "DAILY_RANK_FAME",
        "FAME_FINISH_LINE",
        "FINAL_BATTLE_PERIOD_OFFSET",
        "FINAL_PRACTICE_PERIOD_OFFSET",
        "FIRST_BATTLE_PERIOD_OFFSET",
        "HOME_CLAN",
        "NORMAL_RIVER_RACE_FINISH_LINE",
        "build_war_now_context",
        "count_war_races_for_season",
        "get_current_season_id",
        "get_current_war_day_state",
        "get_current_war_status",
        "get_latest_clan_boat_defense_status",
        "get_latest_war_participant_snapshot_observed_at",
        "get_latest_war_race_finish_time",
        "get_season_window",
        "get_trophy_changes",
        "get_trophy_drops",
        "get_war_day_state",
        "get_war_deck_status_today",
        "get_war_history",
        "get_war_season_snapshot",
        "get_war_season_summary",
        "get_war_week_summary",
        "get_week_win_streak",
        "is_colosseum_week_confirmed",
        "is_war_section_finalized",
        "list_recent_war_day_summaries",
    ),
    "storage.war_members": (
        "get_member_missed_war_days",
        "get_member_war_attendance",
        "get_member_war_battle_record",
        "get_member_war_stats",
        "get_member_war_status",
        "member_roster_status",
    ),
    "storage.war_analytics": (
        "ELDER_DONATION_ROLLING_WEEKS",
        "INACTIVITY_DAYS_PER_1K_TROPHIES_LOOSE",
        "INACTIVITY_DAYS_PER_1K_TROPHIES_TIGHT",
        "LOOSE_MEMBER_COUNT",
        "TIGHT_MEMBER_COUNT",
        "compare_fame_per_member_to_previous_season",
        "compare_member_war_to_clan_average",
        "get_clan_boat_battle_record",
        "get_demotion_candidates",
        "get_members_at_risk",
        "get_members_without_war_participation",
        "get_perfect_war_participants",
        "get_promotion_candidates",
        "get_recent_role_changes",
        "get_trending_war_contributors",
        "get_war_battle_win_rates",
        "get_war_champ_standings",
        "get_war_score_trend",
        "get_war_season_history",
        "reconstruct_member_war_decks",
        "war_player_types_by_tag",
    ),
    "storage.roster": (
        "get_active_roster_map",
        "get_clan_roster_summary",
        "get_member_history",
        "get_member_membership_summary",
        "get_member_overview",
        "get_member_profile",
        "get_member_recent_form",
        "get_members_on_hot_streak",
        "get_members_on_losing_streak",
        "get_weekly_digest_summary",
        "list_clan_daily_metrics",
        "list_longest_tenure_members",
        "list_members",
        "list_recent_joins",
        "pick_best_match",
        "resolve_member",
        "snapshot_clan_daily_metrics",
        "snapshot_members",
    ),
    "storage.cards": (
        "KING_TOWER_MAX_LEVEL",
        "get_clan_favourite_card_counts",
        "get_clan_most_common_maxed_cards",
        "get_clan_overlooked_cards",
        "get_clan_rare_maxed_cards",
        "get_clan_recently_played_cards",
        "get_member_card_collection",
        "get_member_card_profile",
        "get_member_current_deck",
        "get_member_signature_cards",
        "get_members_with_most_level_16_cards",
        "list_card_owners",
        "list_current_member_decks",
        "list_deck_battle_history",
        "lookup_member_cards",
    ),
    "storage.game_modes": (
        "FRIENDLY_GAME_MODE_IDS",
        "LADDER_GAME_MODE_IDS",
        "MODE_GROUPS",
        "MODE_GROUP_LABELS",
        "RANKED_GAME_MODE_IDS",
        "SPECIAL_EVENT_BADGE_CONTEXTS",
        "TWO_V_TWO_GAME_MODE_IDS",
        "WAR_GAME_MODE_IDS",
        "battle_matches_mode",
        "classify_battle_mode",
        "classify_progress_key",
        "mode_group_label",
        "special_event_badge_names",
        "special_event_context_for_badge",
    ),
    "storage.player": (
        "BADGE_NAME_OVERRIDES",
        "CARD_UNLOCK_SIGNAL_RARITIES",
        "CARD_UPGRADE_SIGNAL_MIN_LEVEL",
        "GAMES_PER_DAY_WINDOW_DAYS",
        "MASTERY_BADGE_SIGNAL_MIN_LEVEL",
        "get_clan_game_mode_summary",
        "get_clan_mode_top_members",
        "get_member_mode_activity",
        "get_member_ranked_status",
        "get_member_recent_battles",
        "get_member_recent_losses",
        "get_member_special_event_activity",
        "get_player_intel_refresh_targets",
        "list_clan_daily_battle_rollups",
        "list_player_daily_battle_rollups",
        "snapshot_player_battlelog",
        "snapshot_player_profile",
    ),
    "storage.trends": (
        "build_clan_trend_summary_context",
        "build_member_trend_summary_context",
        "compare_clan_trend_windows",
        "compare_member_trend_windows",
        "get_clan_daily_battle_summary",
        "get_clan_member_count_history",
        "get_clan_score_history",
        "get_clan_total_member_trophies_history",
        "get_member_daily_battle_summary",
        "get_member_trophy_history",
    ),
    "storage.messages": (
        "clear_prompt_feedback",
        "get_llm_call",
        "get_message_by_discord_message_id",
        "list_channel_messages",
        "list_llm_calls",
        "list_pending_system_signals",
        "list_prompt_failures",
        "list_prompt_feedback",
        "list_prompt_review_items",
        "list_thread_messages",
        "mark_prompt_feedback_retry_invited",
        "mark_system_signal_announced",
        "purge_old_conversations",
        "queue_system_signal",
        "record_llm_call",
        "record_prompt_failure",
        "save_message",
        "update_message_summary",
        "upsert_prompt_feedback",
    ),
    "storage.cases": (
        "CASE_DEFERRED",
        "CASE_DISMISSED",
        "CASE_OPEN",
        "CASE_RESOLVED",
        "CASE_TYPES",
        "backfill_decision_cases_from_leader_actions",
        "decision_case_snapshot",
        "expire_departure_verification_cards",
        "get_decision_case",
        "get_decision_case_by_id",
        "list_decision_cases",
        "list_due_decision_cases",
        "raise_departure_verification_cards",
        "reconcile_departed_member_cases",
        "reconcile_uncorroborated_member_cases",
        "resolve_decision_case",
        "sync_terminal_leader_action_cases",
        "upsert_decision_case",
        "upsert_decision_cases_from_signals",
        "upsert_member_review_case",
    ),
    "storage.events_read": (
        "DETECTION_WINDOWS",
        "list_events_after_cursors",
        "list_recent_events",
        "summarize_battle_modes",
        "summarize_event_windows",
    ),
    "storage.api_sentinel": (
        "bootstrap_api_sentinel_baseline",
        "build_api_sentinel_observations",
        "list_api_sentinel_observations",
        "record_api_payload_sentinel_observations",
    ),
    "storage.game_mode_contexts": (
        "list_game_mode_contexts",
        "upsert_game_mode_contexts_from_events",
        "upsert_game_mode_contexts_from_leaderboards",
    ),
    "storage.metadata": (
        "clear_member_birthday",
        "clear_member_join_date",
        "clear_member_note",
        "clear_member_profile_url",
        "list_member_metadata_rows",
        "purge_old_data",
        "set_member_birthday",
        "set_member_join_date",
        "set_member_note",
        "set_member_profile_url",
    ),
    "storage.tournament": (
        "build_tournament_recap_context",
        "deck_selection_label",
        "finalize_tournament",
        "game_mode_label",
        "get_active_tournament",
        "get_recent_tournaments_for_recap",
        "get_tournament_battles",
        "get_tournament_by_tag",
        "get_tournament_card_stats",
        "get_tournament_participants",
        "list_pending_tournament_recaps",
        "poll_tournament",
        "register_tournament",
        "store_tournament_battle",
    ),
    "storage.card_catalog": (
        "catalog_count",
        "get_all_cards",
        "get_card_by_name",
        "lookup_cards",
        "sync_card_catalog",
    ),
    "storage.revisits": (
        "list_due_revisits",
        "list_pending_revisits",
        "mark_revisited",
        "schedule_revisit",
    ),
    "storage.awards": (
        "award_leaderboard",
        "get_award_races",
        "get_awards_by_season",
        "get_iron_king_candidates",
        "get_member_trophy_case",
        "get_rookie_mvp_candidates",
        "get_season_awards_standings",
        "get_season_donation_leaderboard",
        "get_war_participant_candidates",
        "insert_award",
        "list_awards",
        "season_final_section_index",
        "season_is_complete",
    ),
    "storage.member_ranks": (
        "ELDER_ELIGIBILITY_DEFAULTS",
        "RANK_FIELDS",
        "compute_member_ranks",
        "evaluate_elder_eligibility",
    ),
    "storage.leader_actions": (
        "ACTION_DEFERRED",
        "ACTION_DONE",
        "ACTION_OUTCOME_DELAY_HOURS",
        "ACTION_PROPOSED",
        "ACTION_REJECTED",
        "LEADER_ACTION_FEEDBACK_EVENT_TYPE",
        "LEAVE_SOURCE_VERIFIED",
        "POSTING_SENTINEL",
        "auto_withdraw_leader_actions",
        "build_leader_action_baseline",
        "build_leader_action_feedback_synthesis_context",
        "classify_departure",
        "clear_leader_action_decision_by_message",
        "clear_leader_action_source_message",
        "create_leader_action_recommendation",
        "decide_leader_action",
        "decide_leader_action_by_message",
        "evaluate_leader_action",
        "get_leader_action_by_id",
        "get_leader_action_by_key",
        "get_leader_action_by_message",
        "get_recent_leader_action_for_target",
        "has_recent_leader_action",
        "leader_action_board_snapshot",
        "leader_action_decision_stats",
        "list_interpreted_leader_actions",
        "list_leader_action_feedback_profiles",
        "list_leader_actions",
        "note_text_hash",
        "set_leader_action_note_text",
        "record_leader_action_note_by_message",
        "record_note_interpretation",
        "refresh_due_leader_action_outcomes",
        "refresh_leader_action_outcome",
        "set_leader_action_premise",
        "set_leader_action_suppression",
        "update_leader_action_copy_message",
        "update_leader_action_copy_messages",
        "update_leader_action_copy_text",
        "update_leader_action_message",
        "upsert_leader_action_feedback_profile",
    ),
    "storage.improvements": (
        "SUGGESTION_CATEGORIES",
        "SUGGESTION_DISMISSED",
        "SUGGESTION_IMPLEMENTED",
        "SUGGESTION_PROMOTED",
        "SUGGESTION_SHADOW",
        "SUGGESTION_STATUSES",
        "build_improvement_github_issue_body",
        "get_improvement_suggestion",
        "github_labels_for_improvement",
        "list_improvement_suggestions",
        "mark_improvement_suggestion_promoted",
        "suggestion_key_for",
        "upsert_improvement_suggestion",
    ),
    "storage.runtime_status": (
        "get_awareness_activity",
        "get_awareness_loop_by_number",
        "list_runtime_job_status",
        "save_runtime_job_status",
    ),
    "storage.screenshot_observations": (
        "list_arena_relay_screenshot_observations",
        "save_arena_relay_screenshot_observation",
    ),
}


def _build_facade_exports(groups: dict[str, tuple[str, ...]]) -> dict[str, str]:
    exports: dict[str, str] = {}
    for module_name, names in groups.items():
        for name in names:
            previous = exports.get(name)
            if previous is not None:
                raise RuntimeError(
                    f"db facade export {name!r} declared by both {previous!r} and {module_name!r}"
                )
            exports[name] = module_name
    return exports


_FACADE_EXPORTS = _build_facade_exports(_FACADE_EXPORT_GROUPS)
_CORE_EXPORTS = {
    "BATTLE_EVENT_RETENTION_DAYS",
    "CHICAGO_TZ",
    "CLAN_EVENT_RETENTION_DAYS",
    "CONVERSATION_MAX_PER_SCOPE",
    "CONVERSATION_RETENTION_DAYS",
    "DB_PATH",
    "LLM_CALL_RETENTION_DAYS",
    "LLM_PROMPT_RETENTION_DAYS",
    "PLAYER_EVENT_RETENTION_DAYS",
    "RAW_PAYLOAD_RETENTION_DAYS",
    "TOURNAMENT_RETENTION_DAYS",
    "WAR_RETENTION_DAYS",
    "chicago_date_for_cr_timestamp",
    "chicago_date_for_utc_timestamp",
    "chicago_day_bounds_utc",
    "chicago_today",
    "get_connection",
    "managed_connection",
    "set_member_nickname",
}
_core_facade_collisions = _CORE_EXPORTS & set(_FACADE_EXPORTS)
if _core_facade_collisions:
    raise RuntimeError(
        f"db facade exports collide with core names: {', '.join(sorted(_core_facade_collisions))}"
    )
__all__ = sorted(_CORE_EXPORTS | set(_FACADE_EXPORTS))
_facade_lock = threading.RLock()


def __getattr__(name):
    # Lazy operational DB path: always reflects the current environment, so
    # import order vs load_dotenv() can't freeze a stale value. A test
    # monkeypatch.setattr(db, "DB_PATH", ...) creates a real attribute that
    # shadows this resolver.
    if name == "DB_PATH":
        return _resolve_db_path()
    module_name = _FACADE_EXPORTS.get(name)
    if module_name is not None:
        with _facade_lock:
            if name in globals():
                return globals()[name]
            module = importlib.import_module(module_name)
            try:
                value = getattr(module, name)
            except AttributeError as exc:
                raise RuntimeError(
                    f"db facade declares {name!r} from {module_name!r}, but the "
                    "source module does not define it"
                ) from exc
            globals()[name] = value
            return value
    raise AttributeError(f"module 'db' has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
