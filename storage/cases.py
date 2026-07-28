"""Decision-case reads and writes — v5.1 (schema.md §7.3).

Ported from the retired storage/decision_cases.py (Gen B) onto the carried
decision_cases table: source_event_key/source_event_type became
source_event_key/source_event_type; the Gen A game_event_stream lookup is
gone.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import db as _db
from db import managed_connection

CASE_OPEN = "open"
CASE_DEFERRED = "deferred"
CASE_RESOLVED = "resolved"
CASE_DISMISSED = "dismissed"

CASE_TYPES = {
    "inactivity_review",
    "promotion_review",
    "demotion_review",
    "war_recovery",
}

# How long an open member-review case may sit uncorroborated by the management
# engine (state='none') before the backstop dismisses it. >= 7 guarantees a full
# weekly review has run for promote/demote grains before we reap the case.
_CASE_RECONCILE_GRACE_DAYS = 7

_LEADER_REVIEW_CASES = {
    "kick_recommendation": {
        "case_type": "inactivity_review",
        "title": "Inactivity review",
        "priority": 50,
    },
    "demotion_recommendation": {
        "case_type": "demotion_review",
        "title": "Demotion review",
        "priority": 30,
    },
    "promotion_recommendation": {
        "case_type": "promotion_review",
        "title": "Promotion review",
        "priority": 20,
    },
}

__all__ = [
    "CASE_DEFERRED",
    "CASE_DISMISSED",
    "CASE_OPEN",
    "CASE_RESOLVED",
    "backfill_decision_cases_from_leader_actions",
    "upsert_decision_case",
    "get_decision_case",
    "get_decision_case_by_id",
    "list_decision_cases",
    "list_due_decision_cases",
    "resolve_decision_case",
    "reconcile_departed_member_cases",
    "raise_departure_verification_cards",
    "expire_departure_verification_cards",
    "reconcile_uncorroborated_member_cases",
    "sync_terminal_leader_action_cases",
    "link_leader_action_to_case",
    "upsert_decision_cases_from_signals",
    "upsert_member_review_case",
    "decision_case_snapshot",
]


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_dumps(value) -> str:
    return json.dumps(
        value if value is not None else {},
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )


def _json_loads(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except TypeError, json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _case_due(status: str | None, due_at: str | None, *, now: str | None = None) -> bool:
    if status not in {CASE_OPEN, CASE_DEFERRED}:
        return False
    if not due_at:
        return status == CASE_OPEN
    due = _parse_utc(due_at)
    current = _parse_utc(now) or _utcnow_dt()
    return bool(due and due <= current)


def _row_to_case(row: sqlite3.Row | None, *, now: str | None = None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["state"] = _json_loads(item.pop("state_json", "{}"))
    item["is_due"] = _case_due(item.get("status"), item.get("due_at"), now=now)
    return item


def _case_key(
    case_type: str, subject_key: str | None = None, target_player_tag: str | None = None
) -> str:
    if target_player_tag:
        return f"{case_type}:member:{_db._canon_tag(target_player_tag)}"
    if subject_key:
        return f"{case_type}:{subject_key}"
    raise ValueError("subject_key or target_player_tag is required")


def _normalize_case_status(status: str | None) -> str:
    clean = _clean_text(status) or CASE_OPEN
    if clean not in {CASE_OPEN, CASE_DEFERRED, CASE_RESOLVED, CASE_DISMISSED}:
        raise ValueError(f"invalid decision case status: {clean}")
    return clean


@managed_connection
def upsert_decision_case(
    *,
    case_type: str,
    title: str,
    recommendation: str | None = None,
    rationale: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    target_player_tag: str | None = None,
    target_player_name: str | None = None,
    priority: int = 0,
    source_event_key: str | None = None,
    source_event_type: str | None = None,
    due_at: str | None = None,
    status: str = CASE_OPEN,
    state: Optional[dict] = None,
    case_key: str | None = None,
    allow_reopen: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    clean_type = _clean_text(case_type)
    if not clean_type:
        raise ValueError("case_type is required")
    clean_title = _clean_text(title)
    if not clean_title:
        raise ValueError("title is required")
    canon_tag = _db._canon_tag(target_player_tag) if target_player_tag else None
    clean_subject_key = _clean_text(subject_key) or (f"member:{canon_tag}" if canon_tag else None)
    clean_subject_type = _clean_text(subject_type) or ("member" if canon_tag else None)
    clean_case_key = _clean_text(case_key) or _case_key(
        clean_type,
        subject_key=clean_subject_key,
        target_player_tag=canon_tag,
    )
    # QA H20/H21: a resolved/dismissed case is a decision a leader deliberately
    # closed. An automated/awareness write must not silently reopen it — only a
    # controlled re-nomination path (allow_reopen=True) may. Leave it untouched.
    if not allow_reopen:
        existing = get_decision_case(clean_case_key, conn=conn)
        if existing and existing.get("status") in (CASE_RESOLVED, CASE_DISMISSED):
            return existing
    now = _db._utcnow()
    clean_status = _normalize_case_status(status)
    conn.execute(
        """
        INSERT INTO decision_cases (
            case_key, case_type, status, subject_type, subject_key,
            target_player_tag, target_player_name, title, recommendation,
            rationale, priority, source_event_key, source_event_type,
            opened_at, due_at, state_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(case_key) DO UPDATE SET
            status = CASE
                WHEN decision_cases.status IN ('resolved', 'dismissed') THEN excluded.status
                WHEN decision_cases.status = 'deferred' AND decision_cases.due_at IS NOT NULL AND decision_cases.due_at > excluded.updated_at THEN decision_cases.status
                ELSE excluded.status
            END,
            subject_type = COALESCE(excluded.subject_type, decision_cases.subject_type),
            subject_key = COALESCE(excluded.subject_key, decision_cases.subject_key),
            target_player_tag = COALESCE(excluded.target_player_tag, decision_cases.target_player_tag),
            target_player_name = COALESCE(excluded.target_player_name, decision_cases.target_player_name),
            title = excluded.title,
            recommendation = COALESCE(excluded.recommendation, decision_cases.recommendation),
            rationale = COALESCE(excluded.rationale, decision_cases.rationale),
            priority = MAX(decision_cases.priority, excluded.priority),
            source_event_key = COALESCE(excluded.source_event_key, decision_cases.source_event_key),
            source_event_type = COALESCE(excluded.source_event_type, decision_cases.source_event_type),
            due_at = CASE
                WHEN decision_cases.status = 'deferred' AND decision_cases.due_at IS NOT NULL AND decision_cases.due_at > excluded.updated_at THEN decision_cases.due_at
                ELSE COALESCE(excluded.due_at, decision_cases.due_at)
            END,
            resolved_at = CASE WHEN decision_cases.status IN ('resolved', 'dismissed') THEN NULL ELSE decision_cases.resolved_at END,
            resolution = CASE WHEN decision_cases.status IN ('resolved', 'dismissed') THEN NULL ELSE decision_cases.resolution END,
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
        """,
        (
            clean_case_key,
            clean_type,
            clean_status,
            clean_subject_type,
            clean_subject_key,
            canon_tag,
            _clean_text(target_player_name),
            clean_title,
            _clean_text(recommendation),
            _clean_text(rationale),
            int(priority or 0),
            _clean_text(source_event_key),
            _clean_text(source_event_type),
            now,
            _clean_text(due_at),
            _json_dumps(state or {}),
            now,
            now,
        ),
    )
    return get_decision_case(clean_case_key, conn=conn) or {}


@managed_connection
def get_decision_case(case_key: str, conn: Optional[sqlite3.Connection] = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM decision_cases WHERE case_key = ?",
        (_clean_text(case_key),),
    ).fetchone()
    return _row_to_case(row)


@managed_connection
def get_decision_case_by_id(case_id: int, conn: Optional[sqlite3.Connection] = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM decision_cases WHERE case_id = ?",
        (int(case_id),),
    ).fetchone()
    return _row_to_case(row)


@managed_connection
def list_decision_cases(
    *,
    statuses: tuple[str, ...] | list[str] | None = None,
    case_type: str | None = None,
    limit: int = 20,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    clean_statuses = [status for status in (statuses or [CASE_OPEN]) if status]
    where = []
    params: list = []
    if clean_statuses:
        placeholders = ",".join("?" * len(clean_statuses))
        where.append(f"status IN ({placeholders})")
        params.extend(clean_statuses)
    if case_type:
        where.append("case_type = ?")
        params.append(case_type)
    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM decision_cases {sql_where} "
        "ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, priority DESC, updated_at DESC "
        "LIMIT ?",
        (*params, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    return [_row_to_case(row) for row in rows]


@managed_connection
def list_due_decision_cases(
    *,
    case_type: str | None = None,
    limit: int = 20,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    current = _clean_text(now) or _db._utcnow()
    where = [
        "status = ?",
        "(due_at IS NULL OR due_at <= ?)",
    ]
    params: list = [CASE_OPEN, current]
    if case_type:
        where.append("case_type = ?")
        params.append(case_type)
    rows = conn.execute(
        f"SELECT * FROM decision_cases WHERE {' AND '.join(where)} "
        "ORDER BY priority DESC, COALESCE(due_at, opened_at) ASC, case_id ASC LIMIT ?",
        (*params, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    return [_row_to_case(row, now=current) for row in rows]


@managed_connection
def resolve_decision_case(
    case_id: int,
    *,
    status: str = CASE_RESOLVED,
    resolution: str | None = None,
    resolved_at: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    clean_status = _normalize_case_status(status)
    if clean_status not in {CASE_RESOLVED, CASE_DISMISSED}:
        raise ValueError("resolved case status must be resolved or dismissed")
    now = _clean_text(resolved_at) or _db._utcnow()
    conn.execute(
        """
        UPDATE decision_cases
        SET status = ?, resolved_at = ?, resolution = ?, due_at = NULL, updated_at = ?
        WHERE case_id = ?
        """,
        (clean_status, now, _clean_text(resolution), now, int(case_id)),
    )
    return get_decision_case_by_id(case_id, conn=conn)


# Mirror of engine.recognition.recognizers.KICK_SUPPRESS_DAYS. A departure is
# attributed to a kick when a kick_recommendation was marked done within this
# window before the member left. Kept local to avoid a storage->engine import
# (engine depends on storage, not the reverse).
_KICK_ATTRIBUTION_DAYS = 14


def _departure_was_kick(conn, tag: str, left_at: str | None) -> bool:
    """True when a member's departure is attributable to a kick.

    A leader who verified the departure via a ``departure_verification`` card is
    AUTHORITATIVE — the resulting ``clan_memberships.leave_source``
    (``leader_verified_kick`` / ``leader_verified_leave``) overrides the
    inference. Absent a verified signal, fall back to the C1 rule: a ``done``
    kick_recommendation within the attribution window before they left. A kick
    means the leadership action was ENACTED, meaningfully different from a leave.
    """
    verified = conn.execute(
        """SELECT leave_source FROM clan_memberships
           WHERE UPPER(player_tag) = UPPER(?) AND left_at IS NOT NULL
           ORDER BY left_at DESC, membership_id DESC LIMIT 1""",
        (tag,),
    ).fetchone()
    src = (verified["leave_source"] if verified else None) or ""
    if src == "leader_verified_kick":
        return True
    if src == "leader_verified_leave":
        return False
    params: list = [tag]
    window = ""
    anchor = _parse_utc(left_at) if left_at else None
    if anchor:
        cutoff = (anchor - timedelta(days=_KICK_ATTRIBUTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        window = "AND COALESCE(decided_at, proposed_at) >= ?"
        params.append(cutoff)
    row = conn.execute(
        f"""SELECT 1 FROM leader_action_recommendations
            WHERE action_type = 'kick_recommendation' AND target_player_tag = ?
              AND status = 'done' AND COALESCE(is_test, 0) = 0 {window} LIMIT 1""",
        params,
    ).fetchone()
    return row is not None


@managed_connection
def reconcile_departed_member_cases(
    *,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Close open/deferred member-review cases whose subject has left the clan,
    distinguishing a KICK from an organic leave.

    The decision-case store is the durable member-review layer the awareness
    brain and #leader-actions read. Unlike ``member_management`` — which drops a
    row the moment a member leaves — it is NOT self-reconciling: an open
    inactivity / promotion / demotion case for a departed member lingers
    indefinitely and resurfaces as a stale recommendation (observed 2026-07-09
    loop #12: 5 of 9 kick reviews targeted members who had already left, four via
    the 2026-07-04 roster cut). We key off current membership rather than a leave
    event that some paths never emit.

    Kicks are meaningfully different from leaves. When a fulfilled kick explains
    the departure, the review's recommended action was ENACTED, so the case is
    **resolved** (resolution ``kicked``). An organic departure makes the review
    moot, so the case is **dismissed** (resolution ``member_left``).
    """
    current = _clean_text(now) or _db._utcnow()
    placeholders = ",".join("?" * len(CASE_TYPES))
    rows = conn.execute(
        f"""SELECT dc.case_id, dc.case_type, dc.target_player_tag, dc.target_player_name,
                   (SELECT MAX(cm.left_at) FROM clan_memberships cm
                    WHERE UPPER(cm.player_tag) = UPPER(dc.target_player_tag)) AS left_at
            FROM decision_cases dc
            WHERE dc.status IN (?, ?)
              AND dc.case_type IN ({placeholders})
              AND dc.target_player_tag IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM clan_memberships cm
                  WHERE UPPER(cm.player_tag) = UPPER(dc.target_player_tag)
                    AND cm.left_at IS NULL
              )""",
        (CASE_OPEN, CASE_DEFERRED, *sorted(CASE_TYPES)),
    ).fetchall()
    reconciled: list[dict] = []
    for row in rows:
        if _departure_was_kick(conn, row["target_player_tag"], row["left_at"]):
            status, resolution, outcome = CASE_RESOLVED, "kicked", "kicked"
        else:
            status, resolution, outcome = CASE_DISMISSED, "member_left", "left"
        resolve_decision_case(
            int(row["case_id"]),
            status=status,
            resolution=resolution,
            resolved_at=current,
            conn=conn,
        )
        reconciled.append(
            {
                "case_id": int(row["case_id"]),
                "case_type": row["case_type"],
                "target_player_tag": row["target_player_tag"],
                "target_player_name": row["target_player_name"],
                "outcome": outcome,
            }
        )
    return reconciled


# A departure is only carded if it was detected within this window — avoids
# flooding #leader-actions with cards for historical leaves on first deploy.
_DEPARTURE_CARD_LOOKBACK_DAYS = 2


@managed_connection
def raise_departure_verification_cards(
    *,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """For members who left recently with an unverified (inferred) leave_source,
    either settle an already-enacted kick silently or raise a #leader-actions
    card asking leaders to confirm LEAVE vs KICK (+ optional comment).

    A leave and a kick are very different signals, but the roster diff can't tell
    them apart — a member kicked for behavior (no kick card) looks like a leave.
    Scope: all recent departures EXCEPT those already attributable to a ``done``
    kick card (settled to ``leader_verified_kick``, no card — the kick is known).
    The public goodbye is held until a leader verifies (see runtime/awareness)."""
    from storage.leader_actions import create_leader_action_recommendation

    current = _clean_text(now) or _db._utcnow()
    anchor = _parse_utc(current) or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (anchor - timedelta(days=_DEPARTURE_CARD_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT cm.membership_id, cm.player_tag, cm.left_at,
                  COALESCE(p.display_name, p.current_name, cm.player_tag) AS name,
                  CAST(julianday(cm.left_at) - julianday(cm.joined_at) AS INTEGER) AS tenure_days
           FROM clan_memberships cm
           LEFT JOIN players p ON p.player_tag = cm.player_tag
           WHERE cm.left_at IS NOT NULL
             AND cm.leave_source = 'roster_diff'
             AND cm.left_at >= ?""",
        (cutoff,),
    ).fetchall()
    raised: list[dict] = []
    for row in rows:
        tag = row["player_tag"]
        if _departure_was_kick(conn, tag, row["left_at"]):
            # Already an enacted kick (done kick card in window) — record it
            # authoritatively and raise no card; the kick is already known.
            conn.execute(
                "UPDATE clan_memberships SET leave_source = 'leader_verified_kick' "
                "WHERE membership_id = ?",
                (row["membership_id"],),
            )
            continue
        signal_key = f"engine:departure:{tag}:{row['left_at']}"
        exists = conn.execute(
            """SELECT 1 FROM leader_action_recommendations
               WHERE action_type = 'departure_verification'
                 AND (source_signal_key = ?
                      OR (UPPER(target_player_tag) = UPPER(?) AND status = 'proposed'))
                 AND COALESCE(is_test, 0) = 0 LIMIT 1""",
            (signal_key, tag),
        ).fetchone()
        if exists:
            continue
        name = row["name"]
        tenure = row["tenure_days"]
        tenure_txt = f"{tenure} days" if tenure is not None else "unknown tenure"
        create_leader_action_recommendation(
            action_type="departure_verification",
            objective=f"Confirm departure: did {name} leave or get kicked?",
            prompt_text=(
                f"{name} is no longer in the clan (tenure {tenure_txt}). Elixir can't tell "
                f"if they LEFT on their own or were KICKED — click one. On a LEAVE, add a "
                f"note with any context for the farewell (e.g. “alt account of X”, or a "
                f"detail worth a mention) and I'll compose the goodbye with it. No public "
                f"goodbye is posted until this is verified; a KICK is never announced."
            ),
            rationale=f"Departure detected {row['left_at']}; leave_source unverified (roster_diff).",
            target_player_tag=tag,
            target_player_name=name,
            source_signal_key=signal_key,
            source_signal_type="engine_departure",
            conn=conn,
        )
        raised.append({"player_tag": tag, "player_name": name, "left_at": row["left_at"]})
    return raised


# Unanswered departure cards auto-settle to a benign unverified leave after this
# many days, so the action board stays clean and no stale goodbye ever fires.
_DEPARTURE_CARD_TIMEOUT_DAYS = 3


@managed_connection
def expire_departure_verification_cards(
    *,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Auto-settle departure_verification cards leaders never answered. After the
    timeout, treat the departure as an (unverified) organic leave: mark
    leave_source='leave_unverified' and close the card WITHOUT a public goodbye —
    goodbyes only fire on a prompt, explicit LEAVE verification, so they stay
    timely and honest."""
    current = _clean_text(now) or _db._utcnow()
    anchor = _parse_utc(current) or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (anchor - timedelta(days=_DEPARTURE_CARD_TIMEOUT_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT action_id, target_player_tag FROM leader_action_recommendations
           WHERE action_type = 'departure_verification' AND status = 'proposed'
             AND COALESCE(is_test, 0) = 0
             AND COALESCE(proposed_at, created_at) <= ?""",
        (cutoff,),
    ).fetchall()
    expired: list[dict] = []
    for row in rows:
        tag = row["target_player_tag"]
        if tag:
            conn.execute(
                """UPDATE clan_memberships SET leave_source = 'leave_unverified'
                   WHERE membership_id = (
                       SELECT membership_id FROM clan_memberships
                       WHERE UPPER(player_tag) = UPPER(?) AND left_at IS NOT NULL
                         AND leave_source = 'roster_diff'
                       ORDER BY left_at DESC, membership_id DESC LIMIT 1
                   )""",
                (tag,),
            )
        conn.execute(
            """UPDATE leader_action_recommendations
               SET status = 'done', decided_at = ?, decided_by_discord_user_id = 'system',
                   decision_emoji = '⌛',
                   decision_note = COALESCE(decision_note, ?),
                   outcome_json = ?, updated_at = ?
               WHERE action_id = ?""",
            (
                current,
                "Auto-settled: no leader verification within the window; treated as "
                "an organic leave. No public goodbye was posted.",
                _json_dumps({"classification": "leave_unverified", "auto_settled": True}),
                current,
                row["action_id"],
            ),
        )
        expired.append({"action_id": row["action_id"], "target_player_tag": tag})
    return expired


@managed_connection
def reconcile_uncorroborated_member_cases(
    *,
    grace_days: int = _CASE_RECONCILE_GRACE_DAYS,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Dismiss OPEN member-review cases the management engine no longer
    corroborates. A case whose backing ``member_management`` state is 'none' (the
    engine has zero interest on that dimension) for at least ``grace_days``, with
    no open 'proposed' leader-action card, is stale: it was created by the brain
    or a signal path (``upsert_member_review_case``) but the deterministic engine
    never backed it. Ratko #365 was the motivating case — a promotion_review with
    ``promote_state='none'`` and no card, which nothing closed, so it nagged the
    awareness read as "due" forever (Loop #26).

    The grace window lets a fresh flag breathe: ``kick_state`` recomputes each
    tick but promote/demote roll weekly, so ``grace_days`` >= 7 guarantees at
    least one full weekly review has run and still returned 'none' before we
    dismiss. building / at_risk / eligible cases are kept — the engine IS tracking
    them. Departed members go through ``reconcile_departed_member_cases``; this is
    the in-clan-but-uncorroborated backstop. Idempotent — steady-state touches 0.
    """
    current = _clean_text(now) or _db._utcnow()
    try:
        parsed = datetime.fromisoformat(current.rstrip("Z"))
    except ValueError, AttributeError:
        parsed = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (parsed - timedelta(days=max(0, int(grace_days)))).strftime("%Y-%m-%dT%H:%M:%S")

    dismissed: list[dict] = []
    for case_type, state_col, action_type in (
        ("inactivity_review", "kick_state", "kick_recommendation"),
        ("promotion_review", "promote_state", "promotion_recommendation"),
        ("demotion_review", "demote_state", "demotion_recommendation"),
    ):
        rows = conn.execute(
            f"""SELECT dc.case_id, dc.target_player_tag, dc.target_player_name
                FROM decision_cases dc
                LEFT JOIN member_management mm
                  ON UPPER(mm.player_tag) = UPPER(dc.target_player_tag)
                WHERE dc.case_type = ?
                  AND dc.status = ?
                  AND dc.target_player_tag IS NOT NULL
                  AND dc.opened_at <= ?
                  AND COALESCE(mm.{state_col}, 'none') = 'none'
                  AND EXISTS (
                      SELECT 1 FROM clan_memberships cm
                      WHERE UPPER(cm.player_tag) = UPPER(dc.target_player_tag)
                        AND cm.left_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM leader_action_recommendations lar
                      WHERE UPPER(lar.target_player_tag) = UPPER(dc.target_player_tag)
                        AND lar.action_type = ?
                        AND lar.status = 'proposed'
                        AND COALESCE(lar.is_test, 0) = 0
                  )""",
            (case_type, CASE_OPEN, cutoff, action_type),
        ).fetchall()
        for row in rows:
            resolve_decision_case(
                int(row["case_id"]),
                status=CASE_DISMISSED,
                resolution=(
                    "Auto-dismissed: management engine shows no active "
                    f"candidacy (state=none) after {grace_days}d and no open card."
                ),
                resolved_at=current,
                conn=conn,
            )
            dismissed.append(
                {
                    "case_id": int(row["case_id"]),
                    "case_type": case_type,
                    "target_player_tag": row["target_player_tag"],
                    "target_player_name": row["target_player_name"],
                    "outcome": "uncorroborated",
                }
            )
    return dismissed


def _leader_action_case_config(action_type: str | None) -> dict | None:
    return _LEADER_REVIEW_CASES.get((action_type or "").strip())


def _is_action_expired(action: dict, *, now: str | None = None) -> bool:
    if action.get("status") != "proposed" or not action.get("expires_at"):
        return False
    expires_at = _parse_utc(action.get("expires_at"))
    current = _parse_utc(now) or _utcnow_dt()
    return bool(expires_at and expires_at <= current)


def _case_lifecycle_from_action(action: dict, *, now: str | None = None) -> tuple[str, str]:
    status = (action.get("status") or "").strip()
    if _is_action_expired(action, now=now):
        return CASE_DISMISSED, "expired"
    if status == "proposed":
        return CASE_OPEN, "recommended"
    if status == "deferred":
        # Defer retired 2026-07-10: any legacy deferred action closes its case
        # (a decline). The engine re-nominates on sustained evidence instead.
        return CASE_DISMISSED, "rejected"
    if status == "done":
        return CASE_RESOLVED, "accepted"
    if status == "rejected":
        return CASE_DISMISSED, "rejected"
    return CASE_OPEN, status or "unknown"


def _leader_action_resolution(action: dict, outcome: str) -> str | None:
    note = _clean_text(action.get("decision_note"))
    if note:
        return note
    if outcome == "accepted":
        return "Leader accepted the recommended action."
    if outcome == "rejected":
        return "Leader declined the recommended action."
    if outcome == "expired":
        return "Recommendation expired before a leader decision was recorded."
    return None


def _leader_action_case_state(action: dict, *, outcome: str, backfilled_at: str) -> dict:
    return {
        "leader_action": {
            "action_id": action.get("action_id"),
            "action_key": action.get("action_key"),
            "action_type": action.get("action_type"),
            "objective": action.get("objective"),
            "status": action.get("status"),
            "outcome": outcome,
            "source_message_id": action.get("source_message_id"),
            "proposed_at": action.get("proposed_at"),
            "decided_at": action.get("decided_at"),
            "decided_by_discord_user_id": action.get("decided_by_discord_user_id"),
            "decision_emoji": action.get("decision_emoji"),
            "decision_note": action.get("decision_note"),
            "decision_note_at": action.get("decision_note_at"),
            "defer_days": action.get("defer_days"),
            "deferred_until": action.get("deferred_until"),
            "expires_at": action.get("expires_at"),
        },
        "backfill": {
            "source": "leader_action_recommendations",
            "backfilled_at": backfilled_at,
        },
    }


def _source_event_key_for_signal(
    source_event_key: str | None, *, conn: sqlite3.Connection
) -> str | None:
    signal_key = _clean_text(source_event_key)
    if not signal_key:
        return None
    row = conn.execute(
        "SELECT NULL AS event_key WHERE 0",  # Gen A game_event_stream retired
        (signal_key,),
    ).fetchone()
    return row["event_key"] if row else None


@managed_connection
def backfill_decision_cases_from_leader_actions(
    *,
    now: str | None = None,
    limit: int | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Create/link decision cases for historical member-review action cards."""
    review_types = tuple(_LEADER_REVIEW_CASES)
    params: list = list(review_types)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(1, min(int(limit or 1), 1000)))
    rows = conn.execute(
        f"""
        SELECT *
        FROM leader_action_recommendations
        WHERE action_type IN ({",".join("?" * len(review_types))})
          AND COALESCE(is_test, 0) = 0
          AND target_player_tag IS NOT NULL
        ORDER BY proposed_at ASC, action_id ASC
        {limit_sql}
        """,
        params,
    ).fetchall()

    backfilled_at = _clean_text(now) or _db._utcnow()
    summary = {
        "scanned": 0,
        "created": 0,
        "updated": 0,
        "linked": 0,
        "resolved": 0,
        "dismissed": 0,
        "expired": 0,
        "skipped": 0,
    }
    for row in rows:
        action = dict(row)
        summary["scanned"] += 1
        config = _leader_action_case_config(action.get("action_type"))
        tag = (
            _db._canon_tag(action.get("target_player_tag"))
            if action.get("target_player_tag")
            else None
        )
        if not config or not tag:
            summary["skipped"] += 1
            continue

        case_type = config["case_type"]
        case_key = _case_key(case_type, target_player_tag=tag)
        existing = get_decision_case(case_key, conn=conn)
        case_status, outcome = _case_lifecycle_from_action(action, now=now)
        due_at = None  # defer retired 2026-07-10 — no case carries a revisit timer
        name = _clean_text(action.get("target_player_name")) or tag
        title = f"{config['title']}: {name}"
        recommendation = _clean_text(action.get("prompt_text")) or f"Review {name}."
        rationale = _clean_text(action.get("rationale")) or _leader_action_resolution(
            action, outcome
        )
        case = upsert_decision_case(
            case_type=case_type,
            title=title,
            recommendation=recommendation,
            rationale=rationale,
            subject_type="member",
            subject_key=f"member:{tag}",
            target_player_tag=tag,
            target_player_name=name,
            priority=int(config.get("priority") or 0),
            source_event_key=action.get("source_event_key"),
            source_event_type=action.get("source_event_type"),
            due_at=due_at,
            status=case_status if case_status in {CASE_OPEN, CASE_DEFERRED} else CASE_OPEN,
            state=_leader_action_case_state(action, outcome=outcome, backfilled_at=backfilled_at),
            case_key=case_key,
            # A live leader-action card IS the authority; reflect it even over a
            # prior closure (this reconstructs case state from the action board).
            allow_reopen=True,
            conn=conn,
        )
        if not case:
            summary["skipped"] += 1
            continue
        if existing:
            summary["updated"] += 1
        else:
            summary["created"] += 1
        if action.get("case_id") != case["case_id"]:
            link_leader_action_to_case(action["action_id"], case["case_id"], conn=conn)
            summary["linked"] += 1

        if outcome in {"accepted", "rejected", "expired"}:
            terminal_status = CASE_RESOLVED if outcome == "accepted" else CASE_DISMISSED
            resolve_decision_case(
                case["case_id"],
                status=terminal_status,
                resolution=_leader_action_resolution(action, outcome),
                resolved_at=action.get("decided_at") or action.get("expires_at"),
                conn=conn,
            )
            if terminal_status == CASE_RESOLVED:
                summary["resolved"] += 1
            else:
                summary["dismissed"] += 1
            if outcome == "expired":
                summary["expired"] += 1
    return summary


@managed_connection
def sync_terminal_leader_action_cases(
    *,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Propagate a terminal leader-action state onto its backing decision case.

    When a leader marks a kick / promotion / demotion card ``done`` (or rejects
    it), the matching member-review case should move to match — a done kick
    RESOLVES the inactivity review (action enacted), a done promotion RESOLVES
    the promotion review, a rejection DISMISSES it. (Legacy ``deferred`` rows are
    treated as declines and DISMISS the case — defer retired 2026-07-10.) Nothing wired this
    at runtime before (``backfill_decision_cases_from_leader_actions`` was a
    manual one-shot), so a completed kick left its ``inactivity_review`` case
    OPEN and it resurfaced as a stale recommendation to the awareness brain
    (2026-07-09 loop #12). This closes the loop at decision time — before the
    member even leaves the roster, so the membership reconciler is a pure
    backstop.

    RESOLVE-ONLY: it matches an EXISTING case by key and never creates one
    (case creation stays the signal/recognizer path's job). Idempotent — once a
    case is closed it is skipped, so steady-state this touches ~0 rows.
    """
    review_types = tuple(_LEADER_REVIEW_CASES)
    rows = conn.execute(
        f"""SELECT * FROM leader_action_recommendations
            WHERE action_type IN ({",".join("?" * len(review_types))})
              AND COALESCE(is_test, 0) = 0
              AND target_player_tag IS NOT NULL
              AND status IN ('done', 'rejected', 'deferred')
            ORDER BY COALESCE(decided_at, proposed_at) ASC, action_id ASC""",
        review_types,
    ).fetchall()
    synced: list[dict] = []
    for row in rows:
        action = dict(row)
        config = _leader_action_case_config(action.get("action_type"))
        tag = (
            _db._canon_tag(action.get("target_player_tag"))
            if action.get("target_player_tag")
            else None
        )
        if not config or not tag:
            continue
        case = get_decision_case(_case_key(config["case_type"], target_player_tag=tag), conn=conn)
        if not case or case["status"] not in {CASE_OPEN, CASE_DEFERRED}:
            continue  # no backing case, or already closed — nothing to propagate
        _case_status, outcome = _case_lifecycle_from_action(action, now=now)
        if outcome in {"accepted", "rejected", "expired"}:
            terminal = CASE_RESOLVED if outcome == "accepted" else CASE_DISMISSED
            resolve_decision_case(
                case["case_id"],
                status=terminal,
                resolution=_leader_action_resolution(action, outcome),
                resolved_at=action.get("decided_at") or action.get("expires_at") or now,
                conn=conn,
            )
        else:
            continue
        synced.append(
            {
                "case_id": case["case_id"],
                "case_type": config["case_type"],
                "target_player_tag": tag,
                "target_player_name": case.get("target_player_name") or tag,
                "action_type": action.get("action_type"),
                "outcome": outcome,
            }
        )
    return synced


@managed_connection
def link_leader_action_to_case(
    action_id: int,
    case_id: int,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    conn.execute(
        "UPDATE leader_action_recommendations SET case_id = ?, updated_at = ? WHERE action_id = ?",
        (int(case_id), _db._utcnow(), int(action_id)),
    )


def _member_case_priority(member: dict) -> int:
    try:
        days = float(member.get("days_inactive") or member.get("battle_days_ago") or 0)
        threshold = float(member.get("threshold_days") or 0)
    except TypeError, ValueError:
        return 0
    return max(0, int(round((days - threshold) * 10)))


def _inactivity_recommendation(member: dict) -> str:
    name = member.get("name") or member.get("member_name") or member.get("tag") or "member"
    return f"Review {name} for removal from the clan."


def _inactivity_rationale(member: dict) -> str:
    name = member.get("name") or member.get("member_name") or member.get("tag") or "member"
    days = member.get("days_inactive") or member.get("battle_days_ago")
    threshold = member.get("threshold_days")
    login = member.get("login_days_ago")
    parts = [f"{name} is over the inactivity threshold"]
    if days is not None and threshold is not None:
        parts.append(f"{days} days inactive vs {threshold} day threshold")
    if login is not None:
        parts.append(f"last login {login} days ago")
    role = member.get("role")
    if role:
        parts.append(f"role {role}")
    return "; ".join(parts)


@managed_connection
def upsert_member_review_case(
    *,
    case_type: str,
    member: dict,
    title: str | None = None,
    recommendation: str | None = None,
    rationale: str | None = None,
    source_event_key: str | None = None,
    source_event_type: str | None = None,
    due_at: str | None = None,
    allow_reopen: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    # Only a type some reconciler OWNS may be created. `leadership_followup` was
    # the counter-example: it was never in CASE_TYPES, so no reconciler iterated
    # it, and all 9 rows ever created stayed open forever while being invisible
    # to leadership. The tool schemas constrain case_type to an enum, but that is
    # enforced by the model API — this makes the invariant local and true for any
    # caller, so an unclosable case cannot be created by construction.
    if case_type not in CASE_TYPES:
        raise ValueError(
            f"unknown case_type {case_type!r}: a decision case must be a type a "
            f"reconciler owns, one of {sorted(CASE_TYPES)}"
        )
    tag = member.get("tag") or member.get("player_tag") or member.get("member_tag")
    canon_tag = _db._canon_tag(tag)
    if not canon_tag:
        return None
    name = member.get("name") or member.get("member_name") or member.get("current_name")
    if case_type == "inactivity_review":
        clean_title = title or f"Inactivity review: {name or canon_tag}"
        clean_recommendation = recommendation or _inactivity_recommendation(member)
        clean_rationale = rationale or _inactivity_rationale(member)
    else:
        clean_title = title or f"{case_type.replace('_', ' ').title()}: {name or canon_tag}"
        clean_recommendation = recommendation
        clean_rationale = rationale
    return upsert_decision_case(
        case_type=case_type,
        title=clean_title,
        recommendation=clean_recommendation,
        rationale=clean_rationale,
        subject_type="member",
        subject_key=f"member:{canon_tag}",
        target_player_tag=canon_tag,
        target_player_name=name,
        priority=_member_case_priority(member),
        source_event_key=source_event_key,
        source_event_type=source_event_type,
        due_at=due_at,
        state={"member": dict(member)},
        allow_reopen=allow_reopen,
        conn=conn,
    )


@managed_connection
def upsert_decision_cases_from_signals(
    signals: list[dict] | tuple[dict, ...] | None,
    *,
    source_system: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    cases = []
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        signal_type = signal.get("type")
        signal_key = signal.get("signal_key") or signal.get("signal_log_type")
        if signal_type == "inactive_members":
            for member in signal.get("members") or []:
                if not isinstance(member, dict):
                    continue
                case = upsert_member_review_case(
                    case_type="inactivity_review",
                    member=member,
                    source_event_key=signal_key,
                    source_event_type=signal_type,
                    conn=conn,
                )
                if case:
                    cases.append(case)
    return cases


def _compact_case(case: dict) -> dict:
    return {
        "case_id": case.get("case_id"),
        "case_key": case.get("case_key"),
        "case_type": case.get("case_type"),
        "status": case.get("status"),
        "title": case.get("title"),
        "recommendation": case.get("recommendation"),
        "rationale": case.get("rationale"),
        "target_player_tag": case.get("target_player_tag"),
        "target_player_name": case.get("target_player_name"),
        "priority": case.get("priority"),
        "opened_at": case.get("opened_at"),
        "due_at": case.get("due_at"),
        "is_due": case.get("is_due"),
    }


@managed_connection
def decision_case_snapshot(
    *,
    open_limit: int = 10,
    due_limit: int = 10,
    dedupe: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Compact due/open cases. Every due case is also an open case, so by
    default ``open`` repeats the whole ``due`` list. Pass ``dedupe=True`` to
    drop that overlap — ``open`` then means "open but not currently due",
    matching "due = needs attention now; open = being monitored"."""
    due = [_compact_case(case) for case in list_due_decision_cases(limit=due_limit, conn=conn)]
    open_cases = [_compact_case(case) for case in list_decision_cases(limit=open_limit, conn=conn)]
    if dedupe:
        due_keys = {c.get("case_key") for c in due}
        open_cases = [c for c in open_cases if c.get("case_key") not in due_keys]
    return {"due": due, "open": open_cases}
