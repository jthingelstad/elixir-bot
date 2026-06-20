"""War-season snapshot computed on demand from war facts.

The durable ``elixir_projects`` subsystem was retired (Phase 5). The one piece
of genuine value it carried — the coherent River Race season story (race
standing, participation health, season summary, active risks, recent war
communications, prior-cycle comparison) — now lives here as a snapshot built
fresh from the war tables, with no project-row round-trip. Season recognition
lives in ``awards`` and long-term period history in ``event_rollups``.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db import managed_connection

WAR_PROJECT_TOP_LIMIT = 5
WAR_PROJECT_RECENT_COMMUNICATION_LIMIT = 5

__all__ = [
    "get_war_season_snapshot",
]


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


@managed_connection
def get_war_season_snapshot(conn: Optional[sqlite3.Connection] = None) -> dict | None:
    """Fresh River Race season snapshot computed directly from war facts.

    The same state the retired war-season project carried (race standing,
    participation health, season summary, active risks, recent war
    communications, prior-cycle comparison), computed on demand with no project
    row. Returns None when no season is active.
    """
    from storage.war_status import get_current_war_status

    current = get_current_war_status(conn=conn) or {}
    season_id = current.get("season_id")
    if season_id is None:
        return None
    state = _build_war_project_state(conn, current)
    return {
        "season_id": season_id,
        "summary": _war_project_summary(state),
        "started_at": _season_started_at(conn, season_id, current.get("observed_at")),
        "last_observed_at": current.get("observed_at"),
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
