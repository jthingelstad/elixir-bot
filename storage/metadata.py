from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import (
    BATTLE_EVENT_RETENTION_DAYS,
    CLAN_EVENT_RETENTION_DAYS,
    CONVERSATION_RETENTION_DAYS,
    LLM_CALL_RETENTION_DAYS,
    LLM_PROMPT_RETENTION_DAYS,
    PLAYER_EVENT_RETENTION_DAYS,
    RAW_PAYLOAD_RETENTION_DAYS,
    TOURNAMENT_RETENTION_DAYS,
    WAR_RETENTION_DAYS,
    _current_joined_at,
    _ensure_member,
    _normalize_date_string,
    _upsert_member_metadata,
    managed_connection,
)


@managed_connection
def set_member_join_date(
    tag: str, name: str, joined_date: str, conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    normalized_joined_date = _normalize_date_string(joined_date)
    _upsert_member_metadata(conn, member_id, joined_at=normalized_joined_date)
    conn.commit()


@managed_connection
def set_member_birthday(
    tag: str, name: str, month: int, day: int, conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, birth_month=month, birth_day=day)
    conn.commit()


@managed_connection
def set_member_profile_url(
    tag: str, name: str, url: Optional[str], conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, profile_url=(url or "").strip() or None)
    conn.commit()


@managed_connection
def set_member_note(
    tag: str, name: str, note: Optional[str], conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, note=(note or "").strip() or None)
    conn.commit()


@managed_connection
def clear_member_join_date(
    tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, joined_at=None)
    conn.commit()


@managed_connection
def clear_member_birthday(
    tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, birth_month=None, birth_day=None)
    conn.commit()


@managed_connection
def clear_member_profile_url(
    tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, profile_url=None)
    conn.commit()


@managed_connection
def clear_member_note(
    tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None
) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, note=None)
    conn.commit()


