import os
import re

from db import (
    _canon_tag,
    _ensure_member,
    _json_or_none,
    _rowdicts,
    _utcnow,
    get_connection,
)

# -- Discord identity and memory helpers -----------------------------------

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _is_real_discord_user_id(discord_user_id) -> bool:
    return str(discord_user_id or "").isdigit()


def _upsert_discord_user_record(conn, discord_user_id, *, username=None, global_name=None, display_name=None):
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


def _apply_discord_link(conn, discord_user_id, member_tag, *, username=None, display_name=None,
                        source="manual_link", confidence=1.0, is_primary=True):
    member_id = _ensure_member(conn, member_tag, name=None)
    if is_primary:
        conn.execute("UPDATE discord_links SET is_primary = 0 WHERE discord_user_id = ?", (str(discord_user_id),))
        conn.execute("UPDATE discord_links SET is_primary = 0 WHERE member_id = ?", (member_id,))
    conn.execute(
        "INSERT INTO discord_links (discord_user_id, member_id, discord_username, discord_display_name, linked_at, source, confidence, is_primary) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(discord_user_id, member_id) DO UPDATE SET discord_username = excluded.discord_username, discord_display_name = excluded.discord_display_name, linked_at = excluded.linked_at, source = excluded.source, confidence = excluded.confidence, is_primary = excluded.is_primary",
        (str(discord_user_id), member_id, username, display_name, _utcnow(), source, confidence, 1 if is_primary else 0),
    )
    return member_id


def _infer_member_tag_for_discord_user(conn, discord_user_id, *, username=None, global_name=None, display_name=None):
    existing = conn.execute(
        "SELECT m.player_tag "
        "FROM discord_links dl "
        "JOIN members m ON m.member_id = dl.member_id "
        "WHERE dl.discord_user_id = ? AND dl.is_primary = 1",
        (str(discord_user_id),),
    ).fetchone()
    if existing:
        return existing["player_tag"]

    from storage.roster import resolve_member

    candidate_values = []
    for value in (display_name, global_name, username):
        text = (value or "").strip()
        if text and text not in candidate_values:
            candidate_values.append(text)

    matched_tags = []
    for candidate in candidate_values:
        matches = resolve_member(candidate, status="active", limit=3, conn=conn)
        exact = [
            item for item in matches
            if item.get("match_source") in {"current_name_exact", "alias_exact"}
        ]
        if len(exact) == 1:
            matched_tags.append(exact[0]["player_tag"])

    unique_tags = list(dict.fromkeys(matched_tags))
    if len(unique_tags) == 1:
        return unique_tags[0]
    return None


def _maybe_auto_link_discord_user(conn, discord_user_id, *, username=None, global_name=None, display_name=None):
    member_tag = _infer_member_tag_for_discord_user(
        conn,
        discord_user_id,
        username=username,
        global_name=global_name,
        display_name=display_name,
    )
    if not member_tag:
        return None
    return _apply_discord_link(
        conn,
        discord_user_id,
        member_tag,
        username=username,
        display_name=display_name or global_name,
        source="auto_exact_name",
        confidence=0.95,
        is_primary=True,
    )

