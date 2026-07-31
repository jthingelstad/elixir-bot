from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Optional

from db import (
    _canon_tag,
    _ensure_member,
    _json_or_none,
    _rowdicts,
    _upsert_member_metadata,
    _utcnow,
    managed_connection,
)

log = logging.getLogger("storage.identity")

# -- Discord identity and memory helpers -----------------------------------

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _upsert_discord_user_record(
    conn, discord_user_id, *, username=None, global_name=None, display_name=None
):
    now = _utcnow()
    row = conn.execute(
        "SELECT discord_user_id FROM discord_users WHERE discord_user_id = ?",
        (str(discord_user_id),),
    ).fetchone()
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


def _apply_discord_link(
    conn,
    discord_user_id,
    member_tag,
    *,
    username=None,
    display_name=None,
    source="manual_link",
    confidence=1.0,
    is_primary=True,
):
    # v5.1: discord_links is discord_user_id <-> player_tag; the username /
    # display-name columns dropped (they duplicate discord_users). The
    # username/display_name params are accepted for caller compatibility.
    del username, display_name
    player_tag = _ensure_member(conn, member_tag, name=None)
    if is_primary:
        conn.execute(
            "UPDATE discord_links SET is_primary = 0 WHERE discord_user_id = ?",
            (str(discord_user_id),),
        )
        conn.execute(
            "UPDATE discord_links SET is_primary = 0 WHERE player_tag = ?",
            (player_tag,),
        )
    conn.execute(
        "INSERT INTO discord_links (discord_user_id, player_tag, linked_at, source, confidence, is_primary) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(discord_user_id, player_tag) DO UPDATE SET linked_at = excluded.linked_at, source = excluded.source, confidence = excluded.confidence, is_primary = excluded.is_primary",
        (
            str(discord_user_id),
            player_tag,
            _utcnow(),
            source,
            confidence,
            1 if is_primary else 0,
        ),
    )
    return player_tag


def _infer_member_tag_for_discord_user(
    conn, discord_user_id, *, username=None, global_name=None, display_name=None
):
    existing = conn.execute(
        "SELECT dl.player_tag "
        "FROM discord_links dl "
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
            item
            for item in matches
            if item.get("match_source") in {"current_name_exact", "alias_exact"}
        ]
        if len(exact) == 1:
            matched_tags.append(exact[0]["player_tag"])

    unique_tags = list(dict.fromkeys(matched_tags))
    if len(unique_tags) == 1:
        return unique_tags[0]
    return None


def _maybe_auto_link_discord_user(
    conn, discord_user_id, *, username=None, global_name=None, display_name=None
):
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