@managed_connection
def list_member_metadata_rows(
    status: Optional[str] = "active", conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    # v5.1: members/member_current_state/member_metadata were replaced by
    # players/player_current_state/player_metadata (all keyed by player_tag);
    # membership status is an open clan_memberships row, not a column; Discord
    # names live on discord_users; poap_address was dropped (POAP paused).
    active_expr = (
        "EXISTS (SELECT 1 FROM clan_memberships cm "
        "WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
    )
    if status == "active":
        where = f"WHERE {active_expr}"
    elif status:  # any other explicit status (e.g. 'left') → not a current member
        where = f"WHERE NOT {active_expr}"
    else:  # None → all players
        where = ""
    rows = conn.execute(
        "SELECT m.player_tag, m.current_name, "
        f"{active_expr} AS is_active, cs.role, "
        "md.joined_at, md.birth_month, md.birth_day, md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at, "
        "md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at, "
        "md.cr_collection_level, md.cr_collection_level_badge_tier, md.cr_collection_level_badge_max_tier, md.cr_collection_level_updated_at, "
        "md.cr_clan_war_wins, md.cr_battle_wins, md.cr_clan_donations, md.cr_banner_count, md.cr_emote_count, md.cr_profile_badges_updated_at, "
        "md.profile_url, md.note, "
        "du.username AS discord_username, du.display_name AS discord_display_name "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "LEFT JOIN player_metadata md ON md.player_tag = m.player_tag "
        "LEFT JOIN discord_links dl ON dl.player_tag = m.player_tag AND dl.is_primary = 1 "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        f"{where} "
        "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE"
    ).fetchall()
    result = []
    for row in rows:
        item = {
            "player_tag": row["player_tag"],
            "current_name": row["current_name"] or "",
            "status": "active" if row["is_active"] else "left",
            "role": row["role"] or "",
            "discord_username": row["discord_username"] or "",
            "discord_display_name": row["discord_display_name"] or "",
            "joined_date": _current_joined_at(conn, row["player_tag"]) or "",
            "birth_month": row["birth_month"] or "",
            "birth_day": row["birth_day"] or "",
            "cr_account_age_days": row["cr_account_age_days"],
            "cr_account_age_years": row["cr_account_age_years"],
            "cr_account_age_updated_at": row["cr_account_age_updated_at"],
            "cr_games_per_day": row["cr_games_per_day"],
            "cr_games_per_day_window_days": row["cr_games_per_day_window_days"],
            "cr_games_per_day_updated_at": row["cr_games_per_day_updated_at"],
            "cr_collection_level": row["cr_collection_level"],
            "cr_collection_level_badge_tier": row["cr_collection_level_badge_tier"],
            "cr_collection_level_badge_max_tier": row[
                "cr_collection_level_badge_max_tier"
            ],
            "cr_collection_level_updated_at": row["cr_collection_level_updated_at"],
            "cr_clan_war_wins": row["cr_clan_war_wins"],
            "cr_battle_wins": row["cr_battle_wins"],
            "cr_clan_donations": row["cr_clan_donations"],
            "cr_banner_count": row["cr_banner_count"],
            "cr_emote_count": row["cr_emote_count"],
            "cr_profile_badges_updated_at": row["cr_profile_badges_updated_at"],
            "profile_url": row["profile_url"] or "",
            "note": row["note"] or "",
        }
        result.append(item)
    return result


# -- Purge ------------------------------------------------------------------


def _utc_cutoff(days):
    return (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%S")


def _cr_cutoff(days):
    # battle_events stores battle_time in raw Clash Royale format
    # (YYYYMMDDTHHMMSS.000Z). That sorts differently from the ISO cutoff used
    # everywhere else — the 'T'-position char ('0' vs '-') makes every CR
    # timestamp compare greater than an ISO cutoff, so an ISO comparison never
    # deletes anything. This cutoff matches the stored format.
    return (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    ).strftime("%Y%m%dT%H%M%S.000Z")


def _date_cutoff(days):
    return (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    ).strftime("%Y-%m-%d")


# Tables whose retention column stores raw Clash Royale timestamps, not ISO.
# (battle_events.battle_time; renamed from member_battle_facts in v5.1 — the
# stale name here silently disabled battle_events retention entirely.)
_CR_TIMESTAMP_TABLES = {"battle_events"}

# These carried war tables historically mixed CR-compact and ISO timestamps.
# Normalize-at-write/data-repair is the primary rule; date-prefix retention is
# the defense in depth that prevents one legacy compact row living forever.
_MIXED_TIMESTAMP_TABLES = {
    "war_weeks",
    "war_week_clans",
    "war_participation",
    "war_attendance_days",
}


# Ordered list of (table, column, retention_days) for all purge targets.
# v5.1 retention (docs/reference/v5.1/schema.md §1). Durable, never purged: rollups,
# identity/tenure, awards, war_seasons, recognition_ledger, curated memories.
_PURGE_TARGETS = [
    ("raw_api_payloads", "fetched_at", RAW_PAYLOAD_RETENTION_DAYS),
    ("battle_events", "battle_time", BATTLE_EVENT_RETENTION_DAYS),
    ("player_events", "observed_at", PLAYER_EVENT_RETENTION_DAYS),
    ("clan_events", "observed_at", CLAN_EVENT_RETENTION_DAYS),
    ("war_events", "observed_at", WAR_RETENTION_DAYS),
    ("war_weeks", "COALESCE(created_date, '')", WAR_RETENTION_DAYS),
    ("war_week_clans", "observed_at", WAR_RETENTION_DAYS),
    ("war_participation", "observed_at", WAR_RETENTION_DAYS),
    ("war_attendance_days", "observed_at", WAR_RETENTION_DAYS),
    ("messages", "created_at", CONVERSATION_RETENTION_DAYS),
    ("tournaments", "watching_started_at", TOURNAMENT_RETENTION_DAYS),
    # LLM call telemetry: drop the whole row after 90d (blobs get NULLed at 14d
    # first — see the prompt-blob prune in purge_old_data).
    ("llm_calls", "recorded_at", LLM_CALL_RETENTION_DAYS),
]

_PURGE_DATE_TARGETS = []


@managed_connection
def purge_old_data(conn: Optional[sqlite3.Connection] = None) -> dict[str, int]:
    """Delete expired rows and return per-table deletion counts."""
    stats = {}
    for table, column, days in _PURGE_TARGETS:
        if table in _CR_TIMESTAMP_TABLES:
            predicate, cutoff = column, _cr_cutoff(days)
        elif table in _MIXED_TIMESTAMP_TABLES:
            predicate = f"substr(REPLACE({column}, '-', ''), 1, 8)"
            cutoff = _date_cutoff(days).replace("-", "")
        else:
            predicate, cutoff = column, _utc_cutoff(days)
        cursor = conn.execute(f"DELETE FROM {table} WHERE {predicate} < ?", (cutoff,))
        stats[table] = cursor.rowcount
    for table, column, days in _PURGE_DATE_TARGETS:
        cutoff = _date_cutoff(days)
        cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        stats[table] = cursor.rowcount
    # LLM prompt/response blobs: NULL them after the (short) prompt-retention
    # window while keeping the lightweight metadata row for cost analysis until
    # its own 90d row-delete above. UPDATE, not DELETE — the row survives.
    blob_cutoff = _utc_cutoff(LLM_PROMPT_RETENTION_DAYS)
    try:
        cursor = conn.execute(
            "UPDATE llm_calls SET prompt_json = NULL, response_json = NULL "
            "WHERE recorded_at < ? AND (prompt_json IS NOT NULL OR response_json IS NOT NULL)",
            (blob_cutoff,),
        )
        stats["llm_calls_blobs_pruned"] = cursor.rowcount
    except sqlite3.OperationalError:
        # Columns not yet added on a DB that has never recorded a call — nothing
        # to prune. (They are lazily added by record_llm_call on first write.)
        stats["llm_calls_blobs_pruned"] = 0
    conn.commit()
    return stats
