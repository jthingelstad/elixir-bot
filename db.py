"""db.py — SQLite storage layer for Elixir bot.

V2 resets the schema around stable member identity, Discord identity, raw API
payloads, war history, battle facts, and conversational memory.

The module exposes the V2 identity, memory, roster, battle, and war query layer.
"""

from __future__ import annotations

import csv as csv_mod
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from cr_knowledge import TROPHY_MILESTONES

log = logging.getLogger("elixir_db")

DB_PATH = os.getenv("ELIXIR_DB_PATH", os.path.join(os.path.dirname(__file__), "elixir.db"))

SNAPSHOT_RETENTION_DAYS = 90
WAR_RETENTION_DAYS = 180
RAW_PAYLOAD_RETENTION_DAYS = 90
CONVERSATION_RETENTION_DAYS = 30
CONVERSATION_MAX_PER_SCOPE = 20
def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


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
    return level if isinstance(level, int) else None


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
        "SELECT membership_id, joined_at, join_source FROM clan_memberships WHERE member_id = ? AND left_at IS NULL ORDER BY joined_at DESC LIMIT 1",
        (member_id,),
    ).fetchone()


def _current_joined_at(conn: sqlite3.Connection, member_id: int) -> Optional[str]:
    meta = conn.execute(
        "SELECT joined_at_override FROM member_metadata WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    if meta and meta["joined_at_override"]:
        return meta["joined_at_override"]
    membership = _get_current_membership(conn, member_id)
    if membership and membership["join_source"] == "bootstrap_seed":
        return None
    return membership["joined_at"] if membership else None


def _upsert_member_metadata(conn: sqlite3.Connection, member_id: int, **fields) -> None:
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
    item["member_ref_with_handle"] = format_member_reference(tag, style="name_with_handle", conn=conn)
    item["member_ref_with_mention"] = format_member_reference(tag, style="name_with_mention", conn=conn)
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


def _migration_0(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS members (
            member_id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_tag TEXT NOT NULL UNIQUE,
            current_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_metadata (
            member_id INTEGER PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
            joined_at_override TEXT,
            birth_month INTEGER,
            birth_day INTEGER,
            profile_url TEXT DEFAULT '',
            poap_address TEXT DEFAULT '',
            note TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS member_aliases (
            alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(member_id, alias)
        );

        CREATE TABLE IF NOT EXISTS discord_users (
            discord_user_id TEXT PRIMARY KEY,
            username TEXT,
            global_name TEXT,
            display_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS discord_links (
            discord_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL REFERENCES discord_users(discord_user_id) ON DELETE CASCADE,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            discord_username TEXT,
            discord_display_name TEXT,
            linked_at TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            is_primary INTEGER NOT NULL DEFAULT 1,
            UNIQUE(discord_user_id, member_id)
        );

        CREATE TABLE IF NOT EXISTS discord_channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            channel_kind TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_threads (
            thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            channel_id TEXT REFERENCES discord_channels(channel_id) ON DELETE SET NULL,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            UNIQUE(scope_type, scope_key)
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id TEXT UNIQUE,
            thread_id INTEGER NOT NULL REFERENCES conversation_threads(thread_id) ON DELETE CASCADE,
            channel_id TEXT REFERENCES discord_channels(channel_id) ON DELETE SET NULL,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            author_type TEXT NOT NULL,
            workflow TEXT,
            event_type TEXT,
            content TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_facts (
            fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            source_message_id INTEGER REFERENCES messages(message_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_type TEXT NOT NULL,
            subject_key TEXT NOT NULL,
            episode_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            source_message_ids_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channel_state (
            channel_id TEXT PRIMARY KEY REFERENCES discord_channels(channel_id) ON DELETE CASCADE,
            last_elixir_post_at TEXT,
            last_topics_json TEXT,
            recent_style_notes_json TEXT,
            last_summary TEXT
        );

        CREATE TABLE IF NOT EXISTS clan_memberships (
            membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL,
            left_at TEXT,
            join_source TEXT NOT NULL,
            leave_source TEXT
        );

        CREATE TABLE IF NOT EXISTS member_current_state (
            member_id INTEGER PRIMARY KEY REFERENCES members(member_id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            role TEXT,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            clan_rank INTEGER,
            previous_clan_rank INTEGER,
            donations_week INTEGER,
            donations_received_week INTEGER,
            arena_id INTEGER,
            arena_name TEXT,
            arena_raw_name TEXT,
            last_seen_api TEXT,
            source TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS member_state_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            observed_at TEXT NOT NULL,
            name TEXT,
            role TEXT,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            clan_rank INTEGER,
            previous_clan_rank INTEGER,
            donations_week INTEGER,
            donations_received_week INTEGER,
            arena_id INTEGER,
            arena_name TEXT,
            arena_raw_name TEXT,
            last_seen_api TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS member_daily_metrics (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            metric_date TEXT NOT NULL,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            clan_rank INTEGER,
            donations_week INTEGER,
            donations_received_week INTEGER,
            last_seen_api TEXT,
            UNIQUE(member_id, metric_date)
        );

        CREATE TABLE IF NOT EXISTS player_profile_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            exp_level INTEGER,
            trophies INTEGER,
            best_trophies INTEGER,
            wins INTEGER,
            losses INTEGER,
            battle_count INTEGER,
            total_donations INTEGER,
            donations INTEGER,
            donations_received INTEGER,
            war_day_wins INTEGER,
            challenge_max_wins INTEGER,
            challenge_cards_won INTEGER,
            tournament_battle_count INTEGER,
            tournament_cards_won INTEGER,
            three_crown_wins INTEGER,
            current_favourite_card_id INTEGER,
            current_favourite_card_name TEXT,
            league_statistics_json TEXT,
            current_deck_json TEXT,
            cards_json TEXT,
            badges_json TEXT,
            achievements_json TEXT,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS member_card_collection_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            cards_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_deck_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            mode_scope TEXT NOT NULL,
            deck_hash TEXT,
            deck_json TEXT NOT NULL,
            sample_size INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS member_card_usage_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            mode_scope TEXT NOT NULL,
            sample_battles INTEGER NOT NULL DEFAULT 0,
            cards_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_battle_facts (
            battle_fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            battle_time TEXT NOT NULL,
            battle_type TEXT,
            game_mode_name TEXT,
            game_mode_id INTEGER,
            deck_selection TEXT,
            arena_id INTEGER,
            arena_name TEXT,
            crowns_for INTEGER,
            crowns_against INTEGER,
            outcome TEXT,
            trophy_change INTEGER,
            starting_trophies INTEGER,
            is_competitive INTEGER NOT NULL DEFAULT 0,
            is_ladder INTEGER NOT NULL DEFAULT 0,
            is_ranked INTEGER NOT NULL DEFAULT 0,
            is_war INTEGER NOT NULL DEFAULT 0,
            is_special_event INTEGER NOT NULL DEFAULT 0,
            deck_json TEXT,
            support_cards_json TEXT,
            opponent_name TEXT,
            opponent_tag TEXT,
            opponent_clan_tag TEXT,
            raw_json TEXT,
            UNIQUE(member_id, battle_time, battle_type, opponent_tag, crowns_for, crowns_against)
        );

        CREATE TABLE IF NOT EXISTS member_recent_form (
            form_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            computed_at TEXT NOT NULL,
            scope TEXT NOT NULL,
            sample_size INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            draws INTEGER NOT NULL,
            current_streak INTEGER NOT NULL DEFAULT 0,
            current_streak_type TEXT,
            win_rate REAL NOT NULL DEFAULT 0,
            avg_crown_diff REAL,
            avg_trophy_change REAL,
            form_label TEXT,
            summary TEXT,
            UNIQUE(member_id, scope)
        );

        CREATE TABLE IF NOT EXISTS war_current_state (
            war_id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL,
            war_state TEXT,
            clan_tag TEXT,
            clan_name TEXT,
            fame INTEGER,
            repair_points INTEGER,
            period_points INTEGER,
            clan_score INTEGER,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS war_day_status (
            status_id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
            battle_date TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            fame INTEGER,
            repair_points INTEGER,
            boat_attacks INTEGER,
            decks_used_total INTEGER,
            decks_used_today INTEGER,
            raw_json TEXT,
            UNIQUE(member_id, battle_date)
        );

        CREATE TABLE IF NOT EXISTS war_races (
            war_race_id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER NOT NULL,
            section_index INTEGER NOT NULL,
            created_date TEXT,
            our_rank INTEGER,
            trophy_change INTEGER,
            our_fame INTEGER,
            total_clans INTEGER,
            finish_time TEXT,
            raw_json TEXT,
            UNIQUE(season_id, section_index)
        );

        CREATE TABLE IF NOT EXISTS war_participation (
            participation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            war_race_id INTEGER NOT NULL REFERENCES war_races(war_race_id) ON DELETE CASCADE,
            member_id INTEGER REFERENCES members(member_id) ON DELETE SET NULL,
            player_tag TEXT NOT NULL,
            player_name TEXT,
            fame INTEGER,
            repair_points INTEGER,
            boat_attacks INTEGER,
            decks_used INTEGER,
            decks_used_today INTEGER,
            raw_json TEXT,
            UNIQUE(war_race_id, player_tag)
        );

        CREATE TABLE IF NOT EXISTS raw_api_payloads (
            payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(endpoint, entity_key, payload_hash)
        );

        CREATE TABLE IF NOT EXISTS signal_log (
            signal_date TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            UNIQUE(signal_date, signal_type)
        );

        CREATE TABLE IF NOT EXISTS cake_day_announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            announcement_date TEXT NOT NULL,
            announcement_type TEXT NOT NULL,
            target_tag TEXT,
            recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            UNIQUE(announcement_date, announcement_type, target_tag)
        );

        CREATE INDEX IF NOT EXISTS idx_members_status ON members(status);
        CREATE INDEX IF NOT EXISTS idx_members_tag ON members(player_tag);
        CREATE INDEX IF NOT EXISTS idx_memberships_member ON clan_memberships(member_id, left_at, joined_at);
        CREATE INDEX IF NOT EXISTS idx_current_rank ON member_current_state(clan_rank, role);
        CREATE INDEX IF NOT EXISTS idx_state_snapshots_member_time ON member_state_snapshots(member_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_metrics_member_date ON member_daily_metrics(member_id, metric_date DESC);
        CREATE INDEX IF NOT EXISTS idx_profile_snapshots_member_time ON player_profile_snapshots(member_id, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_battle_facts_member_time ON member_battle_facts(member_id, battle_time DESC);
        CREATE INDEX IF NOT EXISTS idx_recent_form_member_scope ON member_recent_form(member_id, scope);
        CREATE INDEX IF NOT EXISTS idx_war_races_season ON war_races(season_id, section_index);
        CREATE INDEX IF NOT EXISTS idx_war_participation_member ON war_participation(member_id, war_race_id);
        CREATE INDEX IF NOT EXISTS idx_war_day_status_member_date ON war_day_status(member_id, battle_date DESC);
        CREATE INDEX IF NOT EXISTS idx_raw_payloads_endpoint_entity ON raw_api_payloads(endpoint, entity_key, fetched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_threads_scope ON conversation_threads(scope_type, scope_key);
        CREATE INDEX IF NOT EXISTS idx_messages_thread_time ON messages(thread_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_subject ON memory_facts(subject_type, subject_key, fact_type);
        """
    )


_MIGRATIONS = [_migration_0]


def _run_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, fn in enumerate(_MIGRATIONS):
        if version < current:
            continue
        fn(conn)
        conn.execute(f"PRAGMA user_version = {version + 1}")
        conn.commit()


def get_connection(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _run_migrations(conn)
    return conn


def _load_extracted(relative_path):
    path = os.path.join(os.path.dirname(__file__), relative_path)
    with open(path, "r") as f:
        exec(compile(f.read(), path, "exec"), globals())


# -- Extracted domains ------------------------------------------------------

_load_extracted("storage/roster.py")
_load_extracted("storage/war.py")
_load_extracted("storage/player.py")
_load_extracted("storage/identity.py")
_load_extracted("storage/messages.py")
_load_extracted("storage/metadata.py")
