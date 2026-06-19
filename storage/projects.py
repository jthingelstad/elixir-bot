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

__all__ = [
    "upsert_project",
    "get_project",
    "get_active_project",
    "link_project_event",
    "link_project_events_for_subject",
    "list_project_event_links",
    "refresh_active_war_season_project",
    "get_active_war_season_project",
    "get_active_war_season_project_snapshot",
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
