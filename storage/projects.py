"""Durable project storage for long-running Elixir missions.

Projects are the stateful layer above raw events and below Discord delivery:
an active war season, a recruiting push, or a clan-development initiative can
hold compact state, evidence links, and lifecycle fields without depending on
one message being posted.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

import db as _db
from db import managed_connection

WAR_PROJECT_TOP_LIMIT = 5
WAR_PROJECT_RECENT_COMMUNICATION_LIMIT = 5
CLAN_DEVELOPMENT_PROJECT_KEY = "clan_development:roster_health"
ONBOARDING_PROJECT_KEY = "onboarding:current"
RECRUITMENT_PROJECT_KEY = "recruitment:current"
CLAN_DEVELOPMENT_EVENT_TYPES = (
    "inactive_members",
    "promotion_recommendation",
    "demotion_recommendation",
    "kick_recommendation",
)
CLAN_DEVELOPMENT_CASE_TYPES = (
    "inactivity_review",
    "promotion_review",
    "demotion_review",
)
ONBOARDING_EVENT_TYPES = ("member_join",)
RECRUITMENT_EVENT_TYPES = (
    "discord_invite_reminder",
    "promotion_content_cycle",
)

__all__ = [
    "upsert_project",
    "get_project",
    "get_project_detail",
    "get_active_project",
    "list_projects",
    "link_project_event",
    "link_project_events_for_subject",
    "list_project_event_links",
    "refresh_operating_projects",
    "refresh_active_war_season_project",
    "get_active_war_season_project",
    "get_active_war_season_project_snapshot",
    "get_active_operating_project_snapshots",
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


def _row_to_project(row: sqlite3.Row | None, conn: sqlite3.Connection | None = None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["state"] = _json_loads(item.pop("state_json", "{}"))
    if conn is not None:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM project_event_links WHERE project_id = ?",
            (item["project_id"],),
        ).fetchone()
        item["linked_event_count"] = int(count["count"] or 0) if count else 0
    return item


def _row_to_link(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


@managed_connection
def upsert_project(
    *,
    project_key: str,
    project_type: str,
    title: str,
    status: str = "active",
    subject_type: str | None = None,
    subject_key: str | None = None,
    season_id: str | int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    last_observed_at: str | None = None,
    next_action_at: str | None = None,
    summary: str | None = None,
    state: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Create or update a durable project and return the stored record."""
    normalized_key = _clean_text(project_key)
    normalized_type = _clean_text(project_type)
    normalized_title = _clean_text(title)
    if not normalized_key:
        raise ValueError("project_key is required")
    if not normalized_type:
        raise ValueError("project_type is required")
    if not normalized_title:
        raise ValueError("title is required")

    now = _db._utcnow()
    conn.execute(
        """
        INSERT INTO elixir_projects (
            project_key, project_type, status, title, subject_type, subject_key,
            season_id, started_at, ended_at, last_observed_at, next_action_at,
            summary, state_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_key) DO UPDATE SET
            project_type = excluded.project_type,
            status = excluded.status,
            title = excluded.title,
            subject_type = excluded.subject_type,
            subject_key = excluded.subject_key,
            season_id = excluded.season_id,
            started_at = COALESCE(elixir_projects.started_at, excluded.started_at),
            ended_at = excluded.ended_at,
            last_observed_at = excluded.last_observed_at,
            next_action_at = excluded.next_action_at,
            summary = excluded.summary,
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
        """,
        (
            normalized_key,
            normalized_type,
            _clean_text(status) or "active",
            normalized_title,
            _clean_text(subject_type),
            _clean_text(subject_key),
            _clean_text(season_id),
            _clean_text(started_at) or now,
            _clean_text(ended_at),
            _clean_text(last_observed_at),
            _clean_text(next_action_at),
            _clean_text(summary),
            _json_dumps(state or {}),
            now,
            now,
        ),
    )
    conn.commit()
    return get_project(normalized_key, conn=conn) or {}