def upsert_discord_user(discord_user_id, username=None, global_name=None, display_name=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        _upsert_discord_user_record(
            conn,
            discord_user_id,
            username=username,
            global_name=global_name,
            display_name=display_name,
        )
        _maybe_auto_link_discord_user(
            conn,
            discord_user_id,
            username=username,
            global_name=global_name,
            display_name=display_name,
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_discord_identity(member_tag, discord_name, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        identity_text = (discord_name or "").strip()
        if not identity_text:
            raise ValueError("discord name is required")
        mention_match = _DISCORD_MENTION_RE.match(identity_text)
        if mention_match:
            discord_user_id = mention_match.group(1)
            existing_user = conn.execute(
                "SELECT username, display_name FROM discord_users WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()
            _upsert_discord_user_record(
                conn,
                discord_user_id,
                username=(existing_user["username"] if existing_user else None),
                display_name=(existing_user["display_name"] if existing_user else None),
            )
            member_id = _apply_discord_link(
                conn,
                discord_user_id,
                member_tag,
                username=(existing_user["username"] if existing_user else None),
                display_name=(existing_user["display_name"] if existing_user else None),
                source="manual_user_id_assignment",
                confidence=1.0,
                is_primary=True,
            )
            conn.commit()
            return member_id
        normalized_name = identity_text.lstrip("@").strip()
        if not normalized_name:
            raise ValueError("discord name is required")
        manual_user_id = f"manual:{normalized_name.casefold()}"
        _upsert_discord_user_record(
            conn,
            manual_user_id,
            username=normalized_name,
            display_name=normalized_name,
        )
        member_id = _apply_discord_link(
            conn,
            manual_user_id,
            member_tag,
            username=normalized_name,
            display_name=normalized_name,
            source="manual_name_assignment",
            confidence=1.0,
            is_primary=True,
        )
        conn.commit()
        return member_id
    finally:
        if close:
            conn.close()


def link_discord_user_to_member(discord_user_id, member_tag, username=None, display_name=None,
                                source="manual_link", confidence=1.0, is_primary=True, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        upsert_discord_user(discord_user_id, username=username, display_name=display_name, conn=conn)
        member_id = _apply_discord_link(
            conn,
            discord_user_id,
            member_tag,
            username=username,
            display_name=display_name,
            source=source,
            confidence=confidence,
            is_primary=is_primary,
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


def get_linked_member_for_discord_user(discord_user_id, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name AS member_name, du.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM discord_links dl "
            "JOIN members m ON m.member_id = dl.member_id "
            "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
            "WHERE dl.discord_user_id = ? AND dl.is_primary = 1",
            (str(discord_user_id),),
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
        if style == "name_with_mention" and _is_real_discord_user_id(user_id):
            return f"{name} (<@{user_id}>)"
        if style == "name_with_handle":
            if _is_real_discord_user_id(user_id):
                return f"{name} (<@{user_id}>)"
            if username:
                handle = username if str(username).startswith("@") else f"@{username}"
                return f"{name} ({handle})"
        if style == "name_with_mention" and username:
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


def get_system_status(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        from storage.war import get_current_season_id
        from storage.player import get_player_intel_refresh_targets
        from storage.roster import get_clan_roster_summary
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        schema_display = f"baseline schema (migration v{schema_version})"
        counts = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM members) AS members_total, "
            "(SELECT COUNT(*) FROM members WHERE status = 'active') AS members_active, "
            "(SELECT COUNT(*) FROM members WHERE status != 'active') AS members_inactive, "
            "(SELECT COUNT(*) FROM discord_users) AS discord_users, "
            "(SELECT COUNT(*) FROM discord_links WHERE is_primary = 1) AS discord_links, "
            "(SELECT COUNT(*) FROM messages) AS message_count, "
            "(SELECT COUNT(*) FROM raw_api_payloads) AS raw_payload_count, "
            "(SELECT COUNT(*) FROM member_battle_facts) AS battle_fact_count, "
            "(SELECT COUNT(*) FROM clan_memories) AS contextual_memory_count, "
            "(SELECT COUNT(*) FROM clan_memories WHERE source_type = 'leader_note') AS contextual_leader_notes, "
            "(SELECT COUNT(*) FROM clan_memories WHERE source_type = 'elixir_inference') AS contextual_inferences, "
            "(SELECT COUNT(*) FROM clan_memories WHERE source_type = 'system') AS contextual_system_notes"
        ).fetchone()
        freshness = {
            "member_state_at": conn.execute(
                "SELECT MAX(observed_at) AS ts FROM member_current_state"
            ).fetchone()["ts"],
            "player_profile_at": conn.execute(
                "SELECT MAX(fetched_at) AS ts FROM player_profile_snapshots"
            ).fetchone()["ts"],
            "battle_fact_at": conn.execute(
                "SELECT MAX(battle_time) AS ts FROM member_battle_facts"
            ).fetchone()["ts"],
            "war_state_at": conn.execute(
                "SELECT MAX(observed_at) AS ts FROM war_current_state"
            ).fetchone()["ts"],
            "contextual_memory_at": conn.execute(
                "SELECT MAX(created_at) AS ts FROM clan_memories"
            ).fetchone()["ts"],
        }
        latest_raw = conn.execute(
            "SELECT endpoint, entity_key, fetched_at FROM raw_api_payloads ORDER BY fetched_at DESC, payload_id DESC LIMIT 1"
        ).fetchone()
        endpoint_counts = _rowdicts(
            conn.execute(
                "SELECT endpoint, COUNT(*) AS count, MAX(fetched_at) AS last_fetched_at "
                "FROM raw_api_payloads GROUP BY endpoint ORDER BY count DESC, endpoint ASC"
            ).fetchall()
        )
        current_season_id = get_current_season_id(conn=conn)
        stale_targets = len(get_player_intel_refresh_targets(limit=500, stale_after_hours=6, conn=conn))
        latest_signal = conn.execute(
            "SELECT signal_type, signal_date FROM signal_log ORDER BY signal_date DESC, signal_type ASC LIMIT 1"
        ).fetchone()
        memory_index_status = conn.execute(
            "SELECT value FROM clan_memory_index_status WHERE key = 'sqlite_vec_enabled'"
        ).fetchone()
        roster_summary = get_clan_roster_summary(conn=conn)
        size_bytes = None
        if db_path and os.path.exists(db_path):
            size_bytes = os.path.getsize(db_path)
        return {
            "db_path": db_path,
            "db_size_bytes": size_bytes,
            "schema_version": schema_version,
            "schema_display": schema_display,
            "counts": dict(counts),
            "roster_summary": roster_summary,
            "freshness": freshness,
            "latest_raw_payload": dict(latest_raw) if latest_raw else None,
            "raw_payloads_by_endpoint": endpoint_counts,
            "current_season_id": current_season_id,
            "stale_player_intel_targets": stale_targets,
            "latest_signal": dict(latest_signal) if latest_signal else None,
            "contextual_memory": {
                "sqlite_vec_enabled": bool(memory_index_status and str(memory_index_status["value"]) == "1"),
                "latest_memory_at": freshness["contextual_memory_at"],
                "total": dict(counts).get("contextual_memory_count", 0),
                "leader_notes": dict(counts).get("contextual_leader_notes", 0),
                "inferences": dict(counts).get("contextual_inferences", 0),
                "system_notes": dict(counts).get("contextual_system_notes", 0),
            },
        }
    finally:
        if close:
            conn.close()


def get_database_status(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        db_row = conn.execute("PRAGMA database_list").fetchone()
        db_path = db_row["file"] if db_row else None
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        db_size_bytes = None
        wal_size_bytes = None
        shm_size_bytes = None
        if db_path and os.path.exists(db_path):
            db_size_bytes = os.path.getsize(db_path)
            wal_path = f"{db_path}-wal"
            shm_path = f"{db_path}-shm"
            if os.path.exists(wal_path):
                wal_size_bytes = os.path.getsize(wal_path)
            if os.path.exists(shm_path):
                shm_size_bytes = os.path.getsize(shm_path)

        table_names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
            ).fetchall()
        ]

        size_by_name = {}
        try:
            for row in conn.execute(
                "SELECT name, SUM(pgsize) AS total_bytes FROM dbstat GROUP BY name"
            ).fetchall():
                size_by_name[row["name"]] = row["total_bytes"] or 0
        except Exception:
            size_by_name = {}

        tables = []
        for table_name in table_names:
            quoted = table_name.replace('"', '""')
            row_count = conn.execute(
                f'SELECT COUNT(*) AS cnt FROM "{quoted}"'
            ).fetchone()["cnt"]
            tables.append({
                "name": table_name,
                "row_count": row_count,
                "approx_bytes": size_by_name.get(table_name),
            })

        tables.sort(
            key=lambda item: (
                -(item.get("approx_bytes") or -1),
                -(item.get("row_count") or 0),
                item["name"],
            )
        )

        return {
            "db_path": db_path,
            "schema_version": schema_version,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "journal_mode": journal_mode,
            "db_size_bytes": db_size_bytes,
            "wal_size_bytes": wal_size_bytes,
            "shm_size_bytes": shm_size_bytes,
            "table_count": len(tables),
            "tables": tables,
        }
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
