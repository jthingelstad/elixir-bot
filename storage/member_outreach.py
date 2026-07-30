"""Durable state for Elixir's DM outreach to collect missing profile info.

Phase 0: the ``member_outreach`` consent/state table, the targeting query (who is
missing a verified email and reachable by DM), and opt-out. NOTHING here contacts
a member — sending a DM is leader-gated and lives in a later phase. This module
only decides *who is eligible* and records *where each member is* in the flow.

Lifecycle (``status``): eligible → proposed (leader card raised) → sent (DM out)
→ awaiting_reply → verifying (got email, 6-digit code out) → fulfilled. Terminals:
opted_out, failed, skipped (leader declined). One row per (player_tag, field);
``field`` is ``'email'`` for now. See db.schema._apply_v6 for the table.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db import _canon_tag, _utcnow, managed_connection
from db.schema import require_columns

FIELD_EMAIL = "email"

# Statuses a member can sit in and still be re-targeted (never asked, or a prior
# attempt failed and its cooldown has passed). Everything else — in-flight or a
# terminal — is excluded from targeting.
RETARGETABLE = ("eligible", "failed")


def ensure_schema(conn) -> None:
    """Compatibility assertion; db.schema owns member_outreach creation."""
    require_columns(
        conn,
        "member_outreach",
        {"outreach_id", "player_tag", "field", "status", "consent"},
    )


@managed_connection
def eligible_targets(
    *,
    field: str = FIELD_EMAIL,
    limit: int = 25,
    now: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Current clan members who are missing a *verified* value for ``field`` and
    are reachable by DM (a primary Discord link), excluding anyone opted out,
    already in-flight, or still inside a retry cooldown."""
    ensure_schema(conn)
    now = now or _utcnow()
    rows = conn.execute(
        """
        SELECT m.player_tag,
               m.current_name AS member_name,
               du.discord_user_id,
               pm.email AS email,
               pm.email_verified_at AS email_verified_at,
               mo.status AS outreach_status,
               mo.attempts AS attempts
        FROM players m
        JOIN discord_links dl
            ON dl.player_tag = m.player_tag AND dl.is_primary = 1
        JOIN discord_users du
            ON du.discord_user_id = dl.discord_user_id
        LEFT JOIN player_metadata pm ON pm.player_tag = m.player_tag
        LEFT JOIN member_outreach mo
            ON mo.player_tag = m.player_tag AND mo.field = ?
        WHERE EXISTS (
                SELECT 1 FROM clan_memberships cm
                WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL
              )
          AND du.discord_user_id IS NOT NULL
          AND (pm.email IS NULL OR pm.email_verified_at IS NULL)
          AND COALESCE(mo.consent, '') <> 'opted_out'
          AND COALESCE(mo.status, 'eligible') IN ({placeholders})
          AND (mo.next_eligible_at IS NULL OR mo.next_eligible_at <= ?)
        ORDER BY m.current_name COLLATE NOCASE
        LIMIT ?
        """.format(placeholders=", ".join("?" * len(RETARGETABLE))),
        (field, *RETARGETABLE, now, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


@managed_connection
def get_outreach(
    member_tag: str,
    *,
    field: str = FIELD_EMAIL,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM member_outreach WHERE player_tag = ? AND field = ?",
        (_canon_tag(member_tag), field),
    ).fetchone()
    return dict(row) if row else None


@managed_connection
def upsert_outreach(
    member_tag: str,
    *,
    field: str = FIELD_EMAIL,
    status: Optional[str] = None,
    consent: Optional[str] = None,
    discord_user_id: Optional[str] = None,
    leader_action_id: Optional[int] = None,
    pending_email: Optional[str] = None,
    next_eligible_at: Optional[str] = None,
    last_asked_at: Optional[str] = None,
    last_error: Optional[str] = None,
    bump_attempts: bool = False,
    now: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Create or update a member's outreach row. Only the fields passed are
    changed (a None leaves the existing value intact); ``bump_attempts`` adds one
    to the send counter. Returns the resulting row."""
    ensure_schema(conn)
    tag = _canon_tag(member_tag)
    now = now or _utcnow()
    existing = conn.execute(
        "SELECT * FROM member_outreach WHERE player_tag = ? AND field = ?",
        (tag, field),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO member_outreach "
            "(player_tag, field, status, consent, attempts, discord_user_id, "
            "leader_action_id, pending_email, last_asked_at, next_eligible_at, "
            "last_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tag,
                field,
                status or "eligible",
                consent,
                1 if bump_attempts else 0,
                discord_user_id,
                leader_action_id,
                pending_email,
                last_asked_at,
                next_eligible_at,
                last_error,
                now,
                now,
            ),
        )
    else:
        cur = dict(existing)
        conn.execute(
            "UPDATE member_outreach SET status = ?, consent = ?, attempts = ?, "
            "discord_user_id = ?, leader_action_id = ?, pending_email = ?, "
            "last_asked_at = ?, next_eligible_at = ?, last_error = ?, updated_at = ? "
            "WHERE player_tag = ? AND field = ?",
            (
                status if status is not None else cur["status"],
                consent if consent is not None else cur["consent"],
                cur["attempts"] + 1 if bump_attempts else cur["attempts"],
                discord_user_id if discord_user_id is not None else cur["discord_user_id"],
                leader_action_id if leader_action_id is not None else cur["leader_action_id"],
                pending_email if pending_email is not None else cur["pending_email"],
                last_asked_at if last_asked_at is not None else cur["last_asked_at"],
                next_eligible_at if next_eligible_at is not None else cur["next_eligible_at"],
                last_error if last_error is not None else cur["last_error"],
                now,
                tag,
                field,
            ),
        )
    conn.commit()
    return get_outreach(tag, field=field, conn=conn)


@managed_connection
def opt_out(
    member_tag: str,
    *,
    field: str = FIELD_EMAIL,
    reason: Optional[str] = None,
    now: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Record that a member declined outreach for ``field`` — they are never
    targeted again for it (a member's 'no' is durable and unconditional)."""
    return upsert_outreach(
        member_tag,
        field=field,
        status="opted_out",
        consent="opted_out",
        last_error=(reason or None),
        now=now,
        conn=conn,
    )