@managed_connection
def get_project(project_key: str, conn: Optional[sqlite3.Connection] = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM elixir_projects WHERE project_key = ?",
        (_clean_text(project_key),),
    ).fetchone()
    return _row_to_project(row, conn)


@managed_connection
def get_active_project(
    project_type: str,
    *,
    subject_key: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    where = ["project_type = ?", "status = 'active'"]
    params: list = [_clean_text(project_type)]
    if subject_key:
        where.append("subject_key = ?")
        params.append(_clean_text(subject_key))
    row = conn.execute(
        f"SELECT * FROM elixir_projects WHERE {' AND '.join(where)} "
        "ORDER BY updated_at DESC, project_id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return _row_to_project(row, conn)


@managed_connection
def list_projects(
    *,
    project_type: str | None = None,
    statuses: tuple[str, ...] | list[str] | None = None,
    limit: int = 25,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    where = []
    params: list = []
    if project_type:
        where.append("project_type = ?")
        params.append(_clean_text(project_type))
    clean_statuses = [status for status in (statuses or []) if _clean_text(status)]
    if clean_statuses:
        placeholders = ",".join("?" * len(clean_statuses))
        where.append(f"status IN ({placeholders})")
        params.extend(clean_statuses)
    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM elixir_projects {sql_where} "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, "
        "updated_at DESC, project_id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 25), 100))),
    ).fetchall()
    return [_row_to_project(row, conn) for row in rows]


@managed_connection
def link_project_event(
    *,
    project_id: int | None = None,
    project_key: str | None = None,
    event_id: int | None = None,
    event_key: str | None = None,
    relationship: str = "evidence",
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    if project_id is None and project_key:
        project = get_project(project_key, conn=conn)
        project_id = project["project_id"] if project else None
    if project_id is None:
        return None

    if event_key is None and event_id is not None:
        event_row = conn.execute(
            "SELECT event_key FROM game_event_stream WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        event_key = event_row["event_key"] if event_row else None
    if event_id is None and event_key:
        event_row = conn.execute(
            "SELECT event_id FROM game_event_stream WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        event_id = event_row["event_id"] if event_row else None

    normalized_event_key = _clean_text(event_key)
    if not normalized_event_key:
        return None

    now = _db._utcnow()
    conn.execute(
        """
        INSERT OR IGNORE INTO project_event_links (
            project_id, event_id, event_key, relationship, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(project_id),
            int(event_id) if event_id is not None else None,
            normalized_event_key,
            _clean_text(relationship) or "evidence",
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM project_event_links WHERE project_id = ? AND event_key = ? AND relationship = ?",
        (int(project_id), normalized_event_key, _clean_text(relationship) or "evidence"),
    ).fetchone()
    return _row_to_link(row)


@managed_connection
def link_project_events_for_subject(
    *,
    project_id: int,
    subject_type: str | None = None,
    subject_key: str | None = None,
    season_id: str | int | None = None,
    relationship: str = "evidence",
    limit: int = 500,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Link existing stream events matching a project subject."""
    where = []
    params: list = []
    if subject_type:
        where.append("subject_type = ?")
        params.append(_clean_text(subject_type))
    if subject_key:
        where.append("subject_key = ?")
        params.append(_clean_text(subject_key))
    if season_id is not None:
        where.append("season_id = ?")
        params.append(_clean_text(season_id))
    if not where:
        return 0

    rows = conn.execute(
        "SELECT event_id, event_key FROM game_event_stream "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY observed_at DESC, event_id DESC LIMIT ?",
        (*params, max(1, int(limit or 500))),
    ).fetchall()
    linked = 0
    for row in rows:
        before = conn.total_changes
        link_project_event(
            project_id=project_id,
            event_id=row["event_id"],
            event_key=row["event_key"],
            relationship=relationship,
            conn=conn,
        )
        if conn.total_changes > before:
            linked += 1
    return linked


@managed_connection
def list_project_event_links(
    project_id: int,
    *,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM project_event_links WHERE project_id = ? "
        "ORDER BY created_at DESC, link_id DESC LIMIT ?",
        (int(project_id), max(1, int(limit or 50))),
    ).fetchall()
    return [dict(row) for row in rows]


def _compact_project_case(case: dict | None) -> dict:
    case = case or {}
    return {
        "case_id": case.get("case_id"),
        "case_key": case.get("case_key"),
        "case_type": case.get("case_type"),
        "status": case.get("status"),
        "title": case.get("title"),
        "target_player_tag": case.get("target_player_tag"),
        "target_player_name": case.get("target_player_name"),
        "due_at": case.get("due_at"),
        "is_due": case.get("is_due"),
    }


def _recent_events_for_types(
    conn: sqlite3.Connection,
    *,
    event_types: tuple[str, ...],
    limit: int = 10,
) -> list[dict]:
    placeholders = ",".join("?" * len(event_types))
    rows = conn.execute(
        f"""
        SELECT event_key, event_type, observed_at, scope, subject_type, subject_key, payload_json
        FROM game_event_stream
        WHERE event_type IN ({placeholders})
        ORDER BY observed_at DESC, event_id DESC
        LIMIT ?
        """,
        (*event_types, max(1, min(int(limit or 10), 50))),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = _json_loads(item.pop("payload_json", "{}"))
        out.append(item)
    return out


def _event_type_counts(conn: sqlite3.Connection, *, event_types: tuple[str, ...]) -> dict:
    placeholders = ",".join("?" * len(event_types))
    rows = conn.execute(
        f"""
        SELECT event_type, COUNT(*) AS count, MAX(observed_at) AS last_observed_at
        FROM game_event_stream
        WHERE event_type IN ({placeholders})
        GROUP BY event_type
        ORDER BY count DESC, event_type ASC
        """,
        event_types,
    ).fetchall()
    return {
        row["event_type"]: {
            "count": int(row["count"] or 0),
            "last_observed_at": row["last_observed_at"],
        }
        for row in rows
    }


def _last_observed_for_types(conn: sqlite3.Connection, *, event_types: tuple[str, ...]) -> str | None:
    placeholders = ",".join("?" * len(event_types))
    row = conn.execute(
        f"SELECT MAX(observed_at) AS last_observed_at FROM game_event_stream WHERE event_type IN ({placeholders})",
        event_types,
    ).fetchone()
    return row["last_observed_at"] if row else None


def _link_project_events_by_types(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    event_types: tuple[str, ...],
    relationship: str = "evidence",
    limit: int = 500,
) -> int:
    placeholders = ",".join("?" * len(event_types))
    rows = conn.execute(
        f"""
        SELECT event_id, event_key
        FROM game_event_stream
        WHERE event_type IN ({placeholders})
        ORDER BY observed_at DESC, event_id DESC
        LIMIT ?
        """,
        (*event_types, max(1, min(int(limit or 500), 2000))),
    ).fetchall()
    linked = 0
    for row in rows:
        before = conn.total_changes
        link_project_event(
            project_id=project_id,
            event_id=row["event_id"],
            event_key=row["event_key"],
            relationship=relationship,
            conn=conn,
        )
        if conn.total_changes > before:
            linked += 1
    return linked


def _link_project_case_events(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    case_types: tuple[str, ...],
) -> int:
    placeholders = ",".join("?" * len(case_types))
    rows = conn.execute(
        f"""
        SELECT DISTINCT source_event_key
        FROM decision_cases
        WHERE case_type IN ({placeholders})
          AND source_event_key IS NOT NULL
        """,
        case_types,
    ).fetchall()
    linked = 0
    for row in rows:
        before = conn.total_changes
        link_project_event(
            project_id=project_id,
            event_key=row["source_event_key"],
            relationship="case_evidence",
            conn=conn,
        )
        if conn.total_changes > before:
            linked += 1
    return linked


def _assign_matching_intents_to_project(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    source_signal_types: tuple[str, ...],
) -> int:
    placeholders = ",".join("?" * len(source_signal_types))
    cursor = conn.execute(
        f"""
        UPDATE communication_intents
        SET project_id = ?, updated_at = ?
        WHERE source_signal_type IN ({placeholders})
          AND COALESCE(project_id, -1) <> ?
        """,
        (int(project_id), _db._utcnow(), *source_signal_types, int(project_id)),
    )
    conn.commit()
    return max(0, int(cursor.rowcount or 0))


def _recent_project_intents(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    limit: int = 5,
) -> list[dict]:
    from storage.communication_intents import list_recent_communication_intents

    return [
        {
            "intent_id": row.get("intent_id"),
            "status": row.get("status"),
            "workflow": row.get("workflow"),
            "intent_type": row.get("intent_type"),
            "target_channel_key": row.get("target_channel_key"),
            "source_signal_type": row.get("source_signal_type"),
            "summary": row.get("summary"),
            "updated_at": row.get("updated_at"),
        }
        for row in list_recent_communication_intents(
            project_id=project_id,
            limit=limit,
            conn=conn,
        )
    ]


def _case_status_counts(conn: sqlite3.Connection, *, case_types: tuple[str, ...]) -> dict:
    placeholders = ",".join("?" * len(case_types))
    rows = conn.execute(
        f"""
        SELECT status, COUNT(*) AS count
        FROM decision_cases
        WHERE case_type IN ({placeholders})
        GROUP BY status
        ORDER BY status
        """,
        case_types,
    ).fetchall()
    return {row["status"]: int(row["count"] or 0) for row in rows}


def _build_clan_development_state(conn: sqlite3.Connection, *, project_id: int | None = None) -> dict:
    from storage.decision_cases import list_decision_cases, list_due_decision_cases
    from storage.leader_actions import leader_action_board_snapshot

    due_cases = [
        case for case in list_due_decision_cases(limit=25, conn=conn)
        if case.get("case_type") in CLAN_DEVELOPMENT_CASE_TYPES
    ]
    open_cases = [
        case for case in list_decision_cases(
            statuses=("open", "deferred"),
            limit=50,
            conn=conn,
        )
        if case.get("case_type") in CLAN_DEVELOPMENT_CASE_TYPES
    ]
    state = {
        "kind": "clan_development",
        "case_types": list(CLAN_DEVELOPMENT_CASE_TYPES),
        "case_counts": _case_status_counts(conn, case_types=CLAN_DEVELOPMENT_CASE_TYPES),
        "due_cases": [_compact_project_case(case) for case in due_cases[:10]],
        "open_cases": [_compact_project_case(case) for case in open_cases[:10]],
        "leader_action_board": leader_action_board_snapshot(open_limit=10, decided_limit=10, conn=conn),
        "event_counts": _event_type_counts(conn, event_types=CLAN_DEVELOPMENT_EVENT_TYPES),
    }
    if project_id is not None:
        state["recent_communications"] = _recent_project_intents(conn, project_id=project_id)
    return state


def _build_onboarding_state(conn: sqlite3.Connection, *, project_id: int | None = None) -> dict:
    recent_joins = _recent_events_for_types(conn, event_types=ONBOARDING_EVENT_TYPES, limit=10)
    state = {
        "kind": "onboarding",
        "event_counts": _event_type_counts(conn, event_types=ONBOARDING_EVENT_TYPES),
        "recent_joins": [
            {
                "event_key": event.get("event_key"),
                "observed_at": event.get("observed_at"),
                "tag": (event.get("payload") or {}).get("tag") or event.get("subject_key"),
                "name": (event.get("payload") or {}).get("name"),
            }
            for event in recent_joins
        ],
    }
    if project_id is not None:
        state["recent_communications"] = _recent_project_intents(conn, project_id=project_id)
    return state


def _recent_recruiting_messages(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        """
        SELECT created_at, workflow, event_type, summary, content
        FROM messages
        WHERE event_type IN ('promotion_content_cycle', 'promotion_content_cycle_part')
           OR workflow = 'promote-the-clan'
        ORDER BY created_at DESC, message_id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 5), 25)),),
    ).fetchall()
    result = []
    for row in rows:
        content = (row["content"] or "").replace("\n", " ").strip()
        result.append({
            "created_at": row["created_at"],
            "workflow": row["workflow"],
            "event_type": row["event_type"],
            "summary": row["summary"],
            "content_preview": content[:240],
        })
    return result


def _build_recruitment_state(conn: sqlite3.Connection, *, project_id: int | None = None) -> dict:
    state = {
        "kind": "recruitment",
        "event_counts": _event_type_counts(conn, event_types=RECRUITMENT_EVENT_TYPES),
        "recent_recruiting_messages": _recent_recruiting_messages(conn),
    }
    if project_id is not None:
        state["recent_communications"] = _recent_project_intents(conn, project_id=project_id)
    return state


def _operating_project_summary(project_type: str, state: dict) -> str:
    if project_type == "clan_development":
        counts = state.get("case_counts") or {}
        due = len(state.get("due_cases") or [])
        active = int(counts.get("open") or 0) + int(counts.get("deferred") or 0)
        return f"{due} due review case(s); {active} active roster review case(s)"
    if project_type == "onboarding":
        joins = ((state.get("event_counts") or {}).get("member_join") or {}).get("count") or 0
        return f"{joins} member join event(s) in the durable stream"
    if project_type == "recruitment":
        intents = len(state.get("recent_communications") or [])
        messages = len(state.get("recent_recruiting_messages") or [])
        return f"{intents} recruiting intent(s); {messages} recent recruiting message(s)"
    return None


def _project_snapshot(project: dict | None) -> dict | None:
    if not project:
        return None
    return {
        "project_id": project.get("project_id"),
        "project_key": project.get("project_key"),
        "project_type": project.get("project_type"),
        "status": project.get("status"),
        "title": project.get("title"),
        "summary": project.get("summary"),
        "started_at": project.get("started_at"),
        "last_observed_at": project.get("last_observed_at"),
        "next_action_at": project.get("next_action_at"),
        "linked_event_count": project.get("linked_event_count") or 0,
        "state": project.get("state") or {},
    }


def _refresh_one_operating_project(
    conn: sqlite3.Connection,
    *,
    project_key: str,
    project_type: str,
    title: str,
    subject_key: str,
    event_types: tuple[str, ...],
    source_signal_types: tuple[str, ...],
    state_builder,
) -> dict:
    initial_state = state_builder(conn)
    project = upsert_project(
        project_key=project_key,
        project_type=project_type,
        status="active",
        title=title,
        subject_type="clan",
        subject_key=subject_key,
        last_observed_at=_last_observed_for_types(conn, event_types=event_types) or _db._utcnow(),
        summary=_operating_project_summary(project_type, initial_state),
        state=initial_state,
        conn=conn,
    )
    _link_project_events_by_types(
        conn,
        project_id=project["project_id"],
        event_types=event_types,
    )
    if project_type == "clan_development":
        _link_project_case_events(
            conn,
            project_id=project["project_id"],
            case_types=CLAN_DEVELOPMENT_CASE_TYPES,
        )
    _assign_matching_intents_to_project(
        conn,
        project_id=project["project_id"],
        source_signal_types=source_signal_types,
    )
    refreshed_state = state_builder(conn, project_id=project["project_id"])
    return upsert_project(
        project_key=project_key,
        project_type=project_type,
        status="active",
        title=title,
        subject_type="clan",
        subject_key=subject_key,
        last_observed_at=_last_observed_for_types(conn, event_types=event_types) or _db._utcnow(),
        summary=_operating_project_summary(project_type, refreshed_state),
        state=refreshed_state,
        conn=conn,
    )


@managed_connection
def refresh_operating_projects(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Refresh non-war standing projects: development, onboarding, recruiting."""
    return {
        "clan_development": _project_snapshot(_refresh_one_operating_project(
            conn,
            project_key=CLAN_DEVELOPMENT_PROJECT_KEY,
            project_type="clan_development",
            title="Clan Development",
            subject_key="poap_kings",
            event_types=CLAN_DEVELOPMENT_EVENT_TYPES,
            source_signal_types=CLAN_DEVELOPMENT_EVENT_TYPES,
            state_builder=_build_clan_development_state,
        )),
        "onboarding": _project_snapshot(_refresh_one_operating_project(
            conn,
            project_key=ONBOARDING_PROJECT_KEY,
            project_type="onboarding",
            title="Onboarding",
            subject_key="poap_kings",
            event_types=ONBOARDING_EVENT_TYPES,
            source_signal_types=ONBOARDING_EVENT_TYPES,
            state_builder=_build_onboarding_state,
        )),
        "recruitment": _project_snapshot(_refresh_one_operating_project(
            conn,
            project_key=RECRUITMENT_PROJECT_KEY,
            project_type="recruitment",
            title="Recruitment",
            subject_key="poap_kings",
            event_types=RECRUITMENT_EVENT_TYPES,
            source_signal_types=RECRUITMENT_EVENT_TYPES,
            state_builder=_build_recruitment_state,
        )),
    }


@managed_connection
def get_active_operating_project_snapshots(conn: Optional[sqlite3.Connection] = None) -> dict:
    return {
        "clan_development": _project_snapshot(get_active_project("clan_development", conn=conn)),
        "onboarding": _project_snapshot(get_active_project("onboarding", conn=conn)),
        "recruitment": _project_snapshot(get_active_project("recruitment", conn=conn)),
    }


def _project_events(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    limit: int = 25,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.*, l.relationship, l.created_at AS linked_at
        FROM project_event_links l
        LEFT JOIN game_event_stream e ON e.event_key = l.event_key
        WHERE l.project_id = ?
        ORDER BY l.created_at DESC, l.link_id DESC
        LIMIT ?
        """,
        (int(project_id), max(1, min(int(limit or 25), 100))),
    ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        payload_raw = item.pop("payload_json", "{}")
        item["payload"] = _json_loads(payload_raw)
        events.append(item)
    return events


def _project_intents(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    limit: int = 25,
) -> list[dict]:
    from storage.communication_intents import list_recent_communication_intents

    return list_recent_communication_intents(
        project_id=int(project_id),
        limit=limit,
        conn=conn,
    )


@managed_connection
def get_project_detail(
    project_key: str | None = None,
    *,
    project_id: int | None = None,
    event_limit: int = 25,
    intent_limit: int = 25,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    if project_id is not None:
        row = conn.execute(
            "SELECT * FROM elixir_projects WHERE project_id = ?",
            (int(project_id),),
        ).fetchone()
        project = _row_to_project(row, conn)
    else:
        project = get_project(project_key or "", conn=conn)
    if not project:
        return None
    return {
        "project": project,
        "events": _project_events(
            conn,
            project_id=int(project["project_id"]),
            limit=event_limit,
        ),
        "communication_intents": _project_intents(
            conn,
            project_id=int(project["project_id"]),
            limit=intent_limit,
        ),
    }


def _compact_member(item: dict | None) -> dict:
    item = item or {}
    return {
        "tag": item.get("tag") or item.get("player_tag"),
        "name": item.get("name") or item.get("current_name") or item.get("player_name"),
        "member_ref": item.get("member_ref"),
        "role": item.get("role"),
        "fame": item.get("fame") or item.get("total_fame"),
        "decks_used": item.get("decks_used") or item.get("decks_used_total"),
        "decks_used_today": item.get("decks_used_today"),
        "fame_today": item.get("fame_today"),
        "races_participated": item.get("races_participated"),
    }


def _compact_members(items, *, limit: int = WAR_PROJECT_TOP_LIMIT) -> list[dict]:
    compact = []
    for item in (items or [])[:limit]:
        row = {key: value for key, value in _compact_member(item).items() if value is not None}
        if row:
            compact.append(row)
    return compact


def _compact_standings(items, *, limit: int = 5) -> list[dict]:
    compact = []
    for clan in (items or [])[:limit]:
        compact.append({
            "rank": clan.get("rank"),
            "clan_tag": clan.get("clan_tag"),
            "clan_name": clan.get("clan_name"),
            "fame": clan.get("fame") or 0,
            "clan_score": clan.get("clan_score") or 0,
            "is_us": bool(clan.get("is_us")),
        })
    return compact


def _recent_war_communications(conn: sqlite3.Connection, *, limit: int = WAR_PROJECT_RECENT_COMMUNICATION_LIMIT) -> list[dict]:
    rows = conn.execute(
        """
        SELECT created_at, workflow, event_type, summary, content
        FROM messages
        WHERE author_type = 'assistant'
          AND (
            workflow IN ('river-race', 'leader-lounge')
            OR event_type LIKE 'war_%'
            OR event_type LIKE 'river_%'
          )
        ORDER BY created_at DESC, message_id DESC
        LIMIT ?
        """,
        (max(1, int(limit or WAR_PROJECT_RECENT_COMMUNICATION_LIMIT)),),
    ).fetchall()
    result = []
    for row in rows:
        content = (row["content"] or "").replace("\n", " ").strip()
        result.append({
            "created_at": row["created_at"],
            "workflow": row["workflow"],
            "event_type": row["event_type"],
            "summary": row["summary"],
            "content_preview": content[:240],
        })
    return result


def _season_started_at(conn: sqlite3.Connection, season_id, fallback: str | None) -> str | None:
    row = conn.execute(
        "SELECT MIN(observed_at) AS started_at FROM war_day_status WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    return (row["started_at"] if row else None) or fallback


def _war_project_summary(state: dict) -> str:
    season = state.get("season_id")
    week = state.get("week")
    phase = state.get("phase_display") or state.get("phase")
    race = state.get("race") or {}
    health = state.get("participation_health") or {}
    parts = [f"Season {season}"]
    if week is not None:
        parts.append(f"Week {week}")
    if phase:
        parts.append(str(phase))
    rank = race.get("rank")
    if rank:
        parts.append(f"rank {rank}")
    total = health.get("total_participants")
    engaged = health.get("engaged_count")
    if total is not None and engaged is not None:
        parts.append(f"{engaged}/{total} engaged today")
    return "; ".join(parts)


def _build_war_project_state(conn: sqlite3.Connection, current: dict) -> dict:
    from storage.war_status import get_current_war_day_state, get_war_season_summary
    from storage.war_analytics import compare_fame_per_member_to_previous_season

    season_id = current.get("season_id")
    day_state = get_current_war_day_state(conn=conn) or {}
    season_summary = get_war_season_summary(season_id=season_id, top_n=WAR_PROJECT_TOP_LIMIT, conn=conn) or {}
    comparison = compare_fame_per_member_to_previous_season(season_id=season_id, conn=conn)

    nonparticipants = season_summary.get("nonparticipants") or []
    race_standings = current.get("race_standings") or []
    phase = current.get("phase")
    day_number = (
        current.get("battle_day_number")
        if phase == "battle"
        else current.get("practice_day_number")
    )
    return {
        "kind": "war_season",
        "season_id": season_id,
        "week": current.get("week"),
        "section_index": current.get("section_index"),
        "phase": phase,
        "phase_display": current.get("phase_display"),
        "day_number": day_number,
        "observed_at": current.get("observed_at"),
        "is_colosseum_week": bool(current.get("colosseum_week")),
        "race": {
            "rank": current.get("race_rank"),
            "fame": current.get("fame") or 0,
            "repair_points": current.get("repair_points") or 0,
            "period_points": current.get("period_points") or 0,
            "clan_score": current.get("clan_score") or 0,
            "trophy_change": current.get("trophy_change"),
            "trophy_stakes_text": current.get("trophy_stakes_text"),
            "race_completed": bool(current.get("race_completed")),
            "standings": _compact_standings(race_standings),
        },
        "participation_health": {
            "war_day_key": day_state.get("war_day_key"),
            "total_participants": day_state.get("total_participants") or 0,
            "engaged_count": day_state.get("engaged_count") or 0,
            "finished_count": day_state.get("finished_count") or 0,
            "untouched_count": day_state.get("untouched_count") or 0,
            "time_left_text": day_state.get("time_left_text"),
            "top_fame_today": _compact_members(day_state.get("top_fame_today") or []),
            "used_none": _compact_members(day_state.get("used_none") or []),
        },
        "season_summary": {
            "races": season_summary.get("races") or 0,
            "total_clan_fame": season_summary.get("total_clan_fame") or 0,
            "fame_per_active_member": season_summary.get("fame_per_active_member") or 0,
            "top_contributors": _compact_members(season_summary.get("top_contributors") or []),
            "nonparticipant_count": len(nonparticipants),
        },
        "active_risks": {
            "no_participation_count": len(nonparticipants),
            "no_participation_members": _compact_members(nonparticipants),
        },
        "recent_communications": _recent_war_communications(conn),
        "prior_cycle_comparison": comparison or {
            "current_season_id": season_id,
            "previous_season_id": None,
            "direction": "unknown",
            "delta": None,
        },
    }


def _close_other_active_projects(conn: sqlite3.Connection, *, keep_project_key: str) -> None:
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE elixir_projects
        SET status = 'completed',
            ended_at = COALESCE(ended_at, ?),
            updated_at = ?
        WHERE project_type = 'war_season'
          AND status = 'active'
          AND project_key <> ?
        """,
        (now, now, keep_project_key),
    )


@managed_connection
def refresh_active_war_season_project(conn: Optional[sqlite3.Connection] = None) -> dict | None:
    """Create or refresh the durable project for the currently observed war season."""
    from storage.war_status import get_current_war_status

    current = get_current_war_status(conn=conn) or {}
    season_id = current.get("season_id")
    if season_id is None:
        return None

    project_key = f"war_season:{season_id}"
    _close_other_active_projects(conn, keep_project_key=project_key)
    state = _build_war_project_state(conn, current)
    project = upsert_project(
        project_key=project_key,
        project_type="war_season",
        status="active",
        title=f"War Season {season_id}",
        subject_type="war",
        subject_key=f"season:{season_id}",
        season_id=season_id,
        started_at=_season_started_at(conn, season_id, current.get("observed_at")),
        last_observed_at=current.get("observed_at"),
        summary=_war_project_summary(state),
        state=state,
        conn=conn,
    )
    link_project_events_for_subject(
        project_id=project["project_id"],
        subject_type="war",
        season_id=season_id,
        relationship="evidence",
        conn=conn,
    )
    return get_project(project_key, conn=conn)


@managed_connection
def get_active_war_season_project(conn: Optional[sqlite3.Connection] = None) -> dict | None:
    return get_active_project("war_season", conn=conn)


@managed_connection
def get_active_war_season_project_snapshot(conn: Optional[sqlite3.Connection] = None) -> dict | None:
    project = get_active_war_season_project(conn=conn)
    if not project:
        return None
    state = project.get("state") or {}
    return {
        "project_id": project.get("project_id"),
        "project_key": project.get("project_key"),
        "project_type": project.get("project_type"),
        "status": project.get("status"),
        "title": project.get("title"),
        "summary": project.get("summary"),
        "season_id": project.get("season_id"),
        "started_at": project.get("started_at"),
        "last_observed_at": project.get("last_observed_at"),
        "linked_event_count": project.get("linked_event_count") or 0,
        "state": {
            "season_id": state.get("season_id"),
            "week": state.get("week"),
            "phase": state.get("phase"),
            "phase_display": state.get("phase_display"),
            "day_number": state.get("day_number"),
            "race": state.get("race") or {},
            "participation_health": state.get("participation_health") or {},
            "season_summary": state.get("season_summary") or {},
            "active_risks": state.get("active_risks") or {},
            "recent_communications": state.get("recent_communications") or [],
            "prior_cycle_comparison": state.get("prior_cycle_comparison") or {},
        },
    }
