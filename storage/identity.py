from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Optional

log = logging.getLogger("storage.identity")

from db import (
    _canon_tag,
    _ensure_member,
    _json_or_none,
    _rowdicts,
    _upsert_member_metadata,
    _utcnow,
    managed_connection,
)

# -- Discord identity and memory helpers -----------------------------------

_DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


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
    # v5.1: discord_links is discord_user_id <-> player_tag; the username /
    # display-name columns dropped (they duplicate discord_users). The
    # username/display_name params are accepted for caller compatibility.
    del username, display_name
    player_tag = _ensure_member(conn, member_tag, name=None)
    if is_primary:
        conn.execute("UPDATE discord_links SET is_primary = 0 WHERE discord_user_id = ?", (str(discord_user_id),))
        conn.execute("UPDATE discord_links SET is_primary = 0 WHERE player_tag = ?", (player_tag,))
    conn.execute(
        "INSERT INTO discord_links (discord_user_id, player_tag, linked_at, source, confidence, is_primary) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(discord_user_id, player_tag) DO UPDATE SET linked_at = excluded.linked_at, source = excluded.source, confidence = excluded.confidence, is_primary = excluded.is_primary",
        (str(discord_user_id), player_tag, _utcnow(), source, confidence, 1 if is_primary else 0),
    )
    return player_tag


def _infer_member_tag_for_discord_user(conn, discord_user_id, *, username=None, global_name=None, display_name=None):
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

