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
