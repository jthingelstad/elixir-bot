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


# -- Discord identity and memory helpers -----------------------------------

def upsert_discord_user(discord_user_id, username=None, global_name=None, display_name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        now = _utcnow()
        row = conn.execute("SELECT discord_user_id FROM discord_users WHERE discord_user_id = ?", (str(discord_user_id),)).fetchone()
        if row:
            conn.execute(
                "UPDATE discord_users SET username = COALESCE(?, username), global_name = COALESCE(?, global_name), display_name = COALESCE(?, display_name), last_seen_at = ? WHERE discord_user_id = ?",
                (username, global_name, display_name, now, str(discord_user_id)),
            )
        else:
            conn.execute(
                "INSERT INTO discord_users (discord_user_id, username, global_name, display_name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(discord_user_id), username, global_name, display_name, now, now),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


def link_discord_user_to_member(discord_user_id, member_tag, username=None, display_name=None,
                                source="manual_link", confidence=1.0, is_primary=True, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        upsert_discord_user(discord_user_id, username=username, display_name=display_name, conn=conn)
        member_id = _ensure_member(conn, member_tag, name=None)
        if is_primary:
            conn.execute("UPDATE discord_links SET is_primary = 0 WHERE discord_user_id = ?", (str(discord_user_id),))
            conn.execute("UPDATE discord_links SET is_primary = 0 WHERE member_id = ?", (member_id,))
        conn.execute(
            "INSERT INTO discord_links (discord_user_id, member_id, discord_username, discord_display_name, linked_at, source, confidence, is_primary) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(discord_user_id, member_id) DO UPDATE SET discord_username = excluded.discord_username, discord_display_name = excluded.discord_display_name, linked_at = excluded.linked_at, source = excluded.source, confidence = excluded.confidence, is_primary = excluded.is_primary",
            (str(discord_user_id), member_id, username, display_name, _utcnow(), source, confidence, 1 if is_primary else 0),
        )
        conn.commit()
        return member_id
    finally:
        if close:
            conn.close()


def get_discord_link(member_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.player_tag, m.current_name, du.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
            "WHERE m.player_tag = ?",
            (_canon_tag(member_tag),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def get_member_identity(member_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name AS member_name, du.discord_user_id, dl.discord_username, dl.discord_display_name, "
            "CASE WHEN dl.discord_user_id IS NULL THEN 0 ELSE 1 END AS in_discord "
            "FROM members m "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
            "WHERE m.player_tag = ?",
            (_canon_tag(member_tag),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def format_member_reference(member_or_tag, style="plain_name", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member = member_or_tag if isinstance(member_or_tag, dict) else get_member_identity(member_or_tag, conn=conn)
        if not member:
            return str(member_or_tag)
        name = member.get("member_name") or member.get("current_name") or member.get("player_tag")
        user_id = member.get("discord_user_id")
        username = member.get("discord_username") or member.get("discord_display_name")
        if style == "name_with_mention" and user_id:
            return f"{name} (<@{user_id}>)"
        if style == "name_with_handle" and username:
            handle = username if str(username).startswith("@") else f"@{username}"
            return f"{name} ({handle})"
        return name
    finally:
        if close:
            conn.close()


def save_memory_fact(subject_type, subject_key, fact_type, fact_value, confidence=1.0,
                     source_message_id=None, expires_at=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        now = _utcnow()
        row = conn.execute(
            "SELECT fact_id FROM memory_facts WHERE subject_type = ? AND subject_key = ? AND fact_type = ?",
            (subject_type, subject_key, fact_type),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE memory_facts SET fact_value = ?, confidence = ?, source_message_id = ?, updated_at = ?, expires_at = ? WHERE fact_id = ?",
                (fact_value, confidence, source_message_id, now, expires_at, row["fact_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO memory_facts (subject_type, subject_key, fact_type, fact_value, confidence, source_message_id, created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subject_type, subject_key, fact_type, fact_value, confidence, source_message_id, now, now, expires_at),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


def save_memory_episode(subject_type, subject_key, episode_type, summary, importance=1,
                        source_message_ids=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT INTO memory_episodes (subject_type, subject_key, episode_type, summary, importance, source_message_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subject_type, subject_key, episode_type, summary, importance, _json_or_none(source_message_ids or []), _utcnow()),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def get_memory_facts(subject_type, subject_key, limit=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT fact_type, fact_value, confidence, updated_at, expires_at "
            "FROM memory_facts "
            "WHERE subject_type = ? AND subject_key = ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (subject_type, str(subject_key), _utcnow(), limit),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def get_memory_episodes(subject_type, subject_key, limit=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT episode_type, summary, importance, source_message_ids_json, created_at "
            "FROM memory_episodes "
            "WHERE subject_type = ? AND subject_key = ? "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (subject_type, str(subject_key), limit),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def get_channel_state(channel_id, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT channel_id, last_elixir_post_at, last_topics_json, recent_style_notes_json, last_summary "
            "FROM channel_state WHERE channel_id = ?",
            (str(channel_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def build_memory_context(discord_user_id=None, member_tag=None, channel_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        context = {
            "discord_user": None,
            "member": None,
            "channel": None,
        }
        if discord_user_id is not None:
            key = str(discord_user_id)
            context["discord_user"] = {
                "facts": get_memory_facts("discord_user", key, conn=conn),
                "episodes": get_memory_episodes("discord_user", key, conn=conn),
            }
        if member_tag:
            member = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(member_tag),),
            ).fetchone()
            if member:
                key = str(member["member_id"])
                context["member"] = {
                    "facts": get_memory_facts("member", key, conn=conn),
                    "episodes": get_memory_episodes("member", key, conn=conn),
                }
        if channel_id is not None:
            key = str(channel_id)
            context["channel"] = {
                "state": get_channel_state(channel_id, conn=conn),
                "episodes": get_memory_episodes("channel", key, conn=conn),
            }
        return context
    finally:
        if close:
            conn.close()


# -- Core member state ------------------------------------------------------

def snapshot_members(member_list, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        observed_at = _utcnow()
        today = observed_at[:10]
        bootstrap_snapshot = conn.execute(
            "SELECT COUNT(*) AS cnt FROM member_current_state"
        ).fetchone()["cnt"] == 0
        seen_tags = set()
        for member in member_list:
            tag = _canon_tag(member.get("tag"))
            if not tag:
                continue
            seen_tags.add(tag)
            name = member.get("name") or ""
            member_id = _ensure_member(conn, tag, name=name, status="active")
            previous = conn.execute(
                "SELECT role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api "
                "FROM member_current_state WHERE member_id = ?",
                (member_id,),
            ).fetchone()
            arena = member.get("arena") or {}
            arena_id = arena.get("id") if isinstance(arena, dict) else None
            arena_name = arena.get("name") if isinstance(arena, dict) else str(arena or "")
            arena_raw_name = arena.get("rawName") if isinstance(arena, dict) else None
            last_seen_api = member.get("lastSeen", member.get("last_seen"))
            state = {
                "observed_at": observed_at,
                "role": member.get("role", "member"),
                "exp_level": member.get("expLevel", member.get("exp_level")),
                "trophies": member.get("trophies", 0),
                "best_trophies": member.get("bestTrophies", member.get("best_trophies")),
                "clan_rank": member.get("clanRank", member.get("clan_rank")),
                "previous_clan_rank": member.get("previousClanRank"),
                "donations_week": member.get("donations", 0),
                "donations_received_week": member.get("donationsReceived", member.get("donations_received", 0)),
                "arena_id": arena_id,
                "arena_name": arena_name,
                "arena_raw_name": arena_raw_name,
                "last_seen_api": last_seen_api,
                "source": "clan_api",
                "raw_json": _json_or_none(member),
            }
            state_changed = (
                previous is None
                or previous["role"] != state["role"]
                or previous["exp_level"] != state["exp_level"]
                or previous["trophies"] != state["trophies"]
                or previous["best_trophies"] != state["best_trophies"]
                or previous["clan_rank"] != state["clan_rank"]
                or previous["previous_clan_rank"] != state["previous_clan_rank"]
                or previous["donations_week"] != state["donations_week"]
                or previous["donations_received_week"] != state["donations_received_week"]
                or previous["arena_id"] != state["arena_id"]
                or previous["arena_name"] != state["arena_name"]
                or previous["arena_raw_name"] != state["arena_raw_name"]
                or previous["last_seen_api"] != state["last_seen_api"]
            )
            conn.execute(
                "INSERT INTO member_current_state (member_id, observed_at, role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api, source, raw_json) "
                "VALUES (:member_id, :observed_at, :role, :exp_level, :trophies, :best_trophies, :clan_rank, :previous_clan_rank, :donations_week, :donations_received_week, :arena_id, :arena_name, :arena_raw_name, :last_seen_api, :source, :raw_json) "
                "ON CONFLICT(member_id) DO UPDATE SET observed_at = excluded.observed_at, role = excluded.role, exp_level = excluded.exp_level, trophies = excluded.trophies, best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank, previous_clan_rank = excluded.previous_clan_rank, donations_week = excluded.donations_week, donations_received_week = excluded.donations_received_week, arena_id = excluded.arena_id, arena_name = excluded.arena_name, arena_raw_name = excluded.arena_raw_name, last_seen_api = excluded.last_seen_api, source = excluded.source, raw_json = excluded.raw_json",
                {"member_id": member_id, **state},
            )
            if state_changed:
                conn.execute(
                    "INSERT INTO member_state_snapshots (member_id, observed_at, name, role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        member_id,
                        observed_at,
                        name,
                        state["role"],
                        state["exp_level"],
                        state["trophies"],
                        state["best_trophies"],
                        state["clan_rank"],
                        state["previous_clan_rank"],
                        state["donations_week"],
                        state["donations_received_week"],
                        state["arena_id"],
                        state["arena_name"],
                        state["arena_raw_name"],
                        state["last_seen_api"],
                        state["raw_json"],
                    ),
                )
            conn.execute(
                "INSERT INTO member_daily_metrics (member_id, metric_date, exp_level, trophies, best_trophies, clan_rank, donations_week, donations_received_week, last_seen_api) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, metric_date) DO UPDATE SET exp_level = excluded.exp_level, trophies = excluded.trophies, best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank, donations_week = excluded.donations_week, donations_received_week = excluded.donations_received_week, last_seen_api = excluded.last_seen_api",
                (member_id, today, state["exp_level"], state["trophies"], state["best_trophies"], state["clan_rank"], state["donations_week"], state["donations_received_week"], state["last_seen_api"]),
            )
            if not _get_current_membership(conn, member_id):
                conn.execute(
                    "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, ?, NULL)",
                    (member_id, today, "bootstrap_seed" if bootstrap_snapshot else "clan_api_snapshot"),
                )

        if seen_tags:
            placeholders = ",".join("?" for _ in seen_tags)
            conn.execute(
                f"UPDATE members SET status = CASE WHEN player_tag IN ({placeholders}) THEN 'active' ELSE status END",
                tuple(seen_tags),
            )
        conn.commit()
        return len(seen_tags)
    finally:
        if close:
            conn.close()


def get_active_roster_map(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT player_tag, current_name FROM members WHERE status = 'active' ORDER BY current_name COLLATE NOCASE"
        ).fetchall()
        return {r["player_tag"]: r["current_name"] for r in rows}
    finally:
        if close:
            conn.close()


def get_member_history(tag, days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "SELECT m.player_tag AS tag, s.name, s.trophies, s.best_trophies, s.donations_week AS donations, s.donations_received_week AS donations_received, s.role, s.arena_id, s.arena_name, s.exp_level, s.clan_rank, s.last_seen_api AS last_seen, s.observed_at AS recorded_at "
            "FROM member_state_snapshots s JOIN members m ON m.member_id = s.member_id "
            "WHERE m.player_tag = ? AND s.observed_at >= ? ORDER BY s.observed_at ASC",
            (_canon_tag(tag), cutoff),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def resolve_member(query, status="active", limit=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        query = (query or "").strip()
        if not query:
            return []
        query_lower = query.lower()
        query_handle = query_lower.lstrip("@")
        query_tag = _canon_tag(query) if query.startswith("#") else ""

        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name, m.status, cs.role, cs.exp_level, cs.trophies, cs.clan_rank, "
            "dl.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "WHERE (? IS NULL OR m.status = ?) "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (status, status),
        ).fetchall()
        aliases = {}
        for row in conn.execute(
            "SELECT member_id, alias FROM member_aliases"
        ).fetchall():
            aliases.setdefault(row["member_id"], []).append(row["alias"])

        candidates = []
        for row in rows:
            member = dict(row)
            member["joined_date"] = _current_joined_at(conn, row["member_id"])
            member["in_discord"] = 1 if row["discord_user_id"] else 0
            member_aliases = aliases.get(row["member_id"], [])
            score = 0
            source = None

            name = (member.get("current_name") or "").lower()
            discord_username = (member.get("discord_username") or "").lower()
            discord_display = (member.get("discord_display_name") or "").lower()
            alias_lowers = [a.lower() for a in member_aliases]

            if query_tag and member["player_tag"] == query_tag:
                score, source = 1000, "player_tag_exact"
            elif name == query_lower:
                score, source = 950, "current_name_exact"
            elif query_lower in alias_lowers:
                score, source = 900, "alias_exact"
            elif discord_username == query_handle:
                score, source = 875, "discord_username_exact"
            elif discord_display == query_lower:
                score, source = 850, "discord_display_exact"
            elif name.startswith(query_lower):
                score, source = 775, "current_name_prefix"
            elif any(a.startswith(query_lower) for a in alias_lowers):
                score, source = 750, "alias_prefix"
            elif discord_username.startswith(query_handle) and query_handle:
                score, source = 725, "discord_username_prefix"
            elif query_lower in name:
                score, source = 650, "current_name_contains"
            elif any(query_lower in a for a in alias_lowers):
                score, source = 625, "alias_contains"
            elif query_handle and query_handle in discord_username:
                score, source = 600, "discord_username_contains"
            elif query_lower and query_lower in discord_display:
                score, source = 575, "discord_display_contains"

            if score:
                member["match_score"] = score
                member["match_source"] = source
                member["aliases"] = member_aliases
                candidates.append(_member_reference_fields(conn, row["member_id"], member))

        candidates.sort(
            key=lambda item: (
                -item["match_score"],
                item.get("clan_rank") if item.get("clan_rank") is not None else 999,
                (item.get("current_name") or "").lower(),
            )
        )
        return candidates[:limit]
    finally:
        if close:
            conn.close()


def list_members(status="active", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name, m.status, cs.role, cs.exp_level, cs.trophies, "
            "cs.best_trophies, cs.clan_rank, cs.donations_week, cs.donations_received_week, cs.arena_name, "
            "md.note, md.profile_url, md.poap_address, dl.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN member_metadata md ON md.member_id = m.member_id "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "WHERE m.status = ? "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (status,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["joined_date"] = _current_joined_at(conn, row["member_id"])
            item["in_discord"] = 1 if row["discord_user_id"] else 0
            result.append(_member_reference_fields(conn, row["member_id"], item))
        return result
    finally:
        if close:
            conn.close()


def get_clan_roster_summary(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS active_members, "
            "ROUND(AVG(COALESCE(cs.exp_level, 0)), 2) AS avg_exp_level, "
            "ROUND(AVG(COALESCE(cs.trophies, 0)), 2) AS avg_trophies, "
            "SUM(COALESCE(cs.donations_week, 0)) AS donations_week_total, "
            "MAX(COALESCE(cs.trophies, 0)) AS top_trophies "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchone()
        war = get_current_war_status(conn=conn)
        result = dict(row)
        result["open_slots"] = max(0, 50 - (result["active_members"] or 0))
        if war:
            result["current_war"] = war
        return result
    finally:
        if close:
            conn.close()


def get_member_profile(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name AS member_name, m.status, "
            "cs.observed_at, cs.role, cs.exp_level, cs.trophies, cs.best_trophies, cs.clan_rank, "
            "cs.previous_clan_rank, cs.donations_week, cs.donations_received_week, cs.arena_name, cs.last_seen_api, "
            "md.birth_month, md.birth_day, md.profile_url, md.poap_address, md.note, "
            "dl.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN member_metadata md ON md.member_id = m.member_id "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "WHERE m.player_tag = ?",
            (_canon_tag(tag),),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["joined_date"] = _current_joined_at(conn, row["member_id"])
        result["in_discord"] = 1 if row["discord_user_id"] else 0
        _member_reference_fields(conn, row["member_id"], result)
        recent_form = get_member_recent_form(tag, conn=conn)
        if recent_form:
            result["recent_form"] = recent_form
        deck = get_member_current_deck(tag, conn=conn)
        if deck:
            result["current_deck"] = deck
        cards = get_member_signature_cards(tag, conn=conn)
        if cards:
            result["signature_cards"] = cards
        return result
    finally:
        if close:
            conn.close()


def get_member_overview(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        profile = get_member_profile(tag, conn=conn)
        if not profile:
            return None
        overview = dict(profile)
        overview["war_status"] = get_member_war_status(tag, conn=conn)
        return overview
    finally:
        if close:
            conn.close()


def list_longest_tenure_members(limit=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        today = datetime.now(timezone.utc).date()
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, cs.clan_rank "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchall()
        result = []
        for row in rows:
            joined_date = _current_joined_at(conn, row["member_id"])
            if not joined_date:
                continue
            joined_day = joined_date[:10]
            try:
                tenure_days = (today - datetime.strptime(joined_day, "%Y-%m-%d").date()).days
            except ValueError:
                tenure_days = None
            item = dict(row)
            item["joined_date"] = joined_day
            item["tenure_days"] = tenure_days
            result.append(_member_reference_fields(conn, row["member_id"], item))
        result.sort(
            key=lambda item: (
                item["joined_date"],
                (item.get("name") or "").lower(),
            )
        )
        return result[:limit]
    finally:
        if close:
            conn.close()


def list_recent_joins(days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days))
        season_id = get_current_season_id(conn=conn)
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, cs.clan_rank "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchall()
        result = []
        for row in rows:
            joined_date = _current_joined_at(conn, row["member_id"])
            if not joined_date:
                continue
            joined_day = joined_date[:10]
            try:
                joined_dt = datetime.strptime(joined_day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if joined_dt < cutoff:
                continue
            item = dict(row)
            item["joined_date"] = joined_day
            form = conn.execute(
                "SELECT wins, losses, sample_size, form_label FROM member_recent_form WHERE member_id = ? AND scope = 'competitive_10'",
                (row["member_id"],),
            ).fetchone()
            if form:
                item["recent_form"] = dict(form)
            if season_id is not None:
                war = conn.execute(
                    "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame "
                    "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                    "WHERE wr.season_id = ? AND wp.member_id = ?",
                    (season_id, row["member_id"]),
                ).fetchone()
                item["current_season_war"] = dict(war)
            result.append(_member_reference_fields(conn, row["member_id"], item))
        result.sort(
            key=lambda item: (
                item["joined_date"],
                (item.get("name") or "").lower(),
            ),
            reverse=True,
        )
        return result
    finally:
        if close:
            conn.close()


def get_member_current_deck(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT p.current_deck_json, p.fetched_at "
            "FROM player_profile_snapshots p "
            "JOIN members m ON m.member_id = p.member_id "
            "WHERE m.player_tag = ? "
            "ORDER BY p.fetched_at DESC LIMIT 1",
            (_canon_tag(tag),),
        ).fetchone()
        if not row or not row["current_deck_json"]:
            return None
        return {
            "fetched_at": row["fetched_at"],
            "cards": json.loads(row["current_deck_json"]),
        }
    finally:
        if close:
            conn.close()


def get_member_signature_cards(tag, mode_scope="overall", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT cards_json, sample_battles, fetched_at FROM member_card_usage_snapshots s "
            "JOIN members m ON m.member_id = s.member_id "
            "WHERE m.player_tag = ? AND s.mode_scope = ? "
            "ORDER BY s.fetched_at DESC LIMIT 1",
            (_canon_tag(tag), mode_scope),
        ).fetchone()
        if not row:
            return None
        return {
            "mode_scope": mode_scope,
            "sample_battles": row["sample_battles"],
            "fetched_at": row["fetched_at"],
            "cards": json.loads(row["cards_json"]),
        }
    finally:
        if close:
            conn.close()


def get_member_recent_form(tag, scope="competitive_10", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT f.scope, f.sample_size, f.wins, f.losses, f.draws, f.current_streak, "
            "f.current_streak_type, f.win_rate, f.avg_crown_diff, f.avg_trophy_change, f.form_label, f.summary, f.computed_at "
            "FROM member_recent_form f "
            "JOIN members m ON m.member_id = f.member_id "
            "WHERE m.player_tag = ? AND f.scope = ?",
            (_canon_tag(tag), scope),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def get_members_on_losing_streak(min_streak=3, scope="competitive_10", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, cs.clan_rank, cs.role, "
            "f.current_streak, f.current_streak_type, f.wins, f.losses, f.sample_size, f.form_label, f.summary "
            "FROM member_recent_form f "
            "JOIN members m ON m.member_id = f.member_id "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' AND f.scope = ? AND f.current_streak_type = 'L' AND f.current_streak >= ? "
            "ORDER BY f.current_streak DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE",
            (scope, min_streak),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            tag = item.get("tag")
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(tag),),
            ).fetchone()["member_id"]
            result.append(_member_reference_fields(conn, member_id, item))
        return result
    finally:
        if close:
            conn.close()


def get_current_war_status(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        war = conn.execute(
            "SELECT observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score "
            "FROM war_current_state ORDER BY observed_at DESC, war_id DESC LIMIT 1"
        ).fetchone()
        if not war:
            return None
        season_id = get_current_season_id(conn=conn)
        current_race = None
        if season_id is not None:
            current_race = conn.execute(
                "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time "
                "FROM war_races WHERE season_id = ? ORDER BY section_index DESC LIMIT 1",
                (season_id,),
            ).fetchone()
        result = dict(war)
        if current_race:
            result["season_id"] = current_race["season_id"]
            result["section_index"] = current_race["section_index"]
            result["week"] = current_race["section_index"] + 1 if current_race["section_index"] is not None else None
            result["race_rank"] = current_race["our_rank"]
            result["trophy_change"] = current_race["trophy_change"]
        return result
    finally:
        if close:
            conn.close()


def get_members_without_war_participation(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return {"season_id": None, "members": []}
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.clan_rank "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM war_participation wp "
            "  JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "  WHERE wr.season_id = ? AND wp.member_id = m.member_id AND COALESCE(wp.decks_used, 0) > 0"
            ") "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (season_id,),
        ).fetchall()
        members = []
        for row in rows:
            item = dict(row)
            item["joined_date"] = _current_joined_at(conn, row["member_id"])
            members.append(_member_reference_fields(conn, row["member_id"], item))
        return {"season_id": season_id, "members": members}
    finally:
        if close:
            conn.close()


def get_war_deck_status_today(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        today = _utcnow()[:10]
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, w.decks_used_today, w.decks_used_total, w.fame "
            "FROM war_day_status w JOIN members m ON m.member_id = w.member_id "
            "WHERE w.battle_date = ? AND m.status = 'active' "
            "ORDER BY COALESCE(w.decks_used_today, 0) DESC, m.current_name COLLATE NOCASE",
            (today,),
        ).fetchall()
        used_all = []
        used_some = []
        used_none = []
        for row in rows:
            item = dict(row)
            decks_today = item.get("decks_used_today") or 0
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(item["tag"]),),
            ).fetchone()["member_id"]
            item = _member_reference_fields(conn, member_id, item)
            if decks_today >= 4:
                used_all.append(item)
            elif decks_today > 0:
                used_some.append(item)
            else:
                used_none.append(item)
        return {
            "battle_date": today,
            "used_all_4": used_all,
            "used_some": used_some,
            "used_none": used_none,
            "total_participants": len(rows),
        }
    finally:
        if close:
            conn.close()


def get_war_season_summary(season_id=None, top_n=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return None
        total_races = conn.execute(
            "SELECT COUNT(*) AS cnt, SUM(COALESCE(our_fame, 0)) AS total_clan_fame "
            "FROM war_races WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        top = get_war_champ_standings(season_id=season_id, conn=conn)[:top_n]
        nonparticipants = get_members_without_war_participation(season_id=season_id, conn=conn)["members"]
        active_members = conn.execute(
            "SELECT COUNT(*) AS cnt FROM members WHERE status = 'active'"
        ).fetchone()["cnt"]
        return {
            "season_id": season_id,
            "races": total_races["cnt"],
            "total_clan_fame": total_races["total_clan_fame"] or 0,
            "fame_per_active_member": round((total_races["total_clan_fame"] or 0) / active_members, 2) if active_members else 0,
            "top_contributors": top,
            "nonparticipants": nonparticipants,
        }
    finally:
        if close:
            conn.close()


def get_member_war_status(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        current_day = None
        today = _utcnow()[:10]
        current_day_row = conn.execute(
            "SELECT w.battle_date, w.decks_used_today, w.decks_used_total, w.fame, w.repair_points "
            "FROM war_day_status w JOIN members m ON m.member_id = w.member_id "
            "WHERE m.player_tag = ? AND w.battle_date = ?",
            (canon_tag, today),
        ).fetchone()
        if current_day_row:
            current_day = dict(current_day_row)
            current_day["decks_left_today"] = max(0, 4 - (current_day["decks_used_today"] or 0))

        summary = {
            "season_id": season_id,
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "current_day": current_day,
            "season": None,
        }
        if season_id is not None:
            season_row = conn.execute(
                "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
                "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used, AVG(COALESCE(wp.fame, 0)) AS avg_fame "
                "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.player_tag = ?",
                (season_id, canon_tag),
            ).fetchone()
            total_races = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?",
                (season_id,),
            ).fetchone()["cnt"]
            season = dict(season_row)
            season["total_races_in_season"] = total_races
            season["participation_rate"] = round((season["races_played"] or 0) / total_races, 4) if total_races else 0
            summary["season"] = season
        return summary
    finally:
        if close:
            conn.close()


def compare_member_war_to_clan_average(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return None
        member = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name "
            "FROM members m WHERE m.player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member:
            return None
        total_races = conn.execute(
            "SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?",
            (season_id,),
        ).fetchone()["cnt"]
        active_members = conn.execute(
            "SELECT COUNT(*) AS cnt FROM members WHERE status = 'active'"
        ).fetchone()["cnt"]
        member_stats = conn.execute(
            "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
            "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used, AVG(COALESCE(wp.fame, 0)) AS avg_fame_per_race "
            "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "WHERE wr.season_id = ? AND wp.player_tag = ?",
            (season_id, canon_tag),
        ).fetchone()
        clan_avgs = conn.execute(
            "SELECT AVG(member_total_fame) AS avg_total_fame, AVG(member_races_played) AS avg_races_played, "
            "AVG(member_avg_fame) AS avg_fame_per_participant, AVG(member_total_decks) AS avg_total_decks "
            "FROM ("
            "  SELECT wp.player_tag, SUM(COALESCE(wp.fame, 0)) AS member_total_fame, "
            "         COUNT(*) AS member_races_played, AVG(COALESCE(wp.fame, 0)) AS member_avg_fame, "
            "         SUM(COALESCE(wp.decks_used, 0)) AS member_total_decks "
            "  FROM war_participation wp "
            "  JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "  JOIN members m ON m.member_id = wp.member_id "
            "  WHERE wr.season_id = ? AND m.status = 'active' "
            "  GROUP BY wp.player_tag"
            ")",
            (season_id,),
        ).fetchone()
        return {
            "season_id": season_id,
            "member": {
                "tag": member["tag"],
                "name": member["name"],
                "member_ref": format_member_reference(member["tag"], style="name_with_handle", conn=conn),
                "races_played": member_stats["races_played"] or 0,
                "total_fame": member_stats["total_fame"] or 0,
                "total_decks_used": member_stats["total_decks_used"] or 0,
                "avg_fame_per_race": round(member_stats["avg_fame_per_race"] or 0, 2),
                "participation_rate": round((member_stats["races_played"] or 0) / total_races, 4) if total_races else 0,
            },
            "clan_average": {
                "active_members": active_members,
                "participants_with_data": conn.execute(
                    "SELECT COUNT(DISTINCT wp.player_tag) AS cnt "
                    "FROM war_participation wp "
                    "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                    "JOIN members m ON m.member_id = wp.member_id "
                    "WHERE wr.season_id = ? AND m.status = 'active'",
                    (season_id,),
                ).fetchone()["cnt"],
                "avg_total_fame": round(clan_avgs["avg_total_fame"] or 0, 2),
                "avg_races_played": round(clan_avgs["avg_races_played"] or 0, 2),
                "avg_fame_per_participant": round(clan_avgs["avg_fame_per_participant"] or 0, 2),
                "avg_total_decks": round(clan_avgs["avg_total_decks"] or 0, 2),
            },
        }
    finally:
        if close:
            conn.close()


def get_members_at_risk(inactivity_days=7, min_donations_week=20, require_war_participation=False,
                        min_war_races=1, tenure_grace_days=14, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        today = datetime.now(timezone.utc).date()
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, "
            "cs.clan_rank, cs.donations_week, cs.last_seen_api "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE"
        ).fetchall()

        flagged = []
        for row in rows:
            joined_date = _current_joined_at(conn, row["member_id"])
            tenure_days = None
            if joined_date:
                try:
                    tenure_days = (today - datetime.strptime(joined_date[:10], "%Y-%m-%d").date()).days
                except ValueError:
                    tenure_days = None
            if tenure_days is not None and tenure_days < tenure_grace_days:
                continue

            reasons = []
            last_seen_dt = _parse_cr_time(row["last_seen_api"])
            if last_seen_dt is not None:
                days_inactive = (today - last_seen_dt.date()).days
                if days_inactive >= inactivity_days:
                    reasons.append({
                        "type": "inactive",
                        "detail": f"last seen {days_inactive} days ago",
                        "value": days_inactive,
                    })

            donations_week = row["donations_week"] or 0
            if donations_week < min_donations_week:
                reasons.append({
                    "type": "low_donations",
                    "detail": f"{donations_week} donations this week",
                    "value": donations_week,
                })

            war_races_played = None
            if require_war_participation and season_id is not None:
                war_races_played = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM war_participation wp "
                    "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                    "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                    (season_id, row["member_id"]),
                ).fetchone()["cnt"]
                if war_races_played < min_war_races:
                    reasons.append({
                        "type": "low_war_participation",
                        "detail": f"{war_races_played} war races played this season",
                        "value": war_races_played,
                    })

            if reasons:
                item = dict(row)
                item["joined_date"] = joined_date
                item["tenure_days"] = tenure_days
                item["risk_score"] = len(reasons)
                item["reasons"] = reasons
                if war_races_played is not None:
                    item["war_races_played"] = war_races_played
                flagged.append(_member_reference_fields(conn, row["member_id"], item))

        flagged.sort(
            key=lambda item: (
                -item["risk_score"],
                item.get("clan_rank") if item.get("clan_rank") is not None else 999,
                (item.get("name") or "").lower(),
            )
        )
        return {
            "season_id": season_id,
            "criteria": {
                "inactivity_days": inactivity_days,
                "min_donations_week": min_donations_week,
                "require_war_participation": require_war_participation,
                "min_war_races": min_war_races,
                "tenure_grace_days": tenure_grace_days,
            },
            "members": flagged,
        }
    finally:
        if close:
            conn.close()


def get_trending_war_contributors(season_id=None, recent_races=2, limit=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return {"season_id": None, "members": []}

        race_rows = conn.execute(
            "SELECT war_race_id, section_index FROM war_races WHERE season_id = ? ORDER BY section_index DESC",
            (season_id,),
        ).fetchall()
        if not race_rows:
            return {"season_id": season_id, "members": []}
        recent_ids = [row["war_race_id"] for row in race_rows[:recent_races]]
        prior_ids = [row["war_race_id"] for row in race_rows[recent_races:]]

        placeholders_recent = ",".join("?" for _ in recent_ids)
        recent_totals = conn.execute(
            f"SELECT wp.member_id, wp.player_tag AS tag, MAX(wp.player_name) AS name, "
            f"SUM(COALESCE(wp.fame, 0)) AS recent_fame, COUNT(*) AS recent_races "
            f"FROM war_participation wp "
            f"JOIN members m ON m.member_id = wp.member_id "
            f"WHERE wp.war_race_id IN ({placeholders_recent}) AND m.status = 'active' "
            f"GROUP BY wp.member_id, wp.player_tag",
            tuple(recent_ids),
        ).fetchall()

        prior_map = {}
        if prior_ids:
            placeholders_prior = ",".join("?" for _ in prior_ids)
            prior_rows = conn.execute(
                f"SELECT wp.member_id, wp.player_tag AS tag, SUM(COALESCE(wp.fame, 0)) AS prior_fame, COUNT(*) AS prior_races "
                f"FROM war_participation wp "
                f"JOIN members m ON m.member_id = wp.member_id "
                f"WHERE wp.war_race_id IN ({placeholders_prior}) AND m.status = 'active' "
                f"GROUP BY wp.member_id, wp.player_tag",
                tuple(prior_ids),
            ).fetchall()
            for row in prior_rows:
                prior_map[(row["member_id"], row["tag"])] = dict(row)

        members = []
        for row in recent_totals:
            recent_avg = (row["recent_fame"] or 0) / row["recent_races"] if row["recent_races"] else 0
            prior = prior_map.get((row["member_id"], row["tag"]), {})
            prior_avg = (prior.get("prior_fame") or 0) / prior.get("prior_races", 1) if prior.get("prior_races") else 0
            item = {
                "tag": row["tag"],
                "name": row["name"],
                "recent_fame": row["recent_fame"] or 0,
                "recent_races": row["recent_races"] or 0,
                "recent_avg_fame": round(recent_avg, 2),
                "prior_avg_fame": round(prior_avg, 2),
                "trend_delta": round(recent_avg - prior_avg, 2),
            }
            if row["member_id"] is not None:
                item = _member_reference_fields(conn, row["member_id"], item)
            members.append(item)

        members.sort(
            key=lambda item: (
                -item["trend_delta"],
                -item["recent_fame"],
                (item.get("name") or "").lower(),
            )
        )
        return {
            "season_id": season_id,
            "recent_races_considered": min(recent_races, len(race_rows)),
            "members": members[:limit],
        }
    finally:
        if close:
            conn.close()


def get_trophy_drops(days=7, min_drop=100, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, "
            "MIN(dm.trophies) AS min_trophies, MAX(dm.trophies) AS max_trophies, "
            "MAX(dm.metric_date) AS latest_metric_date, "
            "(MAX(dm.trophies) - MIN(dm.trophies)) AS spread "
            "FROM member_daily_metrics dm "
            "JOIN members m ON m.member_id = dm.member_id "
            "WHERE dm.metric_date >= ? AND m.status = 'active' "
            "GROUP BY dm.member_id "
            "HAVING spread >= ? "
            "ORDER BY spread DESC",
            (cutoff, min_drop),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["drop"] = item.pop("spread")
            result.append(item)
        return result
    finally:
        if close:
            conn.close()


def get_trophy_changes(since_hours=24, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT m.player_tag AS tag, s.name, s.trophies, s.observed_at,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at ASC) AS rn_asc,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn_desc,
                    s.member_id
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
                WHERE s.observed_at >= ?
            )
            SELECT a.tag, a.name,
                   a.trophies AS old_trophies,
                   b.trophies AS new_trophies,
                   (b.trophies - a.trophies) AS change
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND a.trophies != b.trophies
            ORDER BY ABS(change) DESC
            """,
            (cutoff,),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def detect_milestones(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT s.*, m.player_tag AS tag,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
            )
            SELECT a.tag, a.name,
                   b.trophies AS old_trophies, a.trophies AS new_trophies,
                   b.arena_name AS old_arena, a.arena_name AS new_arena
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn = 1 AND b.rn = 2
            """
        ).fetchall()
        milestones = []
        for row in rows:
            old_t = row["old_trophies"] or 0
            new_t = row["new_trophies"] or 0
            for threshold in TROPHY_MILESTONES:
                if old_t < threshold <= new_t:
                    milestones.append({
                        "tag": row["tag"],
                        "name": row["name"],
                        "type": "trophy_milestone",
                        "old_value": old_t,
                        "new_value": new_t,
                        "milestone": threshold,
                    })
            if row["old_arena"] and row["new_arena"] and row["old_arena"] != row["new_arena"]:
                milestones.append({
                    "tag": row["tag"],
                    "name": row["name"],
                    "type": "arena_change",
                    "old_value": row["old_arena"],
                    "new_value": row["new_arena"],
                })
        return milestones
    finally:
        if close:
            conn.close()


def detect_role_changes(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT s.*, m.player_tag AS tag,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
            )
            SELECT a.tag, a.name, b.role AS old_role, a.role AS new_role
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn = 1 AND b.rn = 2 AND COALESCE(a.role, '') != COALESCE(b.role, '')
            """
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


# -- War --------------------------------------------------------------------

def store_war_log(race_log, clan_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        clan_tag = _tag_key(clan_tag)
        _store_raw_payload(conn, "clan_war_log", clan_tag, race_log)
        stored = 0
        for entry in (race_log or {}).get("items", []):
            season_id = entry.get("seasonId")
            section_index = entry.get("sectionIndex")
            standings = entry.get("standings", [])
            our = None
            for standing in standings:
                clan = standing.get("clan", {})
                if _tag_key(clan.get("tag")) == clan_tag:
                    our = standing
                    break
            total_clans = len(standings)
            trophy_change = our.get("trophyChange") if our else None
            our_rank = our.get("rank") if our else None
            clan = (our or {}).get("clan", {})
            cur = conn.execute(
                "INSERT OR IGNORE INTO war_races (season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (season_id, section_index, entry.get("createdDate"), our_rank, trophy_change, clan.get("fame"), total_clans, clan.get("finishTime"), _json_or_none(entry)),
            )
            if cur.rowcount == 0:
                race_row = conn.execute("SELECT war_race_id FROM war_races WHERE season_id = ? AND section_index = ?", (season_id, section_index)).fetchone()
                war_race_id = race_row["war_race_id"]
            else:
                war_race_id = cur.lastrowid
                stored += 1

            if our:
                for participant in clan.get("participants", []):
                    ptag = _canon_tag(participant.get("tag"))
                    member_id = _ensure_member(conn, ptag, participant.get("name"), status=None) if ptag else None
                    conn.execute(
                        "INSERT OR REPLACE INTO war_participation (war_race_id, member_id, player_tag, player_name, fame, repair_points, boat_attacks, decks_used, decks_used_today, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (war_race_id, member_id, ptag, participant.get("name"), participant.get("fame", 0), participant.get("repairPoints", 0), participant.get("boatAttacks", 0), participant.get("decksUsed", 0), participant.get("decksUsedToday", 0), _json_or_none(participant)),
                    )
        conn.commit()
        return stored
    finally:
        if close:
            conn.close()


def get_war_history(n=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT war_race_id AS id, season_id, section_index, our_rank, our_fame, finish_time, created_date, raw_json AS standings_json FROM war_races ORDER BY created_date DESC LIMIT ?",
            (n,),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def get_member_war_stats(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT wp.participation_id AS id, wp.player_tag AS tag, wp.player_name AS name, wp.fame, wp.repair_points, wp.decks_used, wr.season_id, wr.section_index, wr.our_rank, wr.created_date FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id WHERE wp.player_tag = ? ORDER BY wr.created_date DESC",
            (_canon_tag(tag),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(tag),),
            ).fetchone()
            if member_id:
                item = _member_reference_fields(conn, member_id["member_id"], item)
            result.append(item)
        return result
    finally:
        if close:
            conn.close()


def get_war_champ_standings(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return []
        rows = conn.execute(
            "SELECT wp.player_tag AS tag, MAX(m.current_name) AS name, SUM(COALESCE(wp.fame, 0)) AS total_fame, COUNT(*) AS races_participated, ROUND(AVG(COALESCE(wp.fame, 0)), 0) AS avg_fame "
            "FROM war_participation wp "
            "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "JOIN members m ON m.member_id = wp.member_id "
            "WHERE wr.season_id = ? AND m.status = 'active' AND COALESCE(wp.fame, 0) > 0 "
            "GROUP BY wp.player_tag ORDER BY total_fame DESC, races_participated DESC",
            (season_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            member = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(item["tag"]),),
            ).fetchone()
            if member:
                item = _member_reference_fields(conn, member["member_id"], item)
            result.append(item)
        return result
    finally:
        if close:
            conn.close()


def get_current_season_id(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT MAX(season_id) AS sid FROM war_races").fetchone()
        return row["sid"] if row else None
    finally:
        if close:
            conn.close()


def _season_bounds(conn: sqlite3.Connection, season_id: int) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT MIN(created_date) AS start_date, MAX(created_date) AS end_date "
        "FROM war_races WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    if not row or not row["start_date"] or not row["end_date"]:
        return None, None
    start_dt = _parse_cr_time(row["start_date"])
    end_dt = _parse_cr_time(row["end_date"])
    if not start_dt or not end_dt:
        return None, None
    end_dt = end_dt + timedelta(days=7)
    return start_dt.strftime("%Y%m%dT%H%M%S.000Z"), end_dt.strftime("%Y%m%dT%H%M%S.000Z")


def get_perfect_war_participants(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return []
        total_row = conn.execute("SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?", (season_id,)).fetchone()
        total_races = total_row["cnt"] if total_row else 0
        if total_races == 0:
            return []
        rows = conn.execute(
            "SELECT wp.player_tag AS tag, MAX(m.current_name) AS name, COUNT(*) AS races_participated, SUM(COALESCE(wp.fame, 0)) AS total_fame "
            "FROM war_participation wp "
            "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "JOIN members m ON m.member_id = wp.member_id "
            "WHERE wr.season_id = ? AND m.status = 'active' AND COALESCE(wp.decks_used, 0) > 0 "
            "GROUP BY wp.player_tag HAVING COUNT(*) = ? ORDER BY total_fame DESC",
            (season_id, total_races),
        ).fetchall()
        result = []
        for row in rows:
            item = {**dict(row), "total_races_in_season": total_races}
            member = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(item["tag"]),),
            ).fetchone()
            if member:
                item = _member_reference_fields(conn, member["member_id"], item)
            result.append(item)
        return result
    finally:
        if close:
            conn.close()


def get_recent_role_changes(days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, "
            "curr.role AS new_role, prev.role AS old_role, curr.observed_at AS changed_at "
            "FROM member_state_snapshots curr "
            "JOIN member_state_snapshots prev ON prev.member_id = curr.member_id "
            "JOIN members m ON m.member_id = curr.member_id "
            "WHERE curr.observed_at >= ? "
            "AND prev.observed_at = ("
            "  SELECT MAX(p2.observed_at) FROM member_state_snapshots p2 "
            "  WHERE p2.member_id = curr.member_id AND p2.observed_at < curr.observed_at"
            ") "
            "AND COALESCE(curr.role, '') != COALESCE(prev.role, '') "
            "ORDER BY curr.observed_at DESC",
            (cutoff,),
        ).fetchall()
        seen = set()
        result = []
        for row in rows:
            if row["tag"] in seen:
                continue
            seen.add(row["tag"])
            result.append(_member_reference_fields(conn, row["member_id"], dict(row)))
        return result
    finally:
        if close:
            conn.close()


def get_member_war_attendance(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        member = conn.execute(
            "SELECT member_id, current_name FROM members WHERE player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member:
            return None
        total_races = 0
        season_row = None
        if season_id is not None:
            total_races = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?",
                (season_id,),
            ).fetchone()["cnt"]
            season_row = conn.execute(
                "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
                "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used "
                "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                (season_id, member["member_id"]),
            ).fetchone()

        four_week_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=28)).strftime("%Y%m%dT%H%M%S.000Z")
        recent_total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM war_races WHERE created_date >= ?",
            (four_week_cutoff,),
        ).fetchone()["cnt"]
        recent_played = conn.execute(
            "SELECT COUNT(*) AS cnt "
            "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "WHERE wr.created_date >= ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
            (four_week_cutoff, member["member_id"]),
        ).fetchone()["cnt"]
        return {
            "season_id": season_id,
            "tag": canon_tag,
            "name": member["current_name"],
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "season": {
                "races_played": season_row["races_played"] if season_row else 0,
                "total_races": total_races,
                "participation_rate": round((season_row["races_played"] or 0) / total_races, 4) if season_row and total_races else 0,
                "total_fame": season_row["total_fame"] if season_row else 0,
                "total_decks_used": season_row["total_decks_used"] if season_row else 0,
                "races_missed": max(0, total_races - (season_row["races_played"] or 0)) if season_row else total_races,
            },
            "last_4_weeks": {
                "races_played": recent_played or 0,
                "total_races": recent_total or 0,
                "participation_rate": round((recent_played or 0) / recent_total, 4) if recent_total else 0,
            },
        }
    finally:
        if close:
            conn.close()


def get_member_war_battle_record(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        member = conn.execute(
            "SELECT member_id, current_name FROM members WHERE player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member:
            return None
        start_bound, end_bound = _season_bounds(conn, season_id) if season_id is not None else (None, None)
        where = ["member_id = ?", "is_war = 1"]
        params = [member["member_id"]]
        if start_bound and end_bound:
            where.extend(["battle_time >= ?", "battle_time < ?"])
            params.extend([start_bound, end_bound])
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
            "SUM(CASE WHEN outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
            "COUNT(*) AS battles "
            f"FROM member_battle_facts WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        draws = row["draws"] or 0
        battles = row["battles"] or 0
        return {
            "season_id": season_id,
            "tag": canon_tag,
            "name": member["current_name"],
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "battles": battles,
            "win_rate": round(wins / battles, 4) if battles else 0,
        }
    finally:
        if close:
            conn.close()


def get_war_battle_win_rates(season_id=None, limit=10, min_battles=1, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return {"season_id": None, "members": []}
        start_bound, end_bound = _season_bounds(conn, season_id)
        if not start_bound or not end_bound:
            return {"season_id": season_id, "members": []}
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, "
            "SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN bf.outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
            "SUM(CASE WHEN bf.outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
            "COUNT(*) AS battles "
            "FROM member_battle_facts bf "
            "JOIN members m ON m.member_id = bf.member_id "
            "WHERE m.status = 'active' AND bf.is_war = 1 AND bf.battle_time >= ? AND bf.battle_time < ? "
            "GROUP BY m.member_id "
            "HAVING COUNT(*) >= ? "
            "ORDER BY CAST(SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) DESC, COUNT(*) DESC, m.current_name COLLATE NOCASE",
            (start_bound, end_bound, min_battles),
        ).fetchall()
        members = []
        for row in rows[:limit]:
            item = dict(row)
            item["win_rate"] = round((item["wins"] or 0) / item["battles"], 4) if item["battles"] else 0
            members.append(_member_reference_fields(conn, row["member_id"], item))
        return {
            "season_id": season_id,
            "min_battles": min_battles,
            "members": members,
        }
    finally:
        if close:
            conn.close()


def get_clan_boat_battle_record(wars=3, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        race_rows = conn.execute(
            "SELECT war_race_id, season_id, section_index, created_date "
            "FROM war_races WHERE created_date IS NOT NULL "
            "ORDER BY created_date DESC LIMIT ?",
            (wars,),
        ).fetchall()
        if not race_rows:
            return {"wars_considered": 0, "wins": 0, "losses": 0, "draws": 0, "battles": 0, "per_war": []}

        selected = list(reversed(race_rows))
        per_war = []
        wins = losses = draws = battles = 0
        for idx, row in enumerate(selected):
            start_dt = _parse_cr_time(row["created_date"])
            if not start_dt:
                continue
            if idx + 1 < len(selected):
                end_dt = _parse_cr_time(selected[idx + 1]["created_date"])
            else:
                end_dt = start_dt + timedelta(days=7)
            if not end_dt:
                end_dt = start_dt + timedelta(days=7)
            start_key = start_dt.strftime("%Y%m%dT%H%M%S.000Z")
            end_key = end_dt.strftime("%Y%m%dT%H%M%S.000Z")
            stats = conn.execute(
                "SELECT "
                "SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
                "SUM(CASE WHEN outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
                "COUNT(*) AS battles "
                "FROM member_battle_facts "
                "WHERE battle_type = 'boatBattle' AND battle_time >= ? AND battle_time < ?",
                (start_key, end_key),
            ).fetchone()
            item = {
                "season_id": row["season_id"],
                "section_index": row["section_index"],
                "wins": stats["wins"] or 0,
                "losses": stats["losses"] or 0,
                "draws": stats["draws"] or 0,
                "battles": stats["battles"] or 0,
            }
            per_war.append(item)
            wins += item["wins"]
            losses += item["losses"]
            draws += item["draws"]
            battles += item["battles"]
        return {
            "wars_considered": len(per_war),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "battles": battles,
            "per_war": list(reversed(per_war)),
        }
    finally:
        if close:
            conn.close()


def get_war_score_trend(days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        first = conn.execute(
            "SELECT observed_at, clan_score, fame, war_state FROM war_current_state "
            "WHERE observed_at >= ? AND clan_score IS NOT NULL ORDER BY observed_at ASC LIMIT 1",
            (cutoff,),
        ).fetchone()
        last = conn.execute(
            "SELECT observed_at, clan_score, fame, war_state FROM war_current_state "
            "WHERE observed_at >= ? AND clan_score IS NOT NULL ORDER BY observed_at DESC LIMIT 1",
            (cutoff,),
        ).fetchone()
        race_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S.000Z")
        race_stats = conn.execute(
            "SELECT COUNT(*) AS races, SUM(COALESCE(trophy_change, 0)) AS trophy_change_total, "
            "AVG(COALESCE(our_rank, 0)) AS avg_rank, AVG(COALESCE(our_fame, 0)) AS avg_fame "
            "FROM war_races WHERE created_date >= ?",
            (race_cutoff,),
        ).fetchone()
        if not first or not last:
            return {
                "window_days": days,
                "direction": "unknown",
                "score_change": None,
                "trophy_change_total": race_stats["trophy_change_total"] or 0,
                "races": race_stats["races"] or 0,
            }
        score_change = (last["clan_score"] or 0) - (first["clan_score"] or 0)
        direction = "flat"
        if score_change > 0:
            direction = "up"
        elif score_change < 0:
            direction = "down"
        return {
            "window_days": days,
            "direction": direction,
            "start": dict(first),
            "end": dict(last),
            "score_change": score_change,
            "trophy_change_total": race_stats["trophy_change_total"] or 0,
            "races": race_stats["races"] or 0,
            "avg_rank": round(race_stats["avg_rank"] or 0, 2) if race_stats["races"] else None,
            "avg_fame": round(race_stats["avg_fame"] or 0, 2) if race_stats["races"] else None,
        }
    finally:
        if close:
            conn.close()


def compare_fame_per_member_to_previous_season(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        if season_id is None:
            return None
        previous_row = conn.execute(
            "SELECT MAX(season_id) AS season_id FROM war_races WHERE season_id < ?",
            (season_id,),
        ).fetchone()
        previous_season_id = previous_row["season_id"] if previous_row else None
        if previous_season_id is None:
            return {
                "current_season_id": season_id,
                "previous_season_id": None,
                "current": None,
                "previous": None,
                "direction": "unknown",
                "delta": None,
            }

        def _season_stats(target_season_id):
            row = conn.execute(
                "SELECT COUNT(*) AS races, SUM(COALESCE(our_fame, 0)) AS total_fame "
                "FROM war_races WHERE season_id = ?",
                (target_season_id,),
            ).fetchone()
            participants = conn.execute(
                "SELECT COUNT(DISTINCT player_tag) AS cnt "
                "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                (target_season_id,),
            ).fetchone()["cnt"]
            total_fame = row["total_fame"] or 0
            return {
                "season_id": target_season_id,
                "races": row["races"] or 0,
                "participants": participants or 0,
                "total_fame": total_fame,
                "fame_per_member": round(total_fame / participants, 2) if participants else 0,
            }

        current = _season_stats(season_id)
        previous = _season_stats(previous_season_id)
        delta = current["fame_per_member"] - previous["fame_per_member"]
        direction = "flat"
        if delta > 0:
            direction = "up"
        elif delta < 0:
            direction = "down"
        return {
            "current_season_id": season_id,
            "previous_season_id": previous_season_id,
            "current": current,
            "previous": previous,
            "direction": direction,
            "delta": round(delta, 2),
        }
    finally:
        if close:
            conn.close()


def get_member_missed_war_days(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        member = conn.execute(
            "SELECT member_id, current_name FROM members WHERE player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member or season_id is None:
            return None
        start_bound, end_bound = _season_bounds(conn, season_id)
        if not start_bound or not end_bound:
            return None
        start_dt = _parse_cr_time(start_bound)
        end_dt = _parse_cr_time(end_bound)
        tracked_days = conn.execute(
            "SELECT DISTINCT battle_date FROM war_day_status WHERE battle_date >= ? AND battle_date < ? ORDER BY battle_date",
            (start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")),
        ).fetchall()
        missed = []
        participated = 0
        for row in tracked_days:
            status = conn.execute(
                "SELECT decks_used_today FROM war_day_status WHERE member_id = ? AND battle_date = ?",
                (member["member_id"], row["battle_date"]),
            ).fetchone()
            if status and (status["decks_used_today"] or 0) > 0:
                participated += 1
            else:
                missed.append(row["battle_date"])
        return {
            "season_id": season_id,
            "tag": canon_tag,
            "name": member["current_name"],
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "tracked_days": len(tracked_days),
            "days_participated": participated,
            "days_missed": len(missed),
            "missed_dates": missed,
        }
    finally:
        if close:
            conn.close()


def get_promotion_candidates(min_donations_week=50, min_tenure_days=14, active_within_days=7,
                             min_war_races=1, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        season_id = get_current_season_id(conn=conn)
        counts = conn.execute(
            "SELECT "
            "SUM(CASE WHEN cs.role IN ('leader', 'coLeader') THEN 1 ELSE 0 END) AS leaders, "
            "SUM(CASE WHEN cs.role = 'elder' THEN 1 ELSE 0 END) AS elders, "
            "SUM(CASE WHEN cs.role = 'member' THEN 1 ELSE 0 END) AS members, "
            "COUNT(*) AS active_members "
            "FROM members m JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchone()
        active_members = counts["active_members"] or 0
        target_elder_min = max(0, round(active_members * 0.2))
        target_elder_max = max(target_elder_min, round(active_members * 0.3))

        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, cs.best_trophies, "
            "cs.clan_rank, cs.donations_week AS donations, cs.donations_received_week AS donations_received, cs.last_seen_api AS last_seen "
            "FROM members m "
            "JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' AND cs.role = 'member' "
            "ORDER BY cs.donations_week DESC, cs.trophies DESC, m.current_name COLLATE NOCASE",
        ).fetchall()
        recommended = []
        borderline = []
        today = datetime.now(timezone.utc).date()

        for row in rows:
            joined_date = _current_joined_at(conn, row["member_id"])
            tenure_days = None
            if joined_date:
                try:
                    tenure_days = (today - datetime.strptime(joined_date[:10], "%Y-%m-%d").date()).days
                except ValueError:
                    tenure_days = None
            last_seen = _parse_cr_time(row["last_seen"])
            days_inactive = (today - last_seen.date()).days if last_seen else None
            war_races_played = 0
            if season_id is not None:
                war_races_played = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM war_participation wp "
                    "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                    "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                    (season_id, row["member_id"]),
                ).fetchone()["cnt"]

            checks = {
                "donations": (row["donations"] or 0) >= min_donations_week,
                "tenure": tenure_days is not None and tenure_days >= min_tenure_days,
                "activity": days_inactive is not None and days_inactive <= active_within_days,
                "war": season_id is None or war_races_played >= min_war_races,
            }
            score = sum(1 for passed in checks.values() if passed)
            item = {
                "tag": row["tag"],
                "name": row["name"],
                "exp_level": row["exp_level"],
                "trophies": row["trophies"],
                "best_trophies": row["best_trophies"],
                "clan_rank": row["clan_rank"],
                "donations": row["donations"] or 0,
                "donations_received": row["donations_received"] or 0,
                "joined_date": joined_date,
                "tenure_days": tenure_days,
                "days_inactive": days_inactive,
                "war_races_played": war_races_played,
                "score": score,
                "checks": checks,
                "missing": [key for key, passed in checks.items() if not passed],
            }
            item = _member_reference_fields(conn, row["member_id"], item)
            if all(checks.values()):
                recommended.append(item)
            elif score >= 2:
                borderline.append(item)

        recommended.sort(key=lambda item: (-item["score"], -item["donations"], -item["war_races_played"], -item["trophies"]))
        borderline.sort(key=lambda item: (-item["score"], -item["donations"], -item["war_races_played"], -item["trophies"]))
        composition = {
            "active_members": active_members,
            "leaders": counts["leaders"] or 0,
            "elders": counts["elders"] or 0,
            "members": counts["members"] or 0,
            "target_elder_min": target_elder_min,
            "target_elder_max": target_elder_max,
            "elder_capacity_remaining": max(0, target_elder_max - (counts["elders"] or 0)),
        }
        return {
            "season_id": season_id,
            "criteria": {
                "min_donations_week": min_donations_week,
                "min_tenure_days": min_tenure_days,
                "active_within_days": active_within_days,
                "min_war_races": min_war_races,
            },
            "composition": composition,
            "recommended": recommended,
            "borderline": borderline,
        }
    finally:
        if close:
            conn.close()


def upsert_war_current_state(war_data, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        observed_at = _utcnow()
        clan = (war_data or {}).get("clan", {})
        conn.execute(
            "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observed_at, war_data.get("state"), _canon_tag(clan.get("tag")), clan.get("name"), clan.get("fame"), clan.get("repairPoints"), clan.get("periodPoints"), clan.get("clanScore"), _json_or_none(war_data)),
        )
        battle_date = observed_at[:10]
        for participant in clan.get("participants", []):
            member_id = _ensure_member(conn, participant.get("tag"), participant.get("name"), status=None)
            conn.execute(
                "INSERT INTO war_day_status (member_id, battle_date, observed_at, fame, repair_points, boat_attacks, decks_used_total, decks_used_today, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, battle_date) DO UPDATE SET observed_at = excluded.observed_at, fame = excluded.fame, repair_points = excluded.repair_points, boat_attacks = excluded.boat_attacks, decks_used_total = excluded.decks_used_total, decks_used_today = excluded.decks_used_today, raw_json = excluded.raw_json",
                (member_id, battle_date, observed_at, participant.get("fame", 0), participant.get("repairPoints", 0), participant.get("boatAttacks", 0), participant.get("decksUsed", 0), participant.get("decksUsedToday", 0), _json_or_none(participant)),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


# -- Player profiles and battle facts --------------------------------------

def snapshot_player_profile(player_data, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        tag = _canon_tag(player_data.get("tag"))
        member_id = _ensure_member(conn, tag, player_data.get("name"), status=None)
        previous = conn.execute(
            "SELECT exp_level, cards_json FROM player_profile_snapshots WHERE member_id = ? ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
            (member_id,),
        ).fetchone()
        fetched_at = _utcnow()
        current_deck = player_data.get("currentDeck") or []
        cards = player_data.get("cards") or []
        favourite = player_data.get("currentFavouriteCard") or {}
        conn.execute(
            "INSERT INTO player_profile_snapshots (member_id, fetched_at, exp_level, trophies, best_trophies, wins, losses, battle_count, total_donations, donations, donations_received, war_day_wins, challenge_max_wins, challenge_cards_won, tournament_battle_count, tournament_cards_won, three_crown_wins, current_favourite_card_id, current_favourite_card_name, league_statistics_json, current_deck_json, cards_json, badges_json, achievements_json, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member_id, fetched_at, player_data.get("expLevel"), player_data.get("trophies"), player_data.get("bestTrophies"), player_data.get("wins"), player_data.get("losses"), player_data.get("battleCount"), player_data.get("totalDonations"), player_data.get("donations"), player_data.get("donationsReceived"), player_data.get("warDayWins"), player_data.get("challengeMaxWins"), player_data.get("challengeCardsWon"), player_data.get("tournamentBattleCount"), player_data.get("tournamentCardsWon"), player_data.get("threeCrownWins"), favourite.get("id"), favourite.get("name"), _json_or_none(player_data.get("leagueStatistics")), _json_or_none(current_deck), _json_or_none(cards), _json_or_none(player_data.get("badges") or []), _json_or_none(player_data.get("achievements") or []), _json_or_none(player_data)
            ),
        )
        conn.execute(
            "INSERT INTO member_card_collection_snapshots (member_id, fetched_at, cards_json) VALUES (?, ?, ?)",
            (member_id, fetched_at, _json_or_none(cards) or "[]"),
        )
        deck_hash = _hash_payload(current_deck) if current_deck else None
        conn.execute(
            "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, sample_size) VALUES (?, ?, 'player_profile', 'overall', ?, ?, 1)",
            (member_id, fetched_at, deck_hash, _json_or_none(current_deck) or "[]"),
        )
        _store_raw_payload(conn, "player", _tag_key(tag), player_data)
        conn.commit()
        signals = []
        old_level = previous["exp_level"] if previous else None
        new_level = player_data.get("expLevel")
        if isinstance(old_level, int) and isinstance(new_level, int) and new_level > old_level:
            signals.append({
                "type": "player_level_up",
                "tag": tag,
                "name": player_data.get("name"),
                "old_level": old_level,
                "new_level": new_level,
            })

        previous_cards = {}
        if previous and previous["cards_json"]:
            for card in json.loads(previous["cards_json"] or "[]"):
                if card.get("name"):
                    previous_cards[card["name"]] = _card_level(card)
        milestones = (14, 15, 16)
        for card in cards:
            name = card.get("name")
            if not name:
                continue
            old_card_level = previous_cards.get(name)
            new_card_level = _card_level(card)
            if old_card_level is None or new_card_level is None or new_card_level <= old_card_level:
                continue
            for milestone in milestones:
                if old_card_level < milestone <= new_card_level:
                    signals.append({
                        "type": "card_level_milestone",
                        "tag": tag,
                        "name": player_data.get("name"),
                        "card_name": name,
                        "old_level": old_card_level,
                        "new_level": new_card_level,
                        "milestone": milestone,
                    })
        return signals
    finally:
        if close:
            conn.close()


def get_player_intel_refresh_targets(limit=12, stale_after_hours=6, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        stale_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=stale_after_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "WITH latest_profiles AS ("
            "  SELECT member_id, MAX(fetched_at) AS last_profile_at FROM player_profile_snapshots GROUP BY member_id"
            "), latest_battles AS ("
            "  SELECT member_id, MAX(battle_time) AS last_battle_at FROM member_battle_facts GROUP BY member_id"
            ") "
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.clan_rank, "
            "lp.last_profile_at, lb.last_battle_at "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN latest_profiles lp ON lp.member_id = m.member_id "
            "LEFT JOIN latest_battles lb ON lb.member_id = m.member_id "
            "WHERE m.status = 'active' "
            "ORDER BY "
            "CASE cs.role WHEN 'leader' THEN 0 WHEN 'coLeader' THEN 1 WHEN 'elder' THEN 2 ELSE 3 END, "
            "CASE WHEN lp.last_profile_at IS NULL OR lp.last_profile_at < ? THEN 0 ELSE 1 END, "
            "CASE WHEN lb.last_battle_at IS NULL OR lb.last_battle_at < ? THEN 0 ELSE 1 END, "
            "COALESCE(lp.last_profile_at, '') ASC, "
            "COALESCE(lb.last_battle_at, '') ASC, "
            "COALESCE(cs.clan_rank, 999) ASC, "
            "m.current_name COLLATE NOCASE",
            (stale_cutoff, stale_cutoff),
        ).fetchall()
        targets = []
        for row in rows:
            item = dict(row)
            item["needs_profile_refresh"] = item["last_profile_at"] is None or item["last_profile_at"] < stale_cutoff
            item["needs_battle_refresh"] = item["last_battle_at"] is None or item["last_battle_at"] < stale_cutoff
            if not item["needs_profile_refresh"] and not item["needs_battle_refresh"]:
                continue
            targets.append(_member_reference_fields(conn, row["member_id"], item))
        return targets[:limit]
    finally:
        if close:
            conn.close()


def _classify_battle(battle: dict) -> dict:
    battle_type = battle.get("type") or ""
    mode_name = (battle.get("gameMode") or {}).get("name") or ""
    is_war = battle_type in {"riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum", "boatBattle"}
    is_ladder = mode_name == "Ladder" or battle_type == "PvP"
    is_ranked = battle_type == "pathOfLegend" or "Ranked" in mode_name
    is_competitive = battle_type in {"PvP", "pathOfLegend", "trail", "riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum"}
    is_special_event = battle_type == "trail"
    return {
        "is_war": int(is_war),
        "is_ladder": int(is_ladder),
        "is_ranked": int(is_ranked),
        "is_competitive": int(is_competitive),
        "is_special_event": int(is_special_event),
    }


def snapshot_player_battlelog(player_tag, battle_log, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        tag = _canon_tag(player_tag)
        member_id = _ensure_member(conn, tag, status=None)
        _store_raw_payload(conn, "player_battlelog", _tag_key(tag), battle_log)
        for battle in battle_log or []:
            team = (battle.get("team") or [{}])[0]
            opp = (battle.get("opponent") or [{}])[0]
            if not team:
                continue
            crowns_for = team.get("crowns")
            crowns_against = opp.get("crowns") if opp else None
            if crowns_for is None or crowns_against is None:
                outcome = None
            elif crowns_for > crowns_against:
                outcome = "W"
            elif crowns_for < crowns_against:
                outcome = "L"
            else:
                outcome = "D"
            arena = battle.get("arena") or {}
            classified = _classify_battle(battle)
            conn.execute(
                "INSERT OR IGNORE INTO member_battle_facts (member_id, battle_time, battle_type, game_mode_name, game_mode_id, deck_selection, arena_id, arena_name, crowns_for, crowns_against, outcome, trophy_change, starting_trophies, is_competitive, is_ladder, is_ranked, is_war, is_special_event, deck_json, support_cards_json, opponent_name, opponent_tag, opponent_clan_tag, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    member_id,
                    battle.get("battleTime"),
                    battle.get("type"),
                    (battle.get("gameMode") or {}).get("name"),
                    (battle.get("gameMode") or {}).get("id"),
                    battle.get("deckSelection"),
                    arena.get("id") if isinstance(arena, dict) else None,
                    arena.get("name") if isinstance(arena, dict) else str(arena or ""),
                    crowns_for,
                    crowns_against,
                    outcome,
                    team.get("trophyChange"),
                    team.get("startingTrophies"),
                    classified["is_competitive"],
                    classified["is_ladder"],
                    classified["is_ranked"],
                    classified["is_war"],
                    classified["is_special_event"],
                    _json_or_none(team.get("cards") or []),
                    _json_or_none(team.get("supportCards") or []),
                    opp.get("name") if opp else None,
                    _canon_tag(opp.get("tag")) if opp and opp.get("tag") else None,
                    _canon_tag((opp.get("clan") or {}).get("tag")) if opp else None,
                    _json_or_none(battle),
                ),
            )
        recent_rows = conn.execute(
            "SELECT deck_json, battle_time FROM member_battle_facts WHERE member_id = ? AND is_competitive = 1 ORDER BY battle_time DESC LIMIT 30",
            (member_id,),
        ).fetchall()
        sample_battles, card_usage = _aggregate_card_usage_from_battle_facts(recent_rows)
        conn.execute(
            "INSERT INTO member_card_usage_snapshots (member_id, fetched_at, source, mode_scope, sample_battles, cards_json) VALUES (?, ?, 'battle_log', 'overall', ?, ?)",
            (member_id, _utcnow(), sample_battles, _json_or_none(card_usage) or "[]"),
        )
        if recent_rows:
            latest_cards = json.loads(recent_rows[0]["deck_json"] or "[]")
            if latest_cards:
                conn.execute(
                    "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, sample_size) VALUES (?, ?, 'battle_log', 'recent', ?, ?, ?)",
                    (member_id, _utcnow(), _hash_payload(latest_cards), _json_or_none(latest_cards) or "[]", len(recent_rows)),
                )
        _recompute_member_recent_form(member_id, conn=conn)
        conn.commit()
    finally:
        if close:
            conn.close()


def _recompute_member_recent_form(member_id: int, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        scopes = {
            "overall_10": "1=1",
            "competitive_10": "is_competitive = 1",
            "war_10": "is_war = 1",
        }
        for scope, predicate in scopes.items():
            rows = conn.execute(
                f"SELECT outcome, crowns_for, crowns_against, trophy_change FROM member_battle_facts WHERE member_id = ? AND {predicate} ORDER BY battle_time DESC LIMIT 10",
                (member_id,),
            ).fetchall()
            sample_size = len(rows)
            wins = sum(1 for r in rows if r["outcome"] == "W")
            losses = sum(1 for r in rows if r["outcome"] == "L")
            draws = sum(1 for r in rows if r["outcome"] == "D")
            streak_type = rows[0]["outcome"] if rows and rows[0]["outcome"] else None
            current_streak = 0
            for row in rows:
                if streak_type and row["outcome"] == streak_type:
                    current_streak += 1
                else:
                    break
            diffs = [(r["crowns_for"] or 0) - (r["crowns_against"] or 0) for r in rows if r["crowns_for"] is not None and r["crowns_against"] is not None]
            trophy_changes = [r["trophy_change"] for r in rows if r["trophy_change"] is not None]
            avg_crown_diff = round(sum(diffs) / len(diffs), 2) if diffs else None
            avg_trophy_change = round(sum(trophy_changes) / len(trophy_changes), 2) if trophy_changes else None
            label = _build_form_label(wins, losses, sample_size)
            summary = _build_form_summary(wins, losses, draws, sample_size, label)
            conn.execute(
                "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, scope) DO UPDATE SET computed_at = excluded.computed_at, sample_size = excluded.sample_size, wins = excluded.wins, losses = excluded.losses, draws = excluded.draws, current_streak = excluded.current_streak, current_streak_type = excluded.current_streak_type, win_rate = excluded.win_rate, avg_crown_diff = excluded.avg_crown_diff, avg_trophy_change = excluded.avg_trophy_change, form_label = excluded.form_label, summary = excluded.summary",
                (member_id, _utcnow(), scope, sample_size, wins, losses, draws, current_streak, streak_type, round(wins / sample_size, 4) if sample_size else 0, avg_crown_diff, avg_trophy_change, label, summary),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


# -- Signal and announcement logs ------------------------------------------

def was_signal_sent(signal_type, date_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        return conn.execute("SELECT 1 FROM signal_log WHERE signal_type = ? AND signal_date = ?", (signal_type, date_str)).fetchone() is not None
    finally:
        if close:
            conn.close()


def mark_signal_sent(signal_type, date_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO signal_log (signal_type, signal_date) VALUES (?, ?)", (signal_type, date_str))
        conn.commit()
    finally:
        if close:
            conn.close()


def mark_announcement_sent(date_str, announcement_type, target_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO cake_day_announcements (announcement_date, announcement_type, target_tag) VALUES (?, ?, ?)",
            (date_str, announcement_type, _canon_tag(target_tag) if target_tag else None),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def was_announcement_sent(date_str, announcement_type, target_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM cake_day_announcements WHERE announcement_date = ? AND announcement_type = ? AND target_tag IS ?",
            (date_str, announcement_type, _canon_tag(target_tag) if target_tag else None),
        ).fetchone()
        return row is not None
    finally:
        if close:
            conn.close()


# -- Messaging --------------------------------------------------------------

def save_message(scope, author_type, content, summary=None, channel_id=None, channel_name=None,
                 channel_kind=None, discord_user_id=None, username=None, display_name=None,
                 member_tag=None, workflow=None, event_type=None, discord_message_id=None,
                 raw_json=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = None
        if member_tag:
            member_id = _ensure_member(conn, member_tag)
        if discord_user_id is not None:
            upsert_discord_user(discord_user_id, username=username, display_name=display_name, conn=conn)
            if member_id is None:
                link = conn.execute(
                    "SELECT member_id FROM discord_links WHERE discord_user_id = ? AND is_primary = 1",
                    (str(discord_user_id),),
                ).fetchone()
                if link:
                    member_id = link["member_id"]
        _ensure_channel(conn, channel_id, channel_name=channel_name, channel_kind=channel_kind)
        thread_id = _ensure_thread(
            conn,
            scope,
            channel_id=str(channel_id) if channel_id is not None else None,
            discord_user_id=str(discord_user_id) if discord_user_id is not None else None,
            member_id=member_id,
        )
        now = _utcnow()
        summary = summary if summary is not None else (content[:200] if content else "")
        conn.execute(
            "INSERT INTO messages (discord_message_id, thread_id, channel_id, discord_user_id, member_id, author_type, workflow, event_type, content, summary, created_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(discord_message_id) if discord_message_id is not None else None,
                thread_id,
                str(channel_id) if channel_id is not None else None,
                str(discord_user_id) if discord_user_id is not None else None,
                member_id,
                author_type,
                workflow,
                event_type,
                content,
                summary,
                now,
                _json_or_none(raw_json),
            ),
        )
        message_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "UPDATE conversation_threads SET last_active_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        if scope.startswith("channel:") and author_type == "assistant":
            scope_type, scope_key = _normalize_scope(scope)
            conn.execute(
                "INSERT INTO channel_state (channel_id, last_elixir_post_at, last_summary) VALUES (?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET last_elixir_post_at = excluded.last_elixir_post_at, last_summary = excluded.last_summary",
                (scope_key, now, summary),
            )
        if discord_user_id is not None:
            importance = 2 if workflow in {"leader", "reception"} else 1
            save_memory_episode(
                "discord_user",
                str(discord_user_id),
                workflow or author_type,
                summary,
                importance=importance,
                source_message_ids=[message_id],
                conn=conn,
            )
            if author_type == "user":
                save_memory_fact(
                    "discord_user",
                    str(discord_user_id),
                    "last_user_summary",
                    summary,
                    confidence=0.6,
                    source_message_id=message_id,
                    conn=conn,
                )
        if member_id is not None:
            importance = 2 if workflow in {"leader", "reception"} else 1
            save_memory_episode(
                "member",
                str(member_id),
                workflow or author_type,
                summary,
                importance=importance,
                source_message_ids=[message_id],
                conn=conn,
            )
        if channel_id is not None and author_type == "assistant":
            save_memory_episode(
                "channel",
                str(channel_id),
                workflow or "assistant_post",
                summary,
                importance=1,
                source_message_ids=[message_id],
                conn=conn,
            )
        rows = conn.execute(
            "SELECT message_id FROM messages WHERE thread_id = ? ORDER BY created_at DESC, message_id DESC",
            (thread_id,),
        ).fetchall()
        if len(rows) > CONVERSATION_MAX_PER_SCOPE:
            ids_to_keep = [r["message_id"] for r in rows[:CONVERSATION_MAX_PER_SCOPE]]
            placeholders = ",".join("?" for _ in ids_to_keep)
            conn.execute(
                f"DELETE FROM messages WHERE thread_id = ? AND message_id NOT IN ({placeholders})",
                (thread_id, *ids_to_keep),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


def list_thread_messages(scope, limit=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        scope_type, scope_key = _normalize_scope(scope)
        row = conn.execute(
            "SELECT thread_id FROM conversation_threads WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT author_type, content, summary, created_at FROM messages WHERE thread_id = ? ORDER BY created_at DESC, message_id DESC LIMIT ?",
            (row["thread_id"], limit),
        ).fetchall()
        out = []
        for msg in reversed(rows):
            role = "assistant" if msg["author_type"] == "assistant" else "user"
            out.append({
                "role": role,
                "content": msg["content"],
                "author_name": None,
                "recorded_at": msg["created_at"],
            })
        return out
    finally:
        if close:
            conn.close()


def purge_old_conversations(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CONVERSATION_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        conn.commit()
    finally:
        if close:
            conn.close()


# -- Member metadata --------------------------------------------------------

def record_join_date(tag, name, joined_date, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name, status="active")
        current = _get_current_membership(conn, member_id)
        if not current:
            conn.execute(
                "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, 'manual_record', NULL)",
                (member_id, joined_date),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


def clear_member_tenure(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, status="left")
        current = _get_current_membership(conn, member_id)
        if current:
            conn.execute(
                "UPDATE clan_memberships SET left_at = ?, leave_source = 'manual_clear' WHERE membership_id = ?",
                (_utcnow()[:10], current["membership_id"]),
            )
        conn.execute("UPDATE members SET status = 'left', last_seen_at = ? WHERE member_id = ?", (_utcnow(), member_id))
        conn.execute(
            "DELETE FROM cake_day_announcements WHERE target_tag = ? AND announcement_type = 'join_anniversary'",
            (_canon_tag(tag),),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_join_date(tag, name, joined_date, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, joined_at_override=_normalize_date_string(joined_date))
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_birthday(tag, name, month, day, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, birth_month=month, birth_day=day)
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_profile_url(tag, name, url, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, profile_url=url)
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_poap_address(tag, name, poap_address, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, poap_address=poap_address)
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_note(tag, name, note, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, note=note)
        conn.commit()
    finally:
        if close:
            conn.close()


def get_member_metadata(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.member_id, md.birth_month, md.birth_day, md.profile_url, md.poap_address, md.note FROM members m LEFT JOIN member_metadata md ON md.member_id = m.member_id WHERE m.player_tag = ?",
            (_canon_tag(tag),),
        ).fetchone()
        if not row:
            return None
        member_id = row["member_id"]
        return {
            "tag": _canon_tag(tag),
            "joined_date": _current_joined_at(conn, member_id),
            "birth_month": row["birth_month"],
            "birth_day": row["birth_day"],
            "profile_url": row["profile_url"] or "",
            "poap_address": row["poap_address"] or "",
            "note": row["note"] or "",
        }
    finally:
        if close:
            conn.close()


def get_member_metadata_map(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, md.birth_month, md.birth_day, md.profile_url, md.poap_address, md.note FROM members m LEFT JOIN member_metadata md ON md.member_id = m.member_id"
        ).fetchall()
        result = {}
        for row in rows:
            result[_tag_key(row["player_tag"])] = {
                "joined_date": _current_joined_at(conn, row["member_id"]),
                "birth_month": row["birth_month"],
                "birth_day": row["birth_day"],
                "profile_url": row["profile_url"] or "",
                "poap_address": row["poap_address"] or "",
                "note": row["note"] or "",
            }
        return result
    finally:
        if close:
            conn.close()


def list_member_metadata_rows(status="active", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name, m.status, cs.role, "
            "md.joined_at_override, md.birth_month, md.birth_day, md.profile_url, md.note, "
            "dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN member_metadata md ON md.member_id = m.member_id "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "WHERE (? IS NULL OR m.status = ?) "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (status, status),
        ).fetchall()
        result = []
        for row in rows:
            item = {
                "player_tag": row["player_tag"],
                "current_name": row["current_name"] or "",
                "status": row["status"] or "",
                "role": row["role"] or "",
                "discord_username": row["discord_username"] or "",
                "discord_display_name": row["discord_display_name"] or "",
                "effective_joined_date": _current_joined_at(conn, row["member_id"]) or "",
                "joined_date_override": row["joined_at_override"] or "",
                "birth_month": row["birth_month"] or "",
                "birth_day": row["birth_day"] or "",
                "profile_url": row["profile_url"] or "",
                "note": row["note"] or "",
            }
            result.append(item)
        return result
    finally:
        if close:
            conn.close()


def export_member_metadata_csv(csv_path, status="active", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = list_member_metadata_rows(status=status, conn=conn)
        fieldnames = [
            "player_tag",
            "current_name",
            "status",
            "role",
            "discord_username",
            "discord_display_name",
            "effective_joined_date",
            "joined_date_override",
            "birth_month",
            "birth_day",
            "profile_url",
            "note",
        ]
        with open(csv_path, "w", newline="") as handle:
            writer = csv_mod.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
    finally:
        if close:
            conn.close()


def import_member_metadata_csv(csv_path, *, dry_run=False, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows_read = 0
        updated = 0
        errors = []
        with open(csv_path, "r", newline="") as handle:
            reader = csv_mod.DictReader(handle)
            if reader.fieldnames is None or "player_tag" not in reader.fieldnames:
                raise ValueError("CSV must include a player_tag column")
            for line_number, row in enumerate(reader, start=2):
                rows_read += 1
                try:
                    tag = _canon_tag(row.get("player_tag"))
                    if not tag:
                        raise ValueError("player_tag is required")
                    member = conn.execute(
                        "SELECT m.member_id, md.joined_at_override, md.birth_month, md.birth_day, md.profile_url, md.note "
                        "FROM members m LEFT JOIN member_metadata md ON md.member_id = m.member_id "
                        "WHERE m.player_tag = ?",
                        (tag,),
                    ).fetchone()
                    if not member:
                        raise ValueError(f"unknown player_tag: {tag}")

                    joined_date_override = _normalize_date_string(row.get("joined_date_override"))
                    birth_month = _parse_optional_int(
                        row.get("birth_month"),
                        field_name="birth_month",
                        minimum=1,
                        maximum=12,
                    )
                    birth_day = _parse_optional_int(
                        row.get("birth_day"),
                        field_name="birth_day",
                        minimum=1,
                        maximum=31,
                    )
                    if (birth_month is None) != (birth_day is None):
                        raise ValueError("birth_month and birth_day must both be set or both be blank")
                    profile_url = (row.get("profile_url") or "").strip() or None
                    note = (row.get("note") or "").strip() or None

                    changed = any(
                        [
                            (member["joined_at_override"] or None) != joined_date_override,
                            member["birth_month"] != birth_month,
                            member["birth_day"] != birth_day,
                            (member["profile_url"] or None) != profile_url,
                            (member["note"] or None) != note,
                        ]
                    )
                    if not changed:
                        continue
                    updated += 1
                    if dry_run:
                        continue
                    _upsert_member_metadata(
                        conn,
                        member["member_id"],
                        joined_at_override=joined_date_override,
                        birth_month=birth_month,
                        birth_day=birth_day,
                        profile_url=profile_url,
                        note=note,
                    )
                except Exception as exc:
                    errors.append({"line": line_number, "player_tag": row.get("player_tag", ""), "error": str(exc)})
        if errors:
            if not dry_run:
                conn.rollback()
            return {"rows_read": rows_read, "updated": 0 if not dry_run else updated, "errors": errors}
        if not dry_run:
            conn.commit()
        return {"rows_read": rows_read, "updated": updated, "errors": []}
    finally:
        if close:
            conn.close()


def backfill_join_dates(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT member_id, MIN(observed_at) AS first_seen FROM member_state_snapshots GROUP BY member_id"
        ).fetchall()
        for row in rows:
            member_id = row["member_id"]
            if _current_joined_at(conn, member_id):
                continue
            conn.execute(
                "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, 'backfill', NULL)",
                (member_id, (row["first_seen"] or "")[:10]),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


def get_join_anniversaries_today(today_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        month_day = today_str[5:]
        year = today_str[:4]
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name FROM members m WHERE m.status = 'active'"
        ).fetchall()
        result = []
        for row in rows:
            joined_at = _current_joined_at(conn, row["member_id"])
            if not joined_at or joined_at[5:] != month_day or joined_at[:4] == year:
                continue
            result.append({
                "tag": row["tag"],
                "name": row["name"],
                "joined_date": joined_at,
                "years": int(year) - int(joined_at[:4]),
            })
        return result
    finally:
        if close:
            conn.close()


def get_birthdays_today(today_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        month = int(today_str[5:7])
        day = int(today_str[8:10])
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, md.birth_month, md.birth_day FROM member_metadata md JOIN members m ON m.member_id = md.member_id WHERE md.birth_month = ? AND md.birth_day = ?",
            (month, day),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()
# -- Purge ------------------------------------------------------------------

def purge_old_data(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        snapshot_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=SNAPSHOT_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        war_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=WAR_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        raw_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=RAW_PAYLOAD_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        conv_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CONVERSATION_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("DELETE FROM member_state_snapshots WHERE observed_at < ?", (snapshot_cutoff,))
        conn.execute("DELETE FROM player_profile_snapshots WHERE fetched_at < ?", (snapshot_cutoff,))
        conn.execute("DELETE FROM member_card_collection_snapshots WHERE fetched_at < ?", (snapshot_cutoff,))
        conn.execute("DELETE FROM member_deck_snapshots WHERE fetched_at < ?", (snapshot_cutoff,))
        conn.execute("DELETE FROM member_card_usage_snapshots WHERE fetched_at < ?", (snapshot_cutoff,))
        conn.execute("DELETE FROM member_battle_facts WHERE battle_time < ?", (snapshot_cutoff,))
        conn.execute("DELETE FROM war_races WHERE COALESCE(created_date, '') < ?", (war_cutoff,))
        conn.execute("DELETE FROM war_current_state WHERE observed_at < ?", (war_cutoff,))
        conn.execute("DELETE FROM war_day_status WHERE observed_at < ?", (war_cutoff,))
        conn.execute("DELETE FROM raw_api_payloads WHERE fetched_at < ?", (raw_cutoff,))
        conn.execute("DELETE FROM messages WHERE created_at < ?", (conv_cutoff,))
        cake_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM cake_day_announcements WHERE announcement_date < ?", (cake_cutoff,))
        conn.execute("DELETE FROM signal_log WHERE signal_date < ?", (cake_cutoff,))
        conn.commit()
    finally:
        if close:
            conn.close()