@managed_connection
def upsert_discord_user(discord_user_id: str | int, username: Optional[str] = None, global_name: Optional[str] = None, display_name: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
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
def set_member_discord_identity(member_tag: str, discord_name: str, conn: Optional[sqlite3.Connection] = None) -> int:
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


@managed_connection
def link_discord_user_to_member(discord_user_id: str | int, member_tag: str, username: Optional[str] = None, display_name: Optional[str] = None,
                                source: str = "manual_link", confidence: float = 1.0, is_primary: bool = True, conn: Optional[sqlite3.Connection] = None) -> int:
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
def get_discord_link(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT m.player_tag, m.current_name, du.discord_user_id, du.username AS discord_username, du.display_name AS discord_display_name "
        "FROM players m "
        "LEFT JOIN discord_links dl ON dl.player_tag = m.player_tag AND dl.is_primary = 1 "
        "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
        "WHERE m.player_tag = ?",
        (_canon_tag(member_tag),),
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def get_member_identity(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
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
        "verified" if (out["email"] and out.get("email_verified_at"))
        else ("unverified" if out["email"] else "none")
    )
    return out


@managed_connection
def get_linked_member_for_discord_user(discord_user_id: str | int, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
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
    """Lazy schema for live DBs cut before the email columns/table existed (v5.1
    has no forward-migration runner; fresh builds get these from schema_v51).
    Idempotent — safe to call on every email read/write."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(player_metadata)")}
    if "email" not in cols:
        conn.execute("ALTER TABLE player_metadata ADD COLUMN email TEXT DEFAULT ''")
    if "email_verified_at" not in cols:
        conn.execute("ALTER TABLE player_metadata ADD COLUMN email_verified_at TEXT")
    if "email_source" not in cols:
        conn.execute("ALTER TABLE player_metadata ADD COLUMN email_source TEXT DEFAULT ''")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS email_verifications ("
        "player_tag TEXT PRIMARY KEY REFERENCES players(player_tag) ON DELETE CASCADE, "
        "pending_email TEXT NOT NULL, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
    )


@managed_connection
def set_member_email(member_tag: str, email: Optional[str], *, source: str,
                     verified_at: Optional[str] = None,
                     conn: Optional[sqlite3.Connection] = None) -> None:
    """Store a member's email. `source` records how it was set (self_service /
    admin_set); `verified_at` stamps it verified (admin-set counts as trusted)."""
    _ensure_email_schema(conn)
    tag = _canon_tag(member_tag)
    _ensure_member(conn, tag)
    _upsert_member_metadata(conn, tag, email=(email or "").strip() or None,
                            email_verified_at=verified_at,
                            email_source=(source or "").strip() or None)
    conn.commit()


@managed_connection
def clear_member_email(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> None:
    _ensure_email_schema(conn)
    tag = _canon_tag(member_tag)
    _upsert_member_metadata(conn, tag, email=None, email_verified_at=None, email_source=None)
    conn.execute("DELETE FROM email_verifications WHERE player_tag = ?", (tag,))
    conn.commit()


@managed_connection
def upsert_email_challenge(member_tag: str, *, pending_email: str, code_hash: str,
                           expires_at: str, conn: Optional[sqlite3.Connection] = None) -> None:
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
def get_email_challenge(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    _ensure_email_schema(conn)
    row = conn.execute(
        "SELECT player_tag, pending_email, code_hash, expires_at, attempts, created_at "
        "FROM email_verifications WHERE player_tag = ?",
        (_canon_tag(member_tag),),
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def bump_email_challenge_attempts(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> int:
    tag = _canon_tag(member_tag)
    conn.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE player_tag = ?", (tag,))
    conn.commit()
    row = conn.execute("SELECT attempts FROM email_verifications WHERE player_tag = ?", (tag,)).fetchone()
    return row["attempts"] if row else 0


@managed_connection
def clear_email_challenge(member_tag: str, conn: Optional[sqlite3.Connection] = None) -> None:
    conn.execute("DELETE FROM email_verifications WHERE player_tag = ?", (_canon_tag(member_tag),))
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
def format_member_reference(member_or_tag: str | dict, conn: Optional[sqlite3.Connection] = None) -> str:
    """Return a plain display name for a member tag or identity dict.

    The returned form runs through ``callable_name`` so emoji, hearts,
    fullwidth Latin, and superscripts collapse to the readable equivalent
    a Discord user would actually type — "²⁸" becomes "28",
    "Ｓｈａｆｉｔｈ Ｎｉｈａｌ♥️" becomes "Shafith Nihal", "L-Drxgo⚡" becomes
    "L-Drxgo". Falls back to the literal name when the input is entirely
    non-Latin / non-decomposable.
    """
    from storage._formatting import callable_name

    member = member_or_tag if isinstance(member_or_tag, dict) else get_member_identity(member_or_tag, conn=conn)
    if not member:
        return callable_name(str(member_or_tag))
    raw = member.get("member_name") or member.get("current_name") or member.get("player_tag")
    return callable_name(raw)


@managed_connection
def save_memory_fact(subject_type: str, subject_key: str, fact_type: str, fact_value: str, confidence: float = 1.0,
                     source_message_id: Optional[int] = None, expires_at: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> None:
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


@managed_connection
def save_memory_episode(subject_type: str, subject_key: str, episode_type: str, summary: str, importance: int = 1,
                        source_message_ids: Optional[list] = None, conn: Optional[sqlite3.Connection] = None) -> None:
    conn.execute(
        "INSERT INTO memory_episodes (subject_type, subject_key, episode_type, summary, importance, source_message_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (subject_type, subject_key, episode_type, summary, importance, _json_or_none(source_message_ids or []), _utcnow()),
    )
    conn.commit()


@managed_connection
def get_memory_facts(subject_type: str, subject_key: str, limit: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT fact_type, fact_value, confidence, updated_at, expires_at "
        "FROM memory_facts "
        "WHERE subject_type = ? AND subject_key = ? "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY updated_at DESC LIMIT ?",
        (subject_type, str(subject_key), _utcnow(), limit),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_memory_episodes(subject_type: str, subject_key: str, limit: int = 5, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT episode_type, summary, importance, source_message_ids_json, created_at "
        "FROM memory_episodes "
        "WHERE subject_type = ? AND subject_key = ? "
        "ORDER BY importance DESC, created_at DESC LIMIT ?",
        (subject_type, str(subject_key), limit),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_channel_state(channel_id: str | int, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    row = conn.execute(
        "SELECT channel_id, last_elixir_post_at, last_topics_json, recent_style_notes_json, last_summary "
        "FROM channel_state WHERE channel_id = ?",
        (str(channel_id),),
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def get_system_status(conn: Optional[sqlite3.Connection] = None) -> dict:
    from storage.war import get_current_season_id
    from storage.player import get_player_intel_refresh_targets
    from storage.roster import get_clan_roster_summary
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
        "(SELECT COUNT(*) FROM raw_api_payloads) AS raw_payload_count, "
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
            _memory_vec = None  # embeddings retired in v5.1 (memory.md D2)
        finally:
            pass  # shared operational connection — caller owns its lifecycle
    except Exception:
        counts.setdefault("contextual_memory_count", 0)
        counts.setdefault("contextual_leader_notes", 0)
        counts.setdefault("contextual_inferences", 0)
        counts.setdefault("contextual_system_notes", 0)
        _memory_latest = None
        _memory_vec = None
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
        "SELECT recognition_key AS signal_type, claimed_at AS signal_date FROM recognition_ledger "
        "ORDER BY claimed_at DESC LIMIT 1"
    ).fetchone()
    llm_cost_7d = conn.execute(
        """
        SELECT COUNT(*) AS calls,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,
               ROUND(COALESCE(SUM(CASE
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
        SELECT COUNT(*) AS ticks,
               COUNT(*) AS signals_in,
               SUM(CASE WHEN status = 'fulfilled' THEN 1 ELSE 0 END) AS posts_delivered,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS fallback_failed,
               SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS failed_ticks
        FROM communication_intents
        WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')
        """
    ).fetchone()
    memory_index_status = _memory_vec
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
        "latest_signal": dict(latest_signal) if latest_signal else None,
        "llm_cost_7d": dict(llm_cost_7d) if llm_cost_7d else None,
        "awareness_7d": dict(awareness_7d) if awareness_7d else None,
        "contextual_memory": {
            "sqlite_vec_enabled": bool(memory_index_status and str(memory_index_status["value"]) == "1"),
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
def build_memory_context(discord_user_id: Optional[str | int] = None, member_tag: Optional[str] = None, channel_id: Optional[str | int] = None, *, viewer_scope: str = "public", durable_memory_limit: int = 5, query: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
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
