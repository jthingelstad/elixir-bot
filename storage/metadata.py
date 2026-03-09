from datetime import datetime, timedelta, timezone

from db import (
    CONVERSATION_RETENTION_DAYS,
    RAW_PAYLOAD_RETENTION_DAYS,
    SNAPSHOT_RETENTION_DAYS,
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
    get_connection,
)

def record_join_date(tag, name, joined_date, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
        normalized_joined_date = _normalize_date_string(joined_date)
        _upsert_member_metadata(conn, member_id, joined_at=normalized_joined_date)
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
        _upsert_member_metadata(conn, member_id, profile_url=(url or "").strip() or None)
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_poap_address(tag, name, poap_address, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, poap_address=(poap_address or "").strip() or None)
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_note(tag, name, note, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, note=(note or "").strip() or None)
        conn.commit()
    finally:
        if close:
            conn.close()


def clear_member_join_date(tag, name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, joined_at=None)
        conn.commit()
    finally:
        if close:
            conn.close()


def clear_member_birthday(tag, name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, birth_month=None, birth_day=None)
        conn.commit()
    finally:
        if close:
            conn.close()


def clear_member_profile_url(tag, name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, profile_url=None)
        conn.commit()
    finally:
        if close:
            conn.close()


def clear_member_poap_address(tag, name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, poap_address=None)
        conn.commit()
    finally:
        if close:
            conn.close()


def clear_member_note(tag, name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(conn, member_id, note=None)
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_generated_profile(tag, name, bio, highlight="general", generated_at=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        member_id = _ensure_member(conn, tag, name=name)
        _upsert_member_metadata(
            conn,
            member_id,
            generated_bio=(bio or "").strip(),
            generated_highlight=(highlight or "general").strip() or "general",
            generated_profile_updated_at=generated_at or _utcnow(),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def upsert_member_generated_profiles(profiles_by_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()


def get_member_metadata(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.member_id, md.birth_month, md.birth_day, md.profile_url, md.poap_address, md.note, "
            "md.generated_bio, md.generated_highlight, md.generated_profile_updated_at "
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
            "profile_url": row["profile_url"] or "",
            "poap_address": row["poap_address"] or "",
            "note": row["note"] or "",
            "bio": row["generated_bio"] or "",
            "highlight": row["generated_highlight"] or "",
            "generated_profile_updated_at": row["generated_profile_updated_at"],
        }
    finally:
        if close:
            conn.close()


def get_member_metadata_map(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, md.birth_month, md.birth_day, md.profile_url, md.poap_address, md.note, "
            "md.generated_bio, md.generated_highlight, md.generated_profile_updated_at "
            "FROM members m LEFT JOIN member_metadata md ON md.member_id = m.member_id"
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
                "bio": row["generated_bio"] or "",
                "highlight": row["generated_highlight"] or "",
                "generated_profile_updated_at": row["generated_profile_updated_at"],
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
            "md.joined_at, md.birth_month, md.birth_day, md.profile_url, md.note, "
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
                "profile_url": row["profile_url"] or "",
                "note": row["note"] or "",
            }
            result.append(item)
        return result
    finally:
        if close:
            conn.close()


def backfill_join_dates(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()


def get_join_anniversaries_today(today_str, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
        conn.execute("DELETE FROM war_period_clan_status WHERE observed_at < ?", (war_cutoff,))
        conn.execute("DELETE FROM raw_api_payloads WHERE fetched_at < ?", (raw_cutoff,))
        conn.execute("DELETE FROM messages WHERE created_at < ?", (conv_cutoff,))
        cake_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM cake_day_announcements WHERE announcement_date < ?", (cake_cutoff,))
        conn.execute("DELETE FROM signal_log WHERE signal_date < ?", (cake_cutoff,))
        conn.commit()
    finally:
        if close:
            conn.close()
