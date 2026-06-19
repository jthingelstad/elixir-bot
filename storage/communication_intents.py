"""Durable communication intents for proactive Elixir decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

import db as _db
from db import managed_connection

INTENT_PLANNED = "planned"
INTENT_DELIVERED = "delivered"
INTENT_FAILED = "failed"
INTENT_SKIPPED = "skipped"

INTENT_STATUSES = {
    INTENT_PLANNED,
    INTENT_DELIVERED,
    INTENT_FAILED,
    INTENT_SKIPPED,
}

__all__ = [
    "INTENT_DELIVERED",
    "INTENT_FAILED",
    "INTENT_PLANNED",
    "INTENT_SKIPPED",
    "create_awareness_post_intent",
    "create_awareness_coverage_gap_intent",
    "create_awareness_skip_intent",
    "get_communication_intent",
    "get_communication_intent_by_id",
    "get_communication_trace_for_message",
    "link_communication_intent_event",
    "list_recent_communication_intents",
    "mark_communication_intent_delivered",
    "mark_communication_intent_failed",
    "mark_communication_intent_skipped",
    "upsert_communication_intent",
]


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str, ensure_ascii=False)


def _json_list(value) -> str:
    if value is None:
        items = []
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value if item is not None and str(item).strip()]
    else:
        items = [str(value)]
    return json.dumps(sorted(dict.fromkeys(items)), sort_keys=True, ensure_ascii=False)


def _loads_dict(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _loads_list(value) -> list:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return loaded if isinstance(loaded, list) else []


def _short_hash(value) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()[:16]


def _source_key(signal: dict | None) -> str | None:
    signal = signal or {}
    for key in ("signal_key", "signal_log_type", "source_signal_key"):
        value = _clean_text(signal.get(key))
        if value:
            return value
    parts = [
        _source_type(signal) or "signal",
        _clean_text(signal.get("signal_date")) or "",
        _clean_text(
            signal.get("tag")
            or signal.get("player_tag")
            or signal.get("member_tag")
            or signal.get("target_player_tag")
        ) or "",
        _clean_text(signal.get("season_id")) or "",
        _clean_text(signal.get("week") or signal.get("section_index")) or "",
        _clean_text(signal.get("day_number") or signal.get("period_index")) or "",
        _clean_text(signal.get("milestone") or signal.get("card_name") or signal.get("award_type")) or "",
    ]
    basis = "|".join(parts).strip("|")
    if basis:
        return basis
    return f"signal:{_short_hash(signal)}"


def _source_type(signal: dict | None) -> str | None:
    signal = signal or {}
    return _clean_text(signal.get("signal_type") or signal.get("type"))


def _event_key(signal: dict | None) -> str | None:
    signal = signal or {}
    return _clean_text(signal.get("event_key") or signal.get("source_event_key"))


def _signal_tag(signal: dict | None) -> str | None:
    signal = signal or {}
    for key in ("target_player_tag", "player_tag", "member_tag", "tag"):
        value = _clean_text(signal.get(key))
        if value:
            return _db._canon_tag(value)
    return None


def _row_to_intent(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["covers_signal_keys"] = _loads_list(item.pop("covers_signal_keys_json", "[]"))
    item["event_keys"] = _loads_list(item.pop("event_keys_json", "[]"))
    item["payload"] = _loads_dict(item.pop("payload_json", "{}"))
    return item


def _row_to_event_link(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def _normalize_status(status: str | None) -> str:
    normalized = _clean_text(status) or INTENT_PLANNED
    if normalized not in INTENT_STATUSES:
        raise ValueError(f"invalid communication intent status: {normalized}")
    return normalized


def _matching_signals(signals: list[dict] | tuple[dict, ...] | None, keys: list[str]) -> list[dict]:
    wanted = {key for key in keys if key}
    if not wanted:
        return []
    return [
        signal for signal in (signals or [])
        if isinstance(signal, dict) and _source_key(signal) in wanted
    ]


def _event_keys_for(signals: list[dict] | tuple[dict, ...] | None) -> list[str]:
    keys = []
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        key = _event_key(signal)
        if key:
            keys.append(key)
    return sorted(dict.fromkeys(keys))


def _first_signal(signals: list[dict] | tuple[dict, ...] | None) -> dict:
    for signal in signals or []:
        if isinstance(signal, dict):
            return signal
    return {}


def _content_preview(value, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        text = "\n\n".join(str(item) for item in value if item is not None)
    else:
        text = str(value)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _case_from_due_cases(due_cases: list[dict] | tuple[dict, ...] | None) -> int | None:
    for case in due_cases or []:
        if not isinstance(case, dict):
            continue
        case_id = case.get("case_id")
        if case_id is not None:
            try:
                return int(case_id)
            except (TypeError, ValueError):
                continue
    return None


def _infer_case_id(
    conn: sqlite3.Connection,
    *,
    source_signal_key: str | None = None,
    event_keys: list[str] | None = None,
    signal: dict | None = None,
    due_cases: list[dict] | tuple[dict, ...] | None = None,
) -> int | None:
    direct = (signal or {}).get("case_id")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            pass
    due_case_id = _case_from_due_cases(due_cases)
    if due_case_id is not None:
        return due_case_id

    clauses = []
    params: list = []
    if source_signal_key:
        clauses.append("source_signal_key = ?")
        params.append(source_signal_key)
    keys = [key for key in (event_keys or []) if key]
    if keys:
        placeholders = ",".join("?" * len(keys))
        clauses.append(f"source_event_key IN ({placeholders})")
        params.extend(keys)
    tag = _signal_tag(signal)
    if tag:
        clauses.append("target_player_tag = ?")
        params.append(tag)
    if not clauses:
        return None

    row = conn.execute(
        "SELECT case_id FROM decision_cases "
        f"WHERE ({' OR '.join(clauses)}) "
        "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'deferred' THEN 1 ELSE 2 END, "
        "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, updated_at DESC "
        "LIMIT 1",
        tuple(params),
    ).fetchone()
    return int(row["case_id"]) if row else None


def _infer_project_id(
    conn: sqlite3.Connection,
    *,
    event_keys: list[str] | None = None,
    signal: dict | None = None,
    situation: dict | None = None,
) -> int | None:
    direct = (signal or {}).get("project_id")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            pass

    project_snapshot = ((situation or {}).get("projects") or {}).get("war_season")
    if isinstance(project_snapshot, dict) and project_snapshot.get("project_id") is not None:
        try:
            return int(project_snapshot["project_id"])
        except (TypeError, ValueError):
            pass

    keys = [key for key in (event_keys or []) if key]
    if keys:
        placeholders = ",".join("?" * len(keys))
        row = conn.execute(
            "SELECT project_id FROM project_event_links "
            f"WHERE event_key IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            tuple(keys),
        ).fetchone()
        if row:
            return int(row["project_id"])

    season_id = _clean_text((signal or {}).get("season_id"))
    if season_id:
        row = conn.execute(
            "SELECT project_id FROM elixir_projects "
            "WHERE project_type = 'war_season' AND season_id = ? "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1",
            (season_id,),
        ).fetchone()
        if row:
            return int(row["project_id"])
    return None


@managed_connection
def link_communication_intent_event(
    *,
    intent_id: int,
    event_id: int | None = None,
    event_key: str | None = None,
    relationship: str = "evidence",
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    if event_key is None and event_id is not None:
        row = conn.execute(
            "SELECT event_key FROM game_event_stream WHERE event_id = ?",
            (int(event_id),),
        ).fetchone()
        event_key = row["event_key"] if row else None
    if event_id is None and event_key:
        row = conn.execute(
            "SELECT event_id FROM game_event_stream WHERE event_key = ?",
            (_clean_text(event_key),),
        ).fetchone()
        event_id = row["event_id"] if row else None
    clean_event_key = _clean_text(event_key)
    if not clean_event_key:
        return None

    now = _db._utcnow()
    conn.execute(
        """
        INSERT OR IGNORE INTO communication_intent_event_links (
            intent_id, event_id, event_key, relationship, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(intent_id),
            int(event_id) if event_id is not None else None,
            clean_event_key,
            _clean_text(relationship) or "evidence",
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM communication_intent_event_links "
        "WHERE intent_id = ? AND event_key = ? AND relationship = ?",
        (int(intent_id), clean_event_key, _clean_text(relationship) or "evidence"),
    ).fetchone()
    return _row_to_event_link(row)


