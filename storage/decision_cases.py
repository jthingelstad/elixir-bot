"""Durable decision cases for operational recommendations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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

__all__ = [
    "CASE_DEFERRED",
    "CASE_DISMISSED",
    "CASE_OPEN",
    "CASE_RESOLVED",
    "upsert_decision_case",
    "get_decision_case",
    "get_decision_case_by_id",
    "list_decision_cases",
    "list_due_decision_cases",
    "defer_decision_case",
    "resolve_decision_case",
    "link_leader_action_to_case",
    "upsert_decision_cases_from_signals",
    "upsert_member_review_case",
    "decision_case_snapshot",
]


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str, ensure_ascii=False)


def _json_loads(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
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


def _case_key(case_type: str, subject_key: str | None = None, target_player_tag: str | None = None) -> str:
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
    source_signal_key: str | None = None,
    source_signal_type: str | None = None,
    source_event_key: str | None = None,
    due_at: str | None = None,
    status: str = CASE_OPEN,
    state: Optional[dict] = None,
    case_key: str | None = None,
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
    now = _db._utcnow()
    clean_status = _normalize_case_status(status)
    conn.execute(
        """
        INSERT INTO decision_cases (
            case_key, case_type, status, subject_type, subject_key,
            target_player_tag, target_player_name, title, recommendation,
            rationale, priority, source_signal_key, source_signal_type,
            source_event_key, opened_at, due_at, state_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            source_signal_key = COALESCE(excluded.source_signal_key, decision_cases.source_signal_key),
            source_signal_type = COALESCE(excluded.source_signal_type, decision_cases.source_signal_type),
            source_event_key = COALESCE(excluded.source_event_key, decision_cases.source_event_key),
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
            _clean_text(source_signal_key),
            _clean_text(source_signal_type),
            _clean_text(source_event_key),
            now,
            _clean_text(due_at),
            _json_dumps(state or {}),
            now,
            now,
        ),
    )
    conn.commit()
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
    clean_statuses = [status for status in (statuses or [CASE_OPEN, CASE_DEFERRED]) if status]
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
        "status IN (?, ?)",
        "(due_at IS NULL OR due_at <= ?)",
    ]
    params: list = [CASE_OPEN, CASE_DEFERRED, current]
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
def defer_decision_case(
    case_id: int,
    *,
    due_at: str,
    resolution: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE decision_cases
        SET status = ?, due_at = ?, resolution = COALESCE(?, resolution), updated_at = ?
        WHERE case_id = ?
        """,
        (CASE_DEFERRED, _clean_text(due_at), _clean_text(resolution), now, int(case_id)),
    )
    conn.commit()
    return get_decision_case_by_id(case_id, conn=conn)


@managed_connection
def resolve_decision_case(
    case_id: int,
    *,
    status: str = CASE_RESOLVED,
    resolution: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    clean_status = _normalize_case_status(status)
    if clean_status not in {CASE_RESOLVED, CASE_DISMISSED}:
        raise ValueError("resolved case status must be resolved or dismissed")
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE decision_cases
        SET status = ?, resolved_at = ?, resolution = ?, due_at = NULL, updated_at = ?
        WHERE case_id = ?
        """,
        (clean_status, now, _clean_text(resolution), now, int(case_id)),
    )
    conn.commit()
    return get_decision_case_by_id(case_id, conn=conn)


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
    conn.commit()


def _member_case_priority(member: dict) -> int:
    try:
        days = float(member.get("days_inactive") or member.get("battle_days_ago") or 0)
        threshold = float(member.get("threshold_days") or 0)
    except (TypeError, ValueError):
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
    source_signal_key: str | None = None,
    source_signal_type: str | None = None,
    source_event_key: str | None = None,
    due_at: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
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
        source_signal_key=source_signal_key,
        source_signal_type=source_signal_type,
        source_event_key=source_event_key,
        due_at=due_at,
        state={"member": dict(member)},
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
                    source_signal_key=signal_key,
                    source_signal_type=signal_type,
                    source_event_key=signal.get("event_key"),
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
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    return {
        "due": [_compact_case(case) for case in list_due_decision_cases(limit=due_limit, conn=conn)],
        "open": [_compact_case(case) for case in list_decision_cases(limit=open_limit, conn=conn)],
    }
