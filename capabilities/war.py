"""Canonical River Race and war-season intelligence.

Storage owns the war clock, projections, and analytics.  This module owns the
stable meaning shared by tools, awareness, reports, memory, and admin reads:
weekly fame and daily period points are distinct races, unscored ranks are
never presented as real standings, and every live answer carries freshness.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import db as db_facade
from capabilities.contracts import WarIntelligenceResult, WarSeasonViewResult
from capabilities.game_truth import get_game_truth
from engine.readiness import generation_snapshot
from storage import war as war_storage

CAPABILITY_ID = "war_intelligence"
CONTRACT_VERSION = 1


def _source(source):
    return source or war_storage


def _invoke(source, name: str, *args, conn=None, **kwargs):
    if conn is not None:
        kwargs["conn"] = conn
    return getattr(source, name)(*args, **kwargs)


@contextmanager
def _connection(source, conn=None) -> Iterator[Any]:
    if conn is not None:
        yield conn
        return
    opened = source.get_connection()
    try:
        yield opened
    finally:
        opened.close()


def _ranked(rows: list[dict] | None, *, scored: bool) -> list[dict]:
    result = []
    for item in rows or []:
        row = dict(item)
        if not scored:
            row["rank"] = None
        result.append(row)
    return result


def _is_scored(status: dict, flag: str, standings_key: str, metric: str) -> bool:
    if flag in status:
        return bool(status.get(flag))
    return float(status.get(metric) or 0) > 0 or any(
        float(row.get(metric) or 0) > 0
        for row in (status.get(standings_key) or [])
        if isinstance(row, dict)
    )


def _participant(member: dict) -> dict:
    return {
        "member_ref": member.get("member_ref") or member.get("name"),
        "player_tag": member.get("player_tag") or member.get("tag"),
        "decks_used_today": member.get("decks_used_today"),
        "decks_used_total": member.get("decks_used_total"),
        "points_today": member.get("points_today"),
        "points_total": member.get("points"),
    }


def _remaining_decks(day: dict) -> dict:
    total = int(day.get("total_participants") or 0)
    finished = int(day.get("finished_count") or 0)
    untouched = int(day.get("untouched_count") or 0)
    used_some = day.get("used_some")
    partial = (
        len(used_some)
        if isinstance(used_some, list)
        else max(0, total - finished - untouched)
    )
    remaining = max(0, total - finished)
    if partial + untouched != remaining:
        partial = max(0, remaining - untouched)
    return {
        "total": remaining,
        "partial": partial,
        "untouched": untouched,
        "finished": finished,
        "total_participants": total,
        "count_source": "used_some + used_none",
        "summary": (
            f"{remaining} participants have decks left today: "
            f"{untouched} untouched, {partial} partial."
        ),
    }


def get_war_intelligence(*, source=None, conn=None) -> WarIntelligenceResult:
    """Return the canonical live-war contract, or ``available=False``."""
    if conn is None and source in (None, db_facade, war_storage):
        active_source = source or war_storage
        with _connection(active_source) as active:
            active.execute("BEGIN")
            return get_war_intelligence(source=active_source, conn=active)
    source = _source(source)
    generation = generation_snapshot(conn) if conn is not None else None
    status = _invoke(source, "get_current_war_status", conn=conn)
    if not isinstance(status, dict):
        return {
            "capability": CAPABILITY_ID,
            "contract_version": CONTRACT_VERSION,
            "available": False,
            "sources": ["state_baselines:riverrace", "war_attendance_days"],
            "data_generation": generation,
        }

    context = _invoke(source, "build_war_now_context", conn=conn)
    context = context if isinstance(context, dict) else {}
    day = _invoke(source, "get_current_war_day_state", conn=conn)
    day = day if isinstance(day, dict) else {}
    boat_scored = _is_scored(status, "boat_scored", "race_standings", "fame")
    day_scored = _is_scored(status, "day_scored", "day_standings", "period_points")
    weekly_standings = _ranked(status.get("race_standings"), scored=boat_scored)
    daily_standings = _ranked(status.get("day_standings"), scored=day_scored)

    clock = dict(context)
    clock["race_standings"] = weekly_standings
    clock["day_standings"] = daily_standings
    current_state = dict(status)
    current_state["boat_scored"] = boat_scored
    current_state["day_scored"] = day_scored
    current_state["race_rank"] = status.get("race_rank") if boat_scored else None
    current_state["race_standings"] = weekly_standings
    current_state["day_rank"] = status.get("day_rank") if day_scored else None
    current_state["day_standings"] = daily_standings
    day_state = dict(day)
    day_state["boat_scored"] = boat_scored
    day_state["day_scored"] = day_scored
    day_state["race_rank"] = status.get("race_rank") if boat_scored else None
    day_state["day_rank"] = status.get("day_rank") if day_scored else None

    result = {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "available": True,
        "sources": [
            "state_baselines:riverrace",
            "war_attendance_days",
            "war_participation",
        ],
        "data_generation": generation,
        "observed_at": status.get("observed_at") or day.get("observed_at"),
        "current_state": current_state,
        "day_state": day_state,
        "clock": clock,
        "weekly_race": {
            "metric": "fame",
            "scored": boat_scored,
            "rank": status.get("race_rank") if boat_scored else None,
            "standings": weekly_standings,
            "fame": status.get("fame"),
            "finish_line": status.get("finish_line"),
            "completed": status.get("race_completed"),
        },
        "daily_race": {
            "metric": "period_points",
            "scored": day_scored,
            "rank": status.get("day_rank") if day_scored else None,
            "standings": daily_standings,
            "period_points": status.get("period_points"),
        },
        "projection": {
            "primary_metric": status.get("primary_metric"),
            "projected_day_fame": status.get("projected_day_fame"),
            "projected_defense_fame": status.get("projected_defense_fame"),
            "projected_fame_at_close": status.get("projected_fame_at_close"),
            "defenses_remaining": status.get("defenses_remaining"),
            "clinches_finish_today": bool(status.get("clinches_finish_today")),
        },
        "period": {
            "season_id": status.get("season_id"),
            "section_index": status.get("section_index"),
            "week": status.get("week"),
            "war_day_key": status.get("war_day_key") or day.get("war_day_key"),
            "phase": status.get("phase"),
            "phase_display": status.get("phase_display"),
            "period_type": status.get("period_type"),
            "season_week_label": status.get("season_week_label"),
            "is_colosseum_week": bool(context.get("is_colosseum_week")),
            "is_final_battle_day": bool(status.get("final_battle_day_active")),
            "is_final_practice_day": bool(status.get("final_practice_day_active")),
            "trophy_stakes_text": status.get("trophy_stakes_text"),
        },
        "engagement": {
            "war_day_key": day.get("war_day_key"),
            "observed_at": day.get("observed_at"),
            "clan_fame": day.get("clan_fame"),
            "clan_period_points": day.get("period_points"),
            "day_rank": status.get("day_rank") if day_scored else None,
            "total_participants": day.get("total_participants"),
            "engaged_count": day.get("engaged_count"),
            "finished_count": day.get("finished_count"),
            "untouched_count": day.get("untouched_count"),
            "remaining_decks": _remaining_decks(day),
            "top_points_total": day.get("top_points_total") or [],
            "used_all_4": [_participant(m) for m in (day.get("used_all_4") or [])],
            "used_some": [_participant(m) for m in (day.get("used_some") or [])],
            "used_none": [_participant(m) for m in (day.get("used_none") or [])],
        },
    }
    result["game_truth"] = get_game_truth(topic="river_race", live_war=result)
    return result


def _standings_freshness(source, season_id=None, *, conn=None) -> dict:
    state = _invoke(source, "get_current_war_day_state", conn=conn) or {}
    state_season = state.get("season_id")
    target_season = season_id if season_id is not None else state_season
    war_day_key = state.get("war_day_key")
    section_index = state.get("section_index")

    finalized_races = 0
    current_section_finalized = False
    if target_season is not None:
        finalized_races = (
            _invoke(source, "count_war_races_for_season", int(target_season), conn=conn)
            or 0
        )
        if section_index is not None:
            current_section_finalized = bool(
                _invoke(
                    source,
                    "is_war_section_finalized",
                    int(target_season),
                    int(section_index),
                    conn=conn,
                )
            )

    current_week_included = bool(
        war_day_key
        and section_index is not None
        and state_season == target_season
        and not current_section_finalized
    )
    as_of = None
    if current_week_included:
        as_of = _invoke(
            source,
            "get_latest_war_participant_snapshot_observed_at",
            war_day_key,
            conn=conn,
        )
    if not as_of and target_season is not None:
        as_of = _invoke(
            source, "get_latest_war_race_finish_time", int(target_season), conn=conn
        )
    return {
        "as_of": as_of,
        "current_week_included": current_week_included,
        "current_week_war_day_key": war_day_key if current_week_included else None,
        "current_week_section_index": section_index if current_week_included else None,
        "finalized_races": finalized_races,
        "narration_hint": (
            "Quote `as_of` for current questions. `current_week_included` says "
            "whether today's battles are part of this result."
        ),
    }


def _current_week_top(source, limit: int, *, conn=None) -> list[dict]:
    try:
        with _connection(source, conn) as active:
            row = active.execute(
                "SELECT season_id, MAX(section_index) AS section FROM war_participation "
                "WHERE season_id = (SELECT MAX(season_id) FROM war_participation) "
                "GROUP BY season_id"
            ).fetchone()
            if not row:
                return []
            rows = active.execute(
                """SELECT COALESCE(p.display_name, p.current_name) AS name,
                          wp.fame AS points, wp.decks_used
                   FROM war_participation wp
                   JOIN players p ON p.player_tag = wp.player_tag
                   JOIN clan_memberships cm ON cm.player_tag = wp.player_tag
                        AND cm.left_at IS NULL
                   WHERE wp.season_id = ? AND wp.section_index = ?
                   ORDER BY wp.fame DESC LIMIT ?""",
                (row["season_id"], row["section"], limit),
            ).fetchall()
            return [dict(item) for item in rows]
    except Exception:
        return []


def get_war_season_view(
    *,
    view: str = "summary",
    season_id=None,
    metric: str = "points",
    limit: int = 10,
    source=None,
    conn=None,
) -> WarSeasonViewResult:
    """Return one versioned war-season facet under the ``data`` key."""
    if conn is None and source in (None, db_facade, war_storage):
        active_source = source or war_storage
        with _connection(active_source) as active:
            active.execute("BEGIN")
            return get_war_season_view(
                view=view,
                season_id=season_id,
                metric=metric,
                limit=limit,
                source=active_source,
                conn=active,
            )
    source = _source(source)
    limit = max(1, min(int(limit or 10), 100))

    if view == "summary":
        summary_kwargs = {"top_n": limit}
        if season_id is not None:
            summary_kwargs["season_id"] = season_id
        data = _invoke(source, "get_war_season_summary", conn=conn, **summary_kwargs)
        if isinstance(data, dict) and season_id is None:
            data = dict(data)
            data["current_week_top"] = _current_week_top(source, limit, conn=conn)
    elif view == "snapshot":
        data = _invoke(source, "get_war_season_snapshot", conn=conn)
    elif view == "history":
        data = _invoke(source, "get_war_season_history", limit=limit, conn=conn)
    elif view == "standings":
        clean_metric = "points" if metric == "fame" else metric
        if clean_metric == "points":
            data = {
                "season_id": season_id,
                "metric": "points",
                "freshness": _standings_freshness(source, season_id, conn=conn),
                "members": _invoke(
                    source, "get_war_champ_standings", season_id=season_id, conn=conn
                ),
                "rookie_mvps": _invoke(
                    source,
                    "get_rookie_mvp_candidates",
                    season_id=season_id,
                    limit=3,
                    conn=conn,
                ),
            }
        elif clean_metric == "win_rate":
            data = _invoke(
                source,
                "get_war_battle_win_rates",
                season_id=season_id,
                limit=limit,
                min_battles=4,
                conn=conn,
            )
        elif clean_metric == "attendance":
            data = _invoke(
                source,
                "get_members_without_war_participation",
                season_id=season_id,
                conn=conn,
            )
        else:
            data = {"error": f"Unknown metric: {metric}"}
    elif view == "win_rates":
        data = _invoke(
            source,
            "get_war_battle_win_rates",
            season_id=season_id,
            limit=limit,
            min_battles=4,
            conn=conn,
        )
    elif view == "boat_battles":
        data = _invoke(source, "get_clan_boat_battle_record", weeks=3, conn=conn)
    elif view == "score_trend":
        data = _invoke(source, "get_war_score_trend", days=30, conn=conn)
    elif view == "season_comparison":
        data = _invoke(
            source,
            "compare_fame_per_member_to_previous_season",
            season_id=season_id,
            conn=conn,
        )
    elif view == "trending":
        data = _invoke(
            source,
            "get_trending_war_contributors",
            season_id=season_id,
            recent_races=2,
            limit=limit,
            conn=conn,
        )
    elif view == "perfect_attendance":
        data = _invoke(
            source, "get_perfect_war_participants", season_id=season_id, conn=conn
        )
    elif view == "no_participation":
        data = _invoke(
            source,
            "get_members_without_war_participation",
            season_id=season_id,
            conn=conn,
        )
    else:
        data = {"error": f"Unknown view: {view}"}

    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": view,
        "season_id": season_id,
        "sources": ["war_seasons", "war_weeks", "war_participation"],
        "data_generation": (
            generation_snapshot(conn) if conn is not None else None
        ),
        "data": data,
    }


__all__ = [
    "CAPABILITY_ID",
    "CONTRACT_VERSION",
    "get_war_intelligence",
    "get_war_season_view",
]
