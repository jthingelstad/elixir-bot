from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlite3

from db import (
    CARD_COLLECTION_RETENTION_DAYS,
    CONVERSATION_RETENTION_DAYS,
    PLAYER_PROFILE_RETENTION_DAYS,
    RAW_PAYLOAD_RETENTION_DAYS,
    SIGNAL_OUTCOME_RETENTION_DAYS,
    SNAPSHOT_RETENTION_DAYS,
    TOURNAMENT_RETENTION_DAYS,
    WAR_RETENTION_DAYS,
    _canon_tag,
    _trusted_current_joined_at,
    _current_joined_at,
    _ensure_member,
    _get_current_membership,
    _normalize_date_string,
    _parse_optional_int,
    _rowdicts,
    _tag_key,
    _upsert_member_metadata,
    _utcnow,
    chicago_today,
    managed_connection,
)

@managed_connection
def record_join_date(tag: str, name: str, joined_date: str, conn: Optional[sqlite3.Connection] = None) -> None:
    normalized_joined_date = _normalize_date_string(joined_date)
    member_id = _ensure_member(conn, tag, name=name, status="active")
    current = _get_current_membership(conn, member_id)
    if not current:
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, 'observed_join', NULL)",
            (member_id, normalized_joined_date),
        )
    else:
        conn.execute(
            "UPDATE clan_memberships SET joined_at = ?, join_source = 'observed_join' WHERE membership_id = ?",
            (normalized_joined_date, current["membership_id"]),
        )
    _upsert_member_metadata(conn, member_id, joined_at=normalized_joined_date)
    conn.commit()


@managed_connection
def clear_member_tenure(tag: str, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, status="left")
    current = _get_current_membership(conn, member_id)
    if current:
        conn.execute(
            "UPDATE clan_memberships SET left_at = ?, leave_source = 'manual_clear' WHERE membership_id = ?",
            (chicago_today(), current["membership_id"]),
        )
    conn.execute("UPDATE members SET status = 'left', last_seen_at = ? WHERE member_id = ?", (_utcnow(), member_id))
    conn.execute(
        "DELETE FROM cake_day_announcements WHERE target_tag = ? AND announcement_type = 'join_anniversary'",
        (_canon_tag(tag),),
    )
    conn.commit()