@managed_connection
def upsert_discord_user(
    discord_user_id: str | int,
    username: Optional[str] = None,
    global_name: Optional[str] = None,
    display_name: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
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


@managed_connection
def link_discord_user_to_member(
    discord_user_id: str | int,
    member_tag: str,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    source: str = "manual_link",
    confidence: float = 1.0,
    is_primary: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
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


@managed_connection
def clear_member_discord_link(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> str:
    player_tag = _ensure_member(conn, member_tag)
    conn.execute("UPDATE discord_links SET is_primary = 0 WHERE player_tag = ?", (player_tag,))
    conn.commit()
    return player_tag


@managed_connection
def get_member_identity(
    member_tag: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    _ensure_email_schema(conn)
    row = conn.execute(
        "SELECT m.player_tag, m.current_name AS member_name, du.discord_user_id, du.username AS discord_username, du.display_name AS discord_display_name, "
        "CASE WHEN dl.discord_user_id IS NULL THEN 0 ELSE 1 END AS in_discord, "
        "pm.email AS email, pm.email_verified_at AS email_verified_at, pm.email_source AS email_source "
        "FROM players m "
        "LEFT JOIN discord_links dl ON dl.player_tag = m.player_tag AND dl.is_primary = 1 "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        "LEFT JOIN player_metadata pm ON pm.player_tag = m.player_tag "
        "WHERE m.player_tag = ?",
        (_canon_tag(member_tag),),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["email"] = out.get("email") or ""
    out["email_status"] = (
        "verified"
        if (out["email"] and out.get("email_verified_at"))
        else ("unverified" if out["email"] else "none")
    )
    return out


@managed_connection
def get_linked_member_for_discord_user(
    discord_user_id: str | int, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    row = conn.execute(
        "SELECT m.player_tag, m.current_name AS member_name, du.discord_user_id, du.username AS discord_username, du.display_name AS discord_display_name "
        "FROM discord_links dl "
        "JOIN players m ON m.player_tag = dl.player_tag "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        "WHERE dl.discord_user_id = ? AND dl.is_primary = 1",
        (str(discord_user_id),),
    ).fetchone()
    return dict(row) if row else None


# -- Member email (contact identity) ---------------------------------------
#
# Email is verified contact-identity, kept alongside the Discord link rather than
# in the generic metadata bucket. The columns physically live on player_metadata;
# transient verification challenges live in email_verifications.

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


def _ensure_email_schema(conn) -> None:
    """Compatibility assertion; db.schema owns contact-identity evolution."""
    from db.schema import require_columns

    require_columns(
        conn,
        "player_metadata",
        {"email", "email_verified_at", "email_source"},
    )
    require_columns(
        conn,
        "email_verifications",
        {"player_tag", "code_hash", "expires_at"},
    )


@managed_connection
def set_member_email(
    member_tag: str,
    email: Optional[str],
    *,
    source: str,
    verified_at: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Store a member's email. `source` records how it was set (self_service /
    admin_set); `verified_at` stamps it verified (admin-set counts as trusted)."""
    _ensure_email_schema(conn)
    tag = _canon_tag(member_tag)
    _ensure_member(conn, tag)
    _upsert_member_metadata(
        conn,
        tag,
        email=(email or "").strip() or None,
        email_verified_at=verified_at,
        email_source=(source or "").strip() or None,
    )
    conn.commit()


@managed_connection
def clear_member_email(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> None:
    _ensure_email_schema(conn)
    tag = _canon_tag(member_tag)
    _upsert_member_metadata(conn, tag, email=None, email_verified_at=None, email_source=None)
    conn.execute("DELETE FROM email_verifications WHERE player_tag = ?", (tag,))
    conn.commit()


@managed_connection
def upsert_email_challenge(
    member_tag: str,
    *,
    pending_email: str,
    code_hash: str,
    expires_at: str,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Store (or replace) the one active verification challenge for a member."""
    _ensure_email_schema(conn)
    tag = _canon_tag(member_tag)
    _ensure_member(conn, tag)
    conn.execute(
        "INSERT INTO email_verifications (player_tag, pending_email, code_hash, expires_at, attempts, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?) "
        "ON CONFLICT(player_tag) DO UPDATE SET pending_email=excluded.pending_email, "
        "code_hash=excluded.code_hash, expires_at=excluded.expires_at, attempts=0, created_at=excluded.created_at",
        (tag, pending_email.strip(), code_hash, expires_at, _utcnow()),
    )
    conn.commit()


@managed_connection
def get_email_challenge(
    member_tag: str, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    _ensure_email_schema(conn)
    row = conn.execute(
        "SELECT player_tag, pending_email, code_hash, expires_at, attempts, created_at "
        "FROM email_verifications WHERE player_tag = ?",
        (_canon_tag(member_tag),),
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def bump_email_challenge_attempts(
    member_tag: str, conn: Optional[sqlite3.Connection] = None
) -> int:
    tag = _canon_tag(member_tag)
    conn.execute(
        "UPDATE email_verifications SET attempts = attempts + 1 WHERE player_tag = ?",
        (tag,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT attempts FROM email_verifications WHERE player_tag = ?", (tag,)
    ).fetchone()
    return row["attempts"] if row else 0


@managed_connection
def clear_email_challenge(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> None:
    conn.execute(
        "DELETE FROM email_verifications WHERE player_tag = ?",
        (_canon_tag(member_tag),),
    )
    conn.commit()


@managed_connection
def list_member_emails(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Current clan members with a VERIFIED email on file (self-service verified
    or admin-set, which is trusted). The recipient list for release broadcasts —
    only confirmed-good addresses, and only members who haven't left."""
    _ensure_email_schema(conn)
    rows = conn.execute(
        "SELECT m.player_tag, m.current_name AS member_name, pm.email "
        "FROM players m "
        "JOIN player_metadata pm ON pm.player_tag = m.player_tag "
        "JOIN clan_memberships cm ON cm.player_tag = m.player_tag AND cm.left_at IS NULL "
        "WHERE COALESCE(pm.email, '') != '' AND pm.email_verified_at IS NOT NULL "
        "GROUP BY m.player_tag"
    ).fetchall()
    return [dict(r) for r in rows]


@managed_connection
def format_member_reference(
    member_or_tag: str | dict, conn: Optional[sqlite3.Connection] = None
) -> str:
    """Return the readable, LLM-safe display name for a member tag or dict.

    Normalize-at-source: when a connection + tag are available this reads the
    materialized ``players.display_name`` (stored nickname / cleaned / safe
    fallback). Without a connection it cleans and injection-guards the available
    raw name in place, so "²⁸"→"28" and an instruction-like name never surfaces.
    """
    from storage._formatting import (
        callable_name,
        injection_safe,
        preferred_display_name,
    )

    member = member_or_tag if isinstance(member_or_tag, dict) else None
    tag = (member.get("player_tag") or member.get("tag")) if member else member_or_tag
    if conn is not None and tag:
        dn = preferred_display_name(conn, tag)
        if dn:
            return dn
    if member is None:
        member = get_member_identity(member_or_tag, conn=conn) if conn is not None else None
    raw = (
        (member or {}).get("member_name")
        or (member or {}).get("current_name")
        or (member or {}).get("player_tag")
        or str(member_or_tag)
    )
    return injection_safe(callable_name(raw)) or callable_name(raw)


@managed_connection
def save_memory_episode(
    subject_type: str,
    subject_key: str,
    episode_type: str,
    summary: str,
    importance: int = 1,
    source_message_ids: Optional[list] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    conn.execute(
        "INSERT INTO memory_episodes (subject_type, subject_key, episode_type, summary, importance, source_message_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            subject_type,
            subject_key,
            episode_type,
            summary,
            importance,
            _json_or_none(source_message_ids or []),
            _utcnow(),
        ),
    )
    conn.commit()


@managed_connection
def get_memory_episodes(
    subject_type: str,
    subject_key: str,
    limit: int = 5,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    rows = conn.execute(
        "SELECT episode_type, summary, importance, source_message_ids_json, created_at "
        "FROM memory_episodes "
        "WHERE subject_type = ? AND subject_key = ? "
        "ORDER BY importance DESC, created_at DESC LIMIT ?",
        (subject_type, str(subject_key), limit),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_channel_state(
    channel_id: str | int, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    row = conn.execute(
        "SELECT channel_id, last_summary FROM channel_state WHERE channel_id = ?",
        (str(channel_id),),
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def get_system_status(conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.player import get_player_intel_refresh_targets
    from storage.roster import get_clan_roster_summary
    from storage.war import get_current_season_id

    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    schema_display = f"baseline schema (migration v{schema_version})"
    counts = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM players) AS members_total, "
        "(SELECT COUNT(*) FROM clan_memberships WHERE left_at IS NULL) AS members_active, "
        "(SELECT COUNT(*) FROM players WHERE player_tag NOT IN (SELECT player_tag FROM clan_memberships WHERE left_at IS NULL)) AS members_inactive, "
        "(SELECT COUNT(*) FROM discord_users) AS discord_users, "
        "(SELECT COUNT(*) FROM discord_links WHERE is_primary = 1) AS discord_links, "
        "(SELECT COUNT(*) FROM messages) AS message_count, "
        "(SELECT COUNT(*) FROM api_observation_receipts) AS raw_payload_count, "
        "(SELECT COUNT(*) FROM raw_api_payloads) AS raw_payload_content_count, "
        "(SELECT COUNT(*) FROM battle_events) AS battle_fact_count"
    ).fetchone()
    counts = dict(counts)
    # v5.1: memories live in the engine DB (memory.md D1) — same connection.
    try:
        from memory_store import ensure_memory_schema

        ensure_memory_schema(conn)
        mconn = conn
        try:
            mem = mconn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN kind = 'leader_note' THEN 1 ELSE 0 END) AS leader_notes, "
                "SUM(CASE WHEN kind = 'inference' THEN 1 ELSE 0 END) AS inferences, "
                "SUM(CASE WHEN kind = 'system' THEN 1 ELSE 0 END) AS system_notes, "
                "MAX(created_at) AS latest FROM memories"
            ).fetchone()
            counts["contextual_memory_count"] = mem["total"] or 0
            counts["contextual_leader_notes"] = mem["leader_notes"] or 0
            counts["contextual_inferences"] = mem["inferences"] or 0
            counts["contextual_system_notes"] = mem["system_notes"] or 0
            _memory_latest = mem["latest"]
        finally:
            pass  # shared operational connection — caller owns its lifecycle
    except Exception:
        log.debug("database status memory counts unavailable", exc_info=True)
        counts.setdefault("contextual_memory_count", 0)
        counts.setdefault("contextual_leader_notes", 0)
        counts.setdefault("contextual_inferences", 0)
        counts.setdefault("contextual_system_notes", 0)
        _memory_latest = None
    freshness = {
        "member_state_at": conn.execute(
            "SELECT MAX(observed_at) AS ts FROM player_current_state"
        ).fetchone()["ts"],
        "player_profile_at": conn.execute(
            "SELECT MAX(observed_at) AS ts FROM state_baselines WHERE entity_kind = 'player'"
        ).fetchone()["ts"],
        "battle_fact_at": conn.execute(
            "SELECT MAX(battle_time) AS ts FROM battle_events"
        ).fetchone()["ts"],
        "war_state_at": conn.execute(
            "SELECT MAX(observed_at) AS ts FROM state_baselines WHERE entity_kind = 'riverrace'"
        ).fetchone()["ts"],
        "contextual_memory_at": _memory_latest,
    }
    latest_raw = conn.execute(
        "SELECT endpoint, entity_key, fetched_at FROM api_observation_receipts "
        "ORDER BY receipt_id DESC LIMIT 1"
    ).fetchone()
    endpoint_counts = _rowdicts(
        conn.execute(
            "SELECT endpoint, COUNT(*) AS count, MAX(fetched_at) AS last_fetched_at "
            "FROM api_observation_receipts GROUP BY endpoint "
            "ORDER BY count DESC, endpoint ASC"
        ).fetchall()
    )
    current_season_id = get_current_season_id(conn=conn)
    stale_targets = len(get_player_intel_refresh_targets(limit=500, stale_after_hours=6, conn=conn))
    llm_cost_7d = conn.execute(
        """
        SELECT COUNT(*) AS calls,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,
               ROUND(COALESCE(SUM(CASE
                    WHEN model LIKE 'claude-opus-4-8%' OR model LIKE 'Codex-opus-4-8%'
                    THEN COALESCE(prompt_tokens, 0)*5 + COALESCE(cache_read_tokens, 0)*0.5 + COALESCE(cache_creation_tokens, 0)*6.25 + COALESCE(completion_tokens, 0)*25
                    WHEN model LIKE 'claude-sonnet-5%' OR model LIKE 'Codex-sonnet-5%'
                    THEN COALESCE(prompt_tokens, 0)*2 + COALESCE(cache_read_tokens, 0)*0.2 + COALESCE(cache_creation_tokens, 0)*2.5 + COALESCE(completion_tokens, 0)*10
                    WHEN model LIKE 'claude-sonnet%' OR model LIKE 'Codex-sonnet%'
                    THEN COALESCE(prompt_tokens, 0)*3 + COALESCE(cache_read_tokens, 0)*0.3 + COALESCE(cache_creation_tokens, 0)*3.75 + COALESCE(completion_tokens, 0)*15
                    WHEN model LIKE 'claude-haiku%' OR model LIKE 'Codex-haiku%'
                    THEN COALESCE(prompt_tokens, 0)*1 + COALESCE(cache_read_tokens, 0)*0.1 + COALESCE(cache_creation_tokens, 0)*1.25 + COALESCE(completion_tokens, 0)*5
                    ELSE 0 END), 0) / 1000000.0, 4) AS cost_usd
        FROM llm_calls
        WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')
        """
    ).fetchone()
    awareness_7d = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM awareness_thoughts
             WHERE at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')) AS ticks,
            -- Reads both key names: the field was `signals_by_lane` until the
            -- rename, and ~393 stored thoughts still carry that spelling. New
            -- rows use `signals_by_category`; drop the fallback once the old
            -- rows age out of every window that reads them.
            (SELECT COALESCE(SUM(json_array_length(category.value)), 0)
             FROM awareness_thoughts AS thought,
                  json_each(CASE
                      WHEN json_valid(thought.read_json)
                      THEN COALESCE(
                               json_extract(thought.read_json, '$.signals_by_category'),
                               json_extract(thought.read_json, '$.signals_by_lane'),
                               '{}')
                      ELSE '{}'
                  END) AS category
             WHERE thought.at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')) AS signals_in,
            (SELECT COUNT(*) FROM awareness_posts
             WHERE posted_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')) AS posts_delivered,
            (SELECT COUNT(*) FROM awareness_thoughts
             WHERE at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')
               AND skipped_reason LIKE '⚠️ tick failed: delivery%') AS delivery_failed,
            (SELECT COUNT(*) FROM awareness_thoughts
             WHERE at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')
               AND skipped_reason LIKE '⚠️ tick failed:%') AS failed_ticks
        """
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
        "counts": counts,
        "roster_summary": roster_summary,
        "freshness": freshness,
        "latest_raw_payload": dict(latest_raw) if latest_raw else None,
        "raw_payloads_by_endpoint": endpoint_counts,
        "current_season_id": current_season_id,
        "stale_player_intel_targets": stale_targets,
        "llm_cost_7d": dict(llm_cost_7d) if llm_cost_7d else None,
        "awareness_7d": dict(awareness_7d) if awareness_7d else None,
        "contextual_memory": {
            "latest_memory_at": freshness["contextual_memory_at"],
            "total": counts.get("contextual_memory_count", 0),
            "leader_notes": counts.get("contextual_leader_notes", 0),
            "inferences": counts.get("contextual_inferences", 0),
            "system_notes": counts.get("contextual_system_notes", 0),
        },
    }


@managed_connection
def get_database_status(conn: Optional[sqlite3.Connection] = None) -> dict:
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
    except sqlite3.OperationalError:
        # dbstat is an optional SQLite module; without it the status page just
        # omits per-table sizes. Benign, but say which build we are on.
        log.debug("dbstat unavailable — per-table sizes omitted", exc_info=True)
        size_by_name = {}

    counts_by_name: dict[str, int] = {}
    if table_names:
        # Single UNION ALL query avoids N round-trips for a list of ~30 tables.
        # table_names come from sqlite_master (not user input); quotes still
        # escaped defensively.
        union_parts = [
            f"SELECT '{name.replace(chr(39), chr(39) * 2)}' AS name, COUNT(*) AS cnt FROM \"{name.replace(chr(34), chr(34) * 2)}\""
            for name in table_names
        ]
        for row in conn.execute(" UNION ALL ".join(union_parts)).fetchall():
            counts_by_name[row["name"]] = row["cnt"]

    tables = [
        {
            "name": name,
            "row_count": counts_by_name.get(name, 0),
            "approx_bytes": size_by_name.get(name),
        }
        for name in table_names
    ]

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


@managed_connection
def build_memory_context(
    discord_user_id: Optional[str | int] = None,
    member_tag: Optional[str] = None,
    channel_id: Optional[str | int] = None,
    *,
    viewer_scope: str = "public",
    durable_memory_limit: int = 5,
    query: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """v2 (memory.md §2.3): durable memories come from ranked selection —
    member/channel match + optional query FTS + confidence + recency — via
    memory_store.select_memories on the SAME engine-DB connection (the old
    cross-DB call silently returned [] after the cut). memory_facts sections
    are retired (the table was dead since 2026-06-16); episodes and channel
    state are unchanged."""
    context = {
        "discord_user": None,
        "member": None,
        "channel": None,
    }
    include_cross_channel_conversation = (viewer_scope or "public") == "leadership"
    if include_cross_channel_conversation and discord_user_id is not None:
        key = str(discord_user_id)
        context["discord_user"] = {
            "facts": [],
            "episodes": get_memory_episodes("discord_user", key, conn=conn),
        }
    if include_cross_channel_conversation and member_tag:
        member = conn.execute(
            "SELECT player_tag FROM players WHERE player_tag = ?",
            (_canon_tag(member_tag),),
        ).fetchone()
        if member:
            key = str(member["player_tag"])
            context["member"] = {
                "facts": [],
                "episodes": get_memory_episodes("member", key, conn=conn),
            }
    if channel_id is not None:
        key = str(channel_id)
        context["channel"] = {
            "state": get_channel_state(channel_id, conn=conn),
            "episodes": get_memory_episodes("channel", key, conn=conn),
        }
    if durable_memory_limit:
        try:
            from memory_store import select_memories

            memories = select_memories(
                member_tag=_canon_tag(member_tag) if member_tag else None,
                channel_key=str(channel_id) if channel_id is not None else None,
                query=query,
                viewer_scope=viewer_scope or "public",
                limit=max(1, int(durable_memory_limit)),
                conn=conn,
            )
        except Exception:
            log.exception("durable memory selection failed")
            memories = []
        if memories:
            context["durable_memories"] = memories
    return context


# -- Core member state ------------------------------------------------------