@managed_connection
def upsert_communication_intent(
    *,
    intent_key: str,
    workflow: str,
    intent_type: str,
    status: str = INTENT_PLANNED,
    target_channel_key: str | None = None,
    target_channel_id: str | int | None = None,
    source_signal_key: str | None = None,
    source_signal_type: str | None = None,
    covers_signal_keys: list[str] | tuple[str, ...] | None = None,
    event_keys: list[str] | tuple[str, ...] | None = None,
    project_id: int | None = None,
    case_id: int | None = None,
    summary: str | None = None,
    content_preview: str | None = None,
    skipped_reason: str | None = None,
    error_detail: str | None = None,
    payload: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    clean_key = _clean_text(intent_key)
    if not clean_key:
        raise ValueError("intent_key is required")
    clean_workflow = _clean_text(workflow)
    if not clean_workflow:
        raise ValueError("workflow is required")
    clean_type = _clean_text(intent_type)
    if not clean_type:
        raise ValueError("intent_type is required")
    normalized_status = _normalize_status(status)
    now = _db._utcnow()
    conn.execute(
        """
        INSERT INTO communication_intents (
            intent_key, workflow, intent_type, status, target_channel_key,
            target_channel_id, source_signal_key, source_signal_type,
            covers_signal_keys_json, event_keys_json, project_id, case_id,
            summary, content_preview, skipped_reason, error_detail,
            payload_json, created_at, updated_at, delivered_at, failed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(intent_key) DO UPDATE SET
            workflow = excluded.workflow,
            intent_type = excluded.intent_type,
            status = excluded.status,
            target_channel_key = COALESCE(excluded.target_channel_key, communication_intents.target_channel_key),
            target_channel_id = COALESCE(excluded.target_channel_id, communication_intents.target_channel_id),
            source_signal_key = COALESCE(excluded.source_signal_key, communication_intents.source_signal_key),
            source_signal_type = COALESCE(excluded.source_signal_type, communication_intents.source_signal_type),
            covers_signal_keys_json = excluded.covers_signal_keys_json,
            event_keys_json = excluded.event_keys_json,
            project_id = COALESCE(excluded.project_id, communication_intents.project_id),
            case_id = COALESCE(excluded.case_id, communication_intents.case_id),
            summary = COALESCE(excluded.summary, communication_intents.summary),
            content_preview = COALESCE(excluded.content_preview, communication_intents.content_preview),
            skipped_reason = COALESCE(excluded.skipped_reason, communication_intents.skipped_reason),
            error_detail = excluded.error_detail,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at,
            delivered_at = CASE
                WHEN excluded.status = 'delivered' THEN excluded.updated_at
                WHEN excluded.status = 'planned' THEN NULL
                ELSE communication_intents.delivered_at
            END,
            failed_at = CASE
                WHEN excluded.status = 'failed' THEN excluded.updated_at
                WHEN excluded.status = 'planned' THEN NULL
                ELSE communication_intents.failed_at
            END
        """,
        (
            clean_key,
            clean_workflow,
            clean_type,
            normalized_status,
            _clean_text(target_channel_key),
            str(target_channel_id) if target_channel_id is not None else None,
            _clean_text(source_signal_key),
            _clean_text(source_signal_type),
            _json_list(covers_signal_keys),
            _json_list(event_keys),
            int(project_id) if project_id is not None else None,
            int(case_id) if case_id is not None else None,
            _clean_text(summary),
            _clean_text(content_preview),
            _clean_text(skipped_reason),
            _clean_text(error_detail),
            _json_dumps(payload or {}),
            now,
            now,
            now if normalized_status == INTENT_DELIVERED else None,
            now if normalized_status == INTENT_FAILED else None,
        ),
    )
    conn.commit()
    intent = get_communication_intent(clean_key, conn=conn) or {}
    for event_key in event_keys or []:
        link_communication_intent_event(
            intent_id=intent["intent_id"],
            event_key=str(event_key),
            conn=conn,
        )
    return intent


@managed_connection
def create_awareness_post_intent(
    post: dict,
    signals: list[dict] | tuple[dict, ...] | None = None,
    *,
    workflow: str | None = None,
    situation: dict | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    covers = [str(key) for key in (post or {}).get("covers_signal_keys") or [] if key]
    covered_signals = _matching_signals(signals, covers)
    source_signal = _first_signal(covered_signals or signals)
    event_keys = _event_keys_for(covered_signals)
    source_signal_key = covers[0] if covers else _source_key(source_signal)
    source_signal_type = _source_type(source_signal)
    channel_key = _clean_text((post or {}).get("channel"))
    event_type = _clean_text((post or {}).get("event_type")) or "awareness_update"
    key_basis = {
        "workflow": _clean_text(workflow) or "awareness",
        "intent_type": "post",
        "channel": channel_key,
        "event_type": event_type,
        "covers": covers,
        "content": (post or {}).get("content"),
        "summary": (post or {}).get("summary"),
    }
    intent_key = f"awareness:post:{_short_hash(key_basis)}"
    case_id = _infer_case_id(
        conn,
        source_signal_key=source_signal_key,
        event_keys=event_keys,
        signal=source_signal,
    )
    project_id = _infer_project_id(
        conn,
        event_keys=event_keys,
        signal=source_signal,
        situation=situation,
    )
    return upsert_communication_intent(
        intent_key=intent_key,
        workflow=_clean_text(workflow) or "awareness",
        intent_type="post",
        status=INTENT_PLANNED,
        target_channel_key=channel_key,
        source_signal_key=source_signal_key,
        source_signal_type=source_signal_type,
        covers_signal_keys=covers,
        event_keys=event_keys,
        project_id=project_id,
        case_id=case_id,
        summary=(post or {}).get("summary"),
        content_preview=_content_preview((post or {}).get("content")),
        payload={
            "source": "awareness_loop",
            "post": post or {},
            "covered_signal_count": len(covered_signals),
        },
        conn=conn,
    )


@managed_connection
def create_awareness_coverage_gap_intent(
    signals: list[dict] | tuple[dict, ...] | None = None,
    *,
    workflow: str | None = None,
    reason: str | None = None,
    situation: dict | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    signal_list = [signal for signal in (signals or []) if isinstance(signal, dict)]
    if not signal_list:
        return None
    source_signal = _first_signal(signal_list)
    source_signal_key = _source_key(source_signal)
    source_signal_type = _source_type(source_signal)
    covers = [_source_key(signal) for signal in signal_list if _source_key(signal)]
    event_keys = _event_keys_for(signal_list)
    case_id = _infer_case_id(
        conn,
        source_signal_key=source_signal_key,
        event_keys=event_keys,
        signal=source_signal,
    )
    project_id = _infer_project_id(
        conn,
        event_keys=event_keys,
        signal=source_signal,
        situation=situation,
    )
    clean_reason = _clean_text(reason) or "required signal was not covered by the awareness post plan"
    key_basis = {
        "workflow": _clean_text(workflow) or "awareness",
        "intent_type": "coverage_gap",
        "signals": covers,
        "reason": clean_reason,
    }
    return upsert_communication_intent(
        intent_key=f"awareness:coverage_gap:{_short_hash(key_basis)}",
        workflow=_clean_text(workflow) or "awareness",
        intent_type="coverage_gap",
        status=INTENT_FAILED,
        source_signal_key=source_signal_key,
        source_signal_type=source_signal_type,
        covers_signal_keys=covers,
        event_keys=event_keys,
        project_id=project_id,
        case_id=case_id,
        summary="Awareness coverage gap",
        error_detail=clean_reason,
        payload={
            "source": "awareness_loop",
            "reason": clean_reason,
            "signals_uncovered": len(signal_list),
        },
        conn=conn,
    )


@managed_connection
def create_awareness_skip_intent(
    signals: list[dict] | tuple[dict, ...] | None = None,
    *,
    workflow: str | None = None,
    skipped_reason: str | None = None,
    situation: dict | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    signal_list = [signal for signal in (signals or []) if isinstance(signal, dict)]
    due_cases = ((situation or {}).get("decision_cases") or {}).get("due") or []
    if not signal_list and not due_cases:
        return None
    source_signal = _first_signal(signal_list)
    source_signal_key = _source_key(source_signal)
    source_signal_type = _source_type(source_signal)
    covers = [_source_key(signal) for signal in signal_list if _source_key(signal)]
    event_keys = _event_keys_for(signal_list)
    case_id = _infer_case_id(
        conn,
        source_signal_key=source_signal_key,
        event_keys=event_keys,
        signal=source_signal,
        due_cases=due_cases,
    )
    project_id = _infer_project_id(
        conn,
        event_keys=event_keys,
        signal=source_signal,
        situation=situation,
    )
    key_basis = {
        "workflow": _clean_text(workflow) or "awareness",
        "intent_type": "skip",
        "signals": covers,
        "due_case_ids": [
            case.get("case_id") for case in due_cases
            if isinstance(case, dict) and case.get("case_id") is not None
        ],
        "reason": skipped_reason,
    }
    return upsert_communication_intent(
        intent_key=f"awareness:skip:{_short_hash(key_basis)}",
        workflow=_clean_text(workflow) or "awareness",
        intent_type="skip",
        status=INTENT_SKIPPED,
        source_signal_key=source_signal_key,
        source_signal_type=source_signal_type,
        covers_signal_keys=covers,
        event_keys=event_keys,
        project_id=project_id,
        case_id=case_id,
        skipped_reason=skipped_reason,
        payload={
            "source": "awareness_loop",
            "skipped_reason": skipped_reason,
            "signals_considered": len(signal_list),
            "due_cases_considered": len(due_cases),
        },
        conn=conn,
    )


@managed_connection
def mark_communication_intent_delivered(
    intent_id: int,
    *,
    target_channel_id: str | int | None = None,
    message_ids: list[str | int] | tuple[str | int, ...] | None = None,
    payload: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    row = conn.execute(
        "SELECT payload_json FROM communication_intents WHERE intent_id = ?",
        (int(intent_id),),
    ).fetchone()
    if not row:
        return None
    merged = _loads_dict(row["payload_json"])
    if payload:
        merged.update(payload)
    if message_ids is not None:
        merged["message_ids"] = [str(item) for item in message_ids if item is not None]
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE communication_intents
        SET status = 'delivered',
            target_channel_id = COALESCE(?, target_channel_id),
            payload_json = ?,
            error_detail = NULL,
            updated_at = ?,
            delivered_at = ?,
            failed_at = NULL
        WHERE intent_id = ?
        """,
        (
            str(target_channel_id) if target_channel_id is not None else None,
            _json_dumps(merged),
            now,
            now,
            int(intent_id),
        ),
    )
    conn.commit()
    return get_communication_intent_by_id(int(intent_id), conn=conn)


@managed_connection
def mark_communication_intent_failed(
    intent_id: int,
    *,
    error_detail: str | None = None,
    target_channel_id: str | int | None = None,
    payload: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    row = conn.execute(
        "SELECT payload_json FROM communication_intents WHERE intent_id = ?",
        (int(intent_id),),
    ).fetchone()
    if not row:
        return None
    merged = _loads_dict(row["payload_json"])
    if payload:
        merged.update(payload)
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE communication_intents
        SET status = 'failed',
            target_channel_id = COALESCE(?, target_channel_id),
            payload_json = ?,
            error_detail = ?,
            updated_at = ?,
            failed_at = ?
        WHERE intent_id = ?
        """,
        (
            str(target_channel_id) if target_channel_id is not None else None,
            _json_dumps(merged),
            _clean_text(error_detail),
            now,
            now,
            int(intent_id),
        ),
    )
    conn.commit()
    return get_communication_intent_by_id(int(intent_id), conn=conn)


@managed_connection
def mark_communication_intent_skipped(
    intent_id: int,
    *,
    skipped_reason: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE communication_intents
        SET status = 'skipped',
            skipped_reason = COALESCE(?, skipped_reason),
            updated_at = ?
        WHERE intent_id = ?
        """,
        (_clean_text(skipped_reason), now, int(intent_id)),
    )
    conn.commit()
    return get_communication_intent_by_id(int(intent_id), conn=conn)


@managed_connection
def get_communication_intent(intent_key: str, conn: Optional[sqlite3.Connection] = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM communication_intents WHERE intent_key = ?",
        (_clean_text(intent_key),),
    ).fetchone()
    return _row_to_intent(row)


@managed_connection
def get_communication_intent_by_id(intent_id: int, conn: Optional[sqlite3.Connection] = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM communication_intents WHERE intent_id = ?",
        (int(intent_id),),
    ).fetchone()
    return _row_to_intent(row)


@managed_connection
def list_recent_communication_intents(
    *,
    status: str | None = None,
    workflow: str | None = None,
    project_id: int | None = None,
    case_id: int | None = None,
    target_channel_key: str | None = None,
    limit: int = 25,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(_clean_text(status))
    if workflow:
        clauses.append("workflow = ?")
        params.append(_clean_text(workflow))
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(int(project_id))
    if case_id is not None:
        clauses.append("case_id = ?")
        params.append(int(case_id))
    if target_channel_key:
        clauses.append("target_channel_key = ?")
        params.append(_clean_text(target_channel_key))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM communication_intents {where} "
        "ORDER BY updated_at DESC, intent_id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 25), 200))),
    ).fetchall()
    return [_row_to_intent(row) for row in rows]


@managed_connection
def get_communication_trace_for_message(
    discord_message_id: str | int,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    message = conn.execute(
        "SELECT * FROM messages WHERE discord_message_id = ?",
        (str(discord_message_id),),
    ).fetchone()
    if not message:
        return None
    message_item = dict(message)
    intent = None
    case = None
    project = None
    events: list[dict] = []
    intent_id = message_item.get("intent_id")
    if intent_id is not None:
        intent = get_communication_intent_by_id(int(intent_id), conn=conn)
    if intent:
        if intent.get("case_id") is not None:
            case_row = conn.execute(
                "SELECT * FROM decision_cases WHERE case_id = ?",
                (int(intent["case_id"]),),
            ).fetchone()
            case = dict(case_row) if case_row else None
        if intent.get("project_id") is not None:
            project_row = conn.execute(
                "SELECT * FROM elixir_projects WHERE project_id = ?",
                (int(intent["project_id"]),),
            ).fetchone()
            project = dict(project_row) if project_row else None
        event_rows = conn.execute(
            """
            SELECT e.*
            FROM communication_intent_event_links l
            LEFT JOIN game_event_stream e ON e.event_key = l.event_key
            WHERE l.intent_id = ?
            ORDER BY l.created_at DESC, l.link_id DESC
            """,
            (int(intent["intent_id"]),),
        ).fetchall()
        for row in event_rows:
            if row["event_key"] is None:
                continue
            item = dict(row)
            item["payload"] = _loads_dict(item.pop("payload_json", "{}"))
            events.append(item)
    return {
        "message": message_item,
        "intent": intent,
        "case": case,
        "project": project,
        "events": events,
    }