@managed_connection
def set_member_join_date(tag: str, name: str, joined_date: str, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    normalized_joined_date = _normalize_date_string(joined_date)
    _upsert_member_metadata(conn, member_id, joined_at=normalized_joined_date)
    conn.commit()


@managed_connection
def set_member_birthday(tag: str, name: str, month: int, day: int, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, birth_month=month, birth_day=day)
    conn.commit()


@managed_connection
def set_member_profile_url(tag: str, name: str, url: Optional[str], conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, profile_url=(url or "").strip() or None)
    conn.commit()


@managed_connection
def set_member_poap_address(tag: str, name: str, poap_address: Optional[str], conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, poap_address=(poap_address or "").strip() or None)
    conn.commit()


@managed_connection
def set_member_note(tag: str, name: str, note: Optional[str], conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, note=(note or "").strip() or None)
    conn.commit()


@managed_connection
def clear_member_join_date(tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, joined_at=None)
    conn.commit()


@managed_connection
def clear_member_birthday(tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, birth_month=None, birth_day=None)
    conn.commit()


@managed_connection
def clear_member_profile_url(tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, profile_url=None)
    conn.commit()


@managed_connection
def clear_member_poap_address(tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, poap_address=None)
    conn.commit()


@managed_connection
def clear_member_note(tag: str, name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(conn, member_id, note=None)
    conn.commit()


@managed_connection
def set_member_generated_profile(tag: str, name: str, bio: str, highlight: str = "general", generated_at: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    member_id = _ensure_member(conn, tag, name=name)
    _upsert_member_metadata(
        conn,
        member_id,
        generated_bio=(bio or "").strip(),
        generated_highlight=(highlight or "general").strip() or "general",
        generated_profile_updated_at=generated_at or _utcnow(),
    )
    conn.commit()


@managed_connection
def upsert_member_generated_profiles(profiles_by_tag: Optional[dict], conn: Optional[sqlite3.Connection] = None) -> None:
    now = _utcnow()
    for raw_tag, payload in (profiles_by_tag or {}).items():
        if not payload:
            continue
        tag = _canon_tag(raw_tag)
        name = payload.get("name") or payload.get("member_name") or tag
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(
            conn,
            member_id,
            generated_bio=(payload.get("bio") or "").strip(),
            generated_highlight=(payload.get("highlight") or "general").strip() or "general",
            generated_profile_updated_at=payload.get("generated_at") or now,
        )
    conn.commit()


@managed_connection
def get_member_metadata(tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT m.member_id, md.birth_month, md.birth_day, md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at, "
        "md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at, "
        "md.profile_url, md.poap_address, md.note, md.generated_bio, md.generated_highlight, md.generated_profile_updated_at "
        "FROM members m LEFT JOIN member_metadata md ON md.member_id = m.member_id WHERE m.player_tag = ?",
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
        "cr_account_age_days": row["cr_account_age_days"],
        "cr_account_age_years": row["cr_account_age_years"],
        "cr_account_age_updated_at": row["cr_account_age_updated_at"],
        "cr_games_per_day": row["cr_games_per_day"],
        "cr_games_per_day_window_days": row["cr_games_per_day_window_days"],
        "cr_games_per_day_updated_at": row["cr_games_per_day_updated_at"],
        "profile_url": row["profile_url"] or "",
        "poap_address": row["poap_address"] or "",
        "note": row["note"] or "",
        "bio": row["generated_bio"] or "",
        "highlight": row["generated_highlight"] or "",
        "generated_profile_updated_at": row["generated_profile_updated_at"],
    }


@managed_connection
def get_member_metadata_map(conn: Optional[sqlite3.Connection] = None) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag, md.birth_month, md.birth_day, md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at, "
        "md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at, "
        "md.profile_url, md.poap_address, md.note, md.generated_bio, md.generated_highlight, md.generated_profile_updated_at "
        "FROM members m LEFT JOIN member_metadata md ON md.member_id = m.member_id"
    ).fetchall()
    result = {}
    for row in rows:
        result[_tag_key(row["player_tag"])] = {
            "joined_date": _current_joined_at(conn, row["member_id"]),
            "birth_month": row["birth_month"],
            "birth_day": row["birth_day"],
            "cr_account_age_days": row["cr_account_age_days"],
            "cr_account_age_years": row["cr_account_age_years"],
            "cr_account_age_updated_at": row["cr_account_age_updated_at"],
            "cr_games_per_day": row["cr_games_per_day"],
            "cr_games_per_day_window_days": row["cr_games_per_day_window_days"],
            "cr_games_per_day_updated_at": row["cr_games_per_day_updated_at"],
            "profile_url": row["profile_url"] or "",
            "poap_address": row["poap_address"] or "",
            "note": row["note"] or "",
            "bio": row["generated_bio"] or "",
            "highlight": row["generated_highlight"] or "",
            "generated_profile_updated_at": row["generated_profile_updated_at"],
        }
    return result


@managed_connection
def list_member_metadata_rows(status: Optional[str] = "active", conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag, m.current_name, m.status, cs.role, "
        "md.joined_at, md.birth_month, md.birth_day, md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at, "
        "md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at, "
        "md.profile_url, md.poap_address, md.note, "
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
            "joined_date": _current_joined_at(conn, row["member_id"]) or "",
            "birth_month": row["birth_month"] or "",
            "birth_day": row["birth_day"] or "",
            "cr_account_age_days": row["cr_account_age_days"],
            "cr_account_age_years": row["cr_account_age_years"],
            "cr_account_age_updated_at": row["cr_account_age_updated_at"],
            "cr_games_per_day": row["cr_games_per_day"],
            "cr_games_per_day_window_days": row["cr_games_per_day_window_days"],
            "cr_games_per_day_updated_at": row["cr_games_per_day_updated_at"],
            "profile_url": row["profile_url"] or "",
            "poap_address": row["poap_address"] or "",
            "note": row["note"] or "",
        }
        result.append(item)
    return result


@managed_connection
def backfill_join_dates(conn: Optional[sqlite3.Connection] = None) -> None:
    rows = conn.execute("SELECT member_id FROM members").fetchall()
    for row in rows:
        member_id = row["member_id"]
        if _current_joined_at(conn, member_id):
            continue
        trusted_joined_at = _trusted_current_joined_at(conn, member_id)
        if not trusted_joined_at:
            continue
        _upsert_member_metadata(conn, member_id, joined_at=trusted_joined_at)
    conn.commit()


@managed_connection
def get_join_anniversaries_today(today_str: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    today = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name FROM members m WHERE m.status = 'active'"
    ).fetchall()
    result = []
    for row in rows:
        joined_at = _current_joined_at(conn, row["member_id"])
        if not joined_at:
            continue
        try:
            joined_day = datetime.strptime(joined_at[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if joined_day >= today:
            continue
        if joined_day.day != today.day:
            continue
        months = (today.year - joined_day.year) * 12 + (today.month - joined_day.month)
        if months < 3 or months % 3 != 0:
            continue
        result.append({
            "tag": row["tag"],
            "name": row["name"],
            "joined_date": joined_at,
            "months": months,
            "quarters": months // 3,
            "years": months // 12,
            "is_yearly": months % 12 == 0,
        })
    return result


@managed_connection
def get_birthdays_today(today_str: str, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    month = int(today_str[5:7])
    day = int(today_str[8:10])
    rows = conn.execute(
        "SELECT m.player_tag AS tag, m.current_name AS name, md.birth_month, md.birth_day FROM member_metadata md JOIN members m ON m.member_id = md.member_id WHERE md.birth_month = ? AND md.birth_day = ?",
        (month, day),
    ).fetchall()
    return _rowdicts(rows)
# -- Purge ------------------------------------------------------------------

def _utc_cutoff(days):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _date_cutoff(days):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d")


# Ordered list of (table, column, retention_days) for all purge targets.
_PURGE_TARGETS = [
    ("member_state_snapshots", "observed_at", SNAPSHOT_RETENTION_DAYS),
    ("player_profile_snapshots", "fetched_at", PLAYER_PROFILE_RETENTION_DAYS),
    ("member_card_collection_snapshots", "fetched_at", CARD_COLLECTION_RETENTION_DAYS),
    ("member_deck_snapshots", "fetched_at", SNAPSHOT_RETENTION_DAYS),
    ("member_card_usage_snapshots", "fetched_at", SNAPSHOT_RETENTION_DAYS),
    ("member_battle_facts", "battle_time", SNAPSHOT_RETENTION_DAYS),
    ("war_races", "COALESCE(created_date, '')", WAR_RETENTION_DAYS),
    ("war_current_state", "observed_at", WAR_RETENTION_DAYS),
    ("war_day_status", "observed_at", WAR_RETENTION_DAYS),
    ("war_period_clan_status", "observed_at", WAR_RETENTION_DAYS),
    ("war_participant_snapshots", "observed_at", WAR_RETENTION_DAYS),
    ("raw_api_payloads", "fetched_at", RAW_PAYLOAD_RETENTION_DAYS),
    ("messages", "created_at", CONVERSATION_RETENTION_DAYS),
    ("signal_outcomes", "created_at", SIGNAL_OUTCOME_RETENTION_DAYS),
    ("tournaments", "watching_started_at", TOURNAMENT_RETENTION_DAYS),
]

_PURGE_DATE_TARGETS = [
    ("cake_day_announcements", "announcement_date", 7),
    ("signal_log", "signal_date", SNAPSHOT_RETENTION_DAYS),
]


@managed_connection
def purge_old_data(conn: Optional[sqlite3.Connection] = None) -> dict[str, int]:
    """Delete expired rows and return per-table deletion counts."""
    stats = {}
    for table, column, days in _PURGE_TARGETS:
        cutoff = _utc_cutoff(days)
        cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        stats[table] = cursor.rowcount
    for table, column, days in _PURGE_DATE_TARGETS:
        cutoff = _date_cutoff(days)
        cursor = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        stats[table] = cursor.rowcount
    conn.commit()
    return stats
