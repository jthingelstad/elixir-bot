"""War read layer — v5.1 sources (docs/v5.1/schema.md §9).

Live race state reads the engine's riverrace baseline
(state_baselines('riverrace'), the race-aspect projection); logged weeks read
war_weeks/war_week_clans; per-member day detail reads war_attendance_days +
war_participation. The war_current_state / war_participant_snapshots /
war_races tables are gone (schema.md §8).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import sqlite3

log = logging.getLogger("elixir_db")

from db import (
    _canon_tag,
    _parse_cr_time,
    _rowdicts,
    managed_connection,
)
from storage._enrichment import _member_reference_fields
from storage._war_shared import (
    get_latest_logged_race,
    infer_current_season_id_from_live_state,
)
from storage.war_calendar import (
    FINAL_BATTLE_PERIOD_OFFSET,
    FINAL_PRACTICE_PERIOD_OFFSET,
    FIRST_BATTLE_PERIOD_OFFSET,
    coerce_utc_datetime,
    format_utc_iso,
    is_colosseum_week,
    phase_day_number,
    period_offset,
    resolve_phase,
    war_day_key,
    war_reset_window_utc,
)

HOME_CLAN = "#J2RGCRVG"

# Game constants (architecture §16.4): finish lines per week type.
FAME_FINISH_LINE = 10_000
FAME_FINISH_LINE_COLOSSEUM = 5_000


def _coerce_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _live_race(conn) -> Optional[tuple[dict, str]]:
    """The engine's riverrace baseline: (race-aspect projection, observed_at)."""
    row = conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind = 'riverrace' AND aspect = 'race' "
        "ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"]), row["observed_at"]
    except (TypeError, ValueError):
        return None


def _race_standings_from_projection(projection: dict) -> list[dict]:
    clans = projection.get("clans") or {}
    our_tag = _canon_tag(projection.get("our_tag") or HOME_CLAN)
    ranked = sorted(
        clans.items(),
        key=lambda kv: (
            _coerce_int((kv[1] or {}).get("fame")),
            _coerce_int((kv[1] or {}).get("period_points")),
            _coerce_int((kv[1] or {}).get("clan_score")),
        ),
        reverse=True,
    )
    standings = []
    for rank, (tag, info) in enumerate(ranked, start=1):
        info = info or {}
        fame = _coerce_int(info.get("fame"))
        standings.append({
            "rank": rank,
            "clan_tag": tag,
            "clan_name": info.get("name"),
            "fame": fame,
            "repair_points": 0,
            "period_points": _coerce_int(info.get("period_points")),
            "clan_score": _coerce_int(info.get("clan_score")),
            "is_us": tag == our_tag,
            "active_score": fame,
            "score_source": "fame",
            "score_label": "fame",
        })
    return standings


def is_colosseum_week_confirmed(
    period_type: Optional[str],
    trophy_change: Optional[int] = None,
    *,
    trophy_stakes_known: bool = False,
) -> bool:
    """True when we have positive evidence the current week is the colosseum
    (final) week of the season (§16.1: detected, never forecast)."""
    if period_type == "colosseum":
        return True
    if trophy_stakes_known and abs(trophy_change or 0) == 100:
        return True
    return False


@managed_connection
def get_current_war_status(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    live = _live_race(conn)
    if not live:
        return None
    projection, observed_at = live
    latest_logged = get_latest_logged_race(conn)
    season_id = projection.get("season_id")
    if season_id is None:
        season_id = infer_current_season_id_from_live_state(projection, latest_logged)
    section_index = projection.get("section_index")
    period_index = projection.get("period_index")
    period_type = projection.get("period_type")
    phase = resolve_phase(period_type, period_index)
    offset = period_offset(period_index)
    colosseum = is_colosseum_week(period_type)
    our_fame = _coerce_int(projection.get("our_fame"))
    finish_line = FAME_FINISH_LINE_COLOSSEUM if colosseum else FAME_FINISH_LINE

    trophy_change = None
    if latest_logged and season_id == latest_logged["season_id"] and section_index == latest_logged["section_index"]:
        trophy_change = latest_logged["trophy_change"]

    standings = _race_standings_from_projection(projection)
    race_rank = next((s["rank"] for s in standings if s["is_us"]), None)

    observed_dt = coerce_utc_datetime(observed_at)
    _, period_ends_at = war_reset_window_utc(observed_dt or observed_at)

    result = {
        "observed_at": observed_at,
        "war_state": "training" if phase == "practice" else "inWar",
        "clan_tag": _canon_tag(projection.get("our_tag") or HOME_CLAN),
        "clan_name": next((s["clan_name"] for s in standings if s["is_us"]), None),
        "fame": our_fame,
        "repair_points": 0,
        "period_points": None,
        "clan_score": next((s["clan_score"] for s in standings if s["is_us"]), None),
        "active_score": our_fame,
        "score_source": "fame",
        "score_label": "fame",
        "season_id": season_id,
        "section_index": section_index,
        "week": (section_index + 1) if section_index is not None else None,
        "period_index": period_index,
        "period_offset": offset,
        "period_type": period_type,
        "phase": phase,
        "colosseum_week": colosseum,
        "battle_phase_active": phase == "battle",
        "practice_phase_active": phase == "practice",
        "final_practice_day_active": phase == "practice" and offset == FINAL_PRACTICE_PERIOD_OFFSET,
        "final_battle_day_active": phase == "battle" and offset == FINAL_BATTLE_PERIOD_OFFSET,
        "battle_day_number": phase_day_number(phase, period_index) if phase == "battle" else None,
        "battle_day_total": 4 if phase == "battle" else None,
        "practice_day_number": phase_day_number(phase, period_index) if phase == "practice" else None,
        "practice_day_total": FIRST_BATTLE_PERIOD_OFFSET if phase == "practice" else None,
        "race_rank": race_rank,
        "race_standings": standings,
        "war_day_key": war_day_key(season_id, section_index, period_index, observed_at),
        "finish_time": None,
        "race_completed": phase != "practice" and our_fame >= finish_line,
        "race_completed_at": None,
        "race_completed_early": False,
        "trophy_change": int(trophy_change) if isinstance(trophy_change, (int, float)) else None,
        "trophy_stakes_known": isinstance(trophy_change, (int, float)),
        "trophy_stakes_text": (
            f"{abs(int(trophy_change))} trophies on the line"
            if isinstance(trophy_change, (int, float)) and trophy_change else None
        ),
        "period_ends_at": format_utc_iso(period_ends_at) if period_ends_at else None,
    }
    result["phase_display"] = (
        f"Battle Day {result['battle_day_number']}"
        if result["battle_day_number"] is not None
        else f"Practice Day {result['practice_day_number']}"
        if result["practice_day_number"] is not None
        else (phase.title() if phase else None)
    )
    result["season_week_label"] = (
        f"Season {season_id} Week {result['week']}"
        if season_id is not None and result.get("week") is not None
        else None
    )
    return result


def _format_duration_short(total_seconds: Optional[int]) -> Optional[str]:
    if total_seconds is None:
        return None
    seconds = max(0, int(total_seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@managed_connection
def get_war_day_state(war_day_key_arg: Optional[str] = None, observed_at: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Current war-day engagement, from the live participants projection plus
    war_attendance_days (fame_delta per member for the day)."""
    del observed_at  # historical point-in-time reads retired with the snapshots
    status = get_current_war_status(conn=conn)
    if not status:
        return None
    if war_day_key_arg and status.get("war_day_key") != war_day_key_arg:
        return None  # only the current day is reconstructable live
    live = _live_race(conn)
    if not live:
        return None
    projection, observed_at_live = live
    participants_map = projection.get("participants") or {}

    season_id = status.get("season_id")
    section_index = status.get("section_index")
    day_rows = {}
    if season_id is not None and section_index is not None:
        for row in conn.execute(
            "SELECT player_tag, decks_used, decks_available, fame_delta "
            "FROM war_attendance_days WHERE season_id = ? AND section_index = ? "
            "AND war_day_index = (SELECT MAX(war_day_index) FROM war_attendance_days "
            "                     WHERE season_id = ? AND section_index = ?)",
            (season_id, section_index, season_id, section_index),
        ).fetchall():
            day_rows[row["player_tag"]] = dict(row)

    participants, used_all, used_some, used_none = [], [], [], []
    for tag, info in participants_map.items():
        info = info or {}
        day = day_rows.get(tag) or {}
        item = {
            "tag": tag,
            "name": info.get("name"),
            "fame": _coerce_int(info.get("fame")),
            "repair_points": _coerce_int(info.get("repair_points")),
            "boat_attacks": _coerce_int(info.get("boat_attacks")),
            "decks_used_total": _coerce_int(info.get("decks_used")),
            "decks_used_today": _coerce_int(info.get("decks_used_today")),
            "fame_today": _coerce_int(day.get("fame_delta")),
        }
        item = _member_reference_fields(conn, tag, item)
        participants.append(item)
        decks_today = item["decks_used_today"]
        if decks_today >= 4:
            used_all.append(item)
        elif decks_today > 0:
            used_some.append(item)
        else:
            used_none.append(item)

    top_fame_today = sorted(participants, key=lambda i: (-(i.get("fame_today") or 0), -(i.get("fame") or 0), (i.get("name") or "").lower()))
    top_fame_total = sorted(participants, key=lambda i: (-(i.get("fame") or 0), -(i.get("decks_used_total") or 0), (i.get("name") or "").lower()))

    observed_dt = coerce_utc_datetime(observed_at_live)
    started_at, ends_at = war_reset_window_utc(observed_dt or observed_at_live)
    now = datetime.now(timezone.utc)
    time_left_seconds = max(0, int((ends_at - now).total_seconds())) if ends_at else None

    phase = status.get("phase")
    day_number = status.get("battle_day_number") if phase == "battle" else status.get("practice_day_number")
    return {
        "war_day_key": status.get("war_day_key"),
        "season_id": season_id,
        "section_index": section_index,
        "week": status.get("week"),
        "period_index": status.get("period_index"),
        "period_type": status.get("period_type"),
        "phase": phase,
        "phase_display": status.get("phase_display"),
        "day_number": day_number,
        "day_total": status.get("battle_day_total") if phase == "battle" else status.get("practice_day_total"),
        "race_rank": status.get("race_rank"),
        "clan_fame": status.get("fame"),
        "clan_score": status.get("clan_score"),
        "period_points": status.get("period_points"),
        "finish_time": status.get("finish_time"),
        "race_completed": status.get("race_completed"),
        "race_completed_at": status.get("race_completed_at"),
        "race_completed_early": status.get("race_completed_early"),
        "trophy_change": status.get("trophy_change"),
        "trophy_stakes_known": status.get("trophy_stakes_known"),
        "trophy_stakes_text": status.get("trophy_stakes_text"),
        "observed_at": observed_at_live,
        "first_observed_at": observed_at_live,
        "last_observed_at": observed_at_live,
        "period_started_at": format_utc_iso(started_at) if started_at else None,
        "period_ends_at": format_utc_iso(ends_at) if ends_at else None,
        "time_left_seconds": time_left_seconds,
        "time_left_text": _format_duration_short(time_left_seconds),
        "total_participants": len(participants),
        "engaged_count": len(used_all) + len(used_some),
        "finished_count": len(used_all),
        "untouched_count": len(used_none),
        "used_all_4": used_all,
        "used_some": used_some,
        "used_none": used_none,
        "top_fame_today": top_fame_today[:5],
        "top_fame_total": top_fame_total[:5],
        "participants": participants,
    }


def get_current_war_day_state(conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    return get_war_day_state(None, conn=conn)


@managed_connection
def list_recent_war_day_summaries(limit: int = 7, phase: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Recent war-day engagement from war_attendance_days (finalized days)."""
    rows = conn.execute(
        "SELECT season_id, section_index, war_day_index, "
        "COUNT(*) AS participants, "
        "SUM(CASE WHEN decks_used >= 4 THEN 1 ELSE 0 END) AS finished_count, "
        "SUM(CASE WHEN decks_used > 0 THEN 1 ELSE 0 END) AS engaged_count, "
        "MAX(observed_at) AS observed_at "
        "FROM war_attendance_days "
        "GROUP BY season_id, section_index, war_day_index "
        "ORDER BY observed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    del phase  # attendance days are battle days by definition
    out = []
    for row in rows:
        out.append({
            "war_day_key": f"{row['season_id']}:{row['section_index']}:{row['war_day_index']}",
            "season_id": row["season_id"],
            "section_index": row["section_index"],
            "phase": "battle",
            "phase_display": f"Battle Day {row['war_day_index'] + 1}",
            "engaged_count": row["engaged_count"],
            "finished_count": row["finished_count"],
            "total_participants": row["participants"],
            "observed_at": row["observed_at"],
            "top_fame_today": [],
        })
    return out


@managed_connection
def get_latest_war_participant_snapshot_observed_at(war_day_key_arg: str = "", conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Freshness anchor: the riverrace baseline's observed_at (the snapshot
    tables are gone; the baseline is the live participants source)."""
    del war_day_key_arg
    live = _live_race(conn)
    return live[1] if live else None


@managed_connection
def get_latest_clan_boat_defense_status(clan_tag: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Boat-defense detail came from war_period_clan_status (dropped). The new
    race projection has no defense fields; return None so callers fall back
    gracefully (the war clock gates boat-defense talk anyway, §16.2)."""
    del clan_tag, conn
    return None


def get_war_deck_status_today(conn: Optional[sqlite3.Connection] = None) -> dict:
    state = get_current_war_day_state(conn=conn)
    if not state:
        return {
            "battle_date": None,
            "used_all_4": [],
            "used_some": [],
            "used_none": [],
            "total_participants": 0,
        }
    return {
        "battle_date": state.get("war_day_key"),
        "season_id": state.get("season_id"),
        "week": state.get("week"),
        "phase": state.get("phase"),
        "phase_display": state.get("phase_display"),
        "day_number": state.get("day_number"),
        "period_started_at": state.get("period_started_at"),
        "period_ends_at": state.get("period_ends_at"),
        "time_left_seconds": state.get("time_left_seconds"),
        "time_left_text": state.get("time_left_text"),
        "race_rank": state.get("race_rank"),
        "clan_fame": state.get("clan_fame"),
        "clan_score": state.get("clan_score"),
        "period_points": state.get("period_points"),
        "used_all_4": state.get("used_all_4") or [],
        "used_some": state.get("used_some") or [],
        "used_none": state.get("used_none") or [],
        "top_fame_today": state.get("top_fame_today") or [],
        "top_fame_total": state.get("top_fame_total") or [],
        "engaged_count": state.get("engaged_count") or 0,
        "finished_count": state.get("finished_count") or 0,
        "untouched_count": state.get("untouched_count") or 0,
        "total_participants": state.get("total_participants") or 0,
    }


def _format_war_now_text(data: dict) -> str:
    parts = [f"Season {data['season_id']} · Week {data['week']}"]
    phase_with_total = data.get("phase_display")
    day_number = data.get("day_number")
    day_total = data.get("day_total")
    if phase_with_total and day_total:
        phase_with_total = f"{phase_with_total} of {day_total}"
        if day_number is not None:
            after_today = max(0, day_total - day_number)
            phase_word = "battle" if data.get("phase") == "battle" else "practice"
            if after_today > 0:
                more = "day" if after_today == 1 else "days"
                phase_with_total += f" (today + {after_today} more {phase_word} {more})"
    if phase_with_total:
        parts.append(phase_with_total)
    if data.get("is_colosseum_week"):
        parts.append("Colosseum (final week, 100 trophy stakes)")
    if data.get("is_final_battle_day"):
        parts.append("Final battle day")
    elif data.get("is_final_practice_day"):
        parts.append("Final practice day")

    lines = ["=== RIVER RACE — CURRENT MOMENT ===", " · ".join(parts)]
    if data.get("time_left_text"):
        lines.append(f"Period ends in {data['time_left_text']}")

    standings = data.get("race_standings") or []
    if standings:
        lines.append("Race standings:")
        for clan in standings:
            marker = " (us)" if clan.get("is_us") else ""
            score = clan.get("active_score", clan.get("fame", 0))
            label = clan.get("score_label") or "fame"
            lines.append(
                f"  {clan['rank']}. {clan.get('clan_name', '?')}{marker} | "
                f"{score:,} {label}"
            )
    return "\n".join(lines)


def build_war_now_context(conn: Optional[sqlite3.Connection] = None) -> tuple[Optional[dict], str]:
    """Single source of truth for 'what moment is it in the war' for LLM
    consumption. Returns (data, text); (None, "") when there is no race
    baseline yet."""
    status = get_current_war_status(conn=conn) or {}
    if not status:
        return None, ""
    day_state = get_current_war_day_state(conn=conn) or {}

    period_type = status.get("period_type")
    phase = status.get("phase")
    if phase == "battle":
        day_number = status.get("battle_day_number")
        day_total = status.get("battle_day_total")
    else:
        day_number = status.get("practice_day_number")
        day_total = status.get("practice_day_total")

    time_left_seconds = day_state.get("time_left_seconds")
    colosseum_week = is_colosseum_week_confirmed(
        period_type,
        status.get("trophy_change"),
        trophy_stakes_known=bool(status.get("trophy_stakes_known")),
    )

    data = {
        "season_id": status.get("season_id"),
        "week": status.get("week"),
        "phase": phase,
        "phase_display": status.get("phase_display"),
        "day_number": day_number,
        "day_total": day_total,
        "period_type": period_type,
        "time_left_seconds": time_left_seconds,
        "time_left_text": _format_duration_short(time_left_seconds),
        "period_started_at": day_state.get("period_started_at"),
        "period_ends_at": day_state.get("period_ends_at"),
        "is_colosseum_week": bool(colosseum_week),
        "is_final_battle_day": bool(status.get("final_battle_day_active", False)),
        "is_final_practice_day": bool(status.get("final_practice_day_active", False)),
        "race_standings": status.get("race_standings") or [],
    }
    data["now_text"] = _format_war_now_text(data)
    data.pop("day_total", None)
    return data, data["now_text"]


@managed_connection
def get_war_week_summary(season_id: Optional[int] = None, section_index: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    current = get_current_war_status(conn=conn)
    if season_id is None:
        season_id = (current or {}).get("season_id")
    if section_index is None:
        section_index = (current or {}).get("section_index")
    if season_id is None or section_index is None:
        return None

    race = conn.execute(
        "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, finish_time "
        "FROM war_weeks WHERE season_id = ? AND section_index = ?",
        (season_id, section_index),
    ).fetchone()
    participant_rows = conn.execute(
        "SELECT wp.player_tag, p.current_name AS player_name, wp.fame, wp.repair_points, wp.boat_attacks, wp.decks_used "
        "FROM war_participation wp LEFT JOIN players p ON p.player_tag = wp.player_tag "
        "WHERE wp.season_id = ? AND wp.section_index = ? "
        "ORDER BY COALESCE(wp.fame, 0) DESC, COALESCE(wp.decks_used, 0) DESC, player_name COLLATE NOCASE",
        (season_id, section_index),
    ).fetchall()
    top_participants = []
    for row in participant_rows[:5]:
        item = {
            "tag": row["player_tag"],
            "name": row["player_name"],
            "fame": row["fame"] or 0,
            "repair_points": row["repair_points"] or 0,
            "boat_attacks": row["boat_attacks"] or 0,
            "decks_used": row["decks_used"] or 0,
        }
        top_participants.append(_member_reference_fields(conn, row["player_tag"], item))

    day_summaries = []
    for row in conn.execute(
        "SELECT war_day_index, COUNT(*) AS participants, "
        "SUM(CASE WHEN decks_used >= 4 THEN 1 ELSE 0 END) AS finished_count, "
        "SUM(CASE WHEN decks_used > 0 THEN 1 ELSE 0 END) AS engaged_count "
        "FROM war_attendance_days WHERE season_id = ? AND section_index = ? "
        "GROUP BY war_day_index ORDER BY war_day_index ASC",
        (season_id, section_index),
    ).fetchall():
        day_summaries.append({
            "war_day_key": f"{season_id}:{section_index}:{row['war_day_index']}",
            "phase": "battle",
            "phase_display": f"Battle Day {row['war_day_index'] + 1}",
            "engaged_count": row["engaged_count"],
            "finished_count": row["finished_count"],
            "top_fame_today": [],
        })

    return {
        "season_id": season_id,
        "section_index": section_index,
        "week": section_index + 1,
        "race": dict(race) if race else None,
        "participant_count": len(participant_rows),
        "top_participants": top_participants,
        "day_summaries": day_summaries,
    }


@managed_connection
def get_war_season_summary(season_id: Optional[int] = None, top_n: int = 5, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    from storage.war_analytics import (
        get_members_without_war_participation,
        get_war_champ_standings,
    )

    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None
    total_races = conn.execute(
        "SELECT COUNT(*) AS cnt, SUM(COALESCE(our_fame, 0)) AS total_clan_fame "
        "FROM war_weeks WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    top = get_war_champ_standings(season_id=season_id, conn=conn)[:top_n]
    nonparticipants = get_members_without_war_participation(season_id=season_id, conn=conn)["members"]
    active_members = conn.execute(
        "SELECT COUNT(*) AS cnt FROM clan_memberships WHERE left_at IS NULL"
    ).fetchone()["cnt"]
    return {
        "season_id": season_id,
        "races": total_races["cnt"],
        "total_clan_fame": total_races["total_clan_fame"] or 0,
        "fame_per_active_member": round((total_races["total_clan_fame"] or 0) / active_members, 2) if active_members else 0,
        "top_contributors": top,
        "nonparticipants": nonparticipants,
    }


@managed_connection
def get_trophy_drops(days: int = 7, min_drop: int = 100, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT m.player_tag AS tag, m.current_name AS name, "
        "MIN(dm.trophies) AS min_trophies, MAX(dm.trophies) AS max_trophies, "
        "MAX(dm.metric_date) AS latest_metric_date, "
        "(MAX(dm.trophies) - MIN(dm.trophies)) AS spread "
        "FROM player_daily_metrics dm "
        "JOIN players m ON m.player_tag = dm.player_tag "
        "WHERE dm.metric_date >= ? "
        "AND EXISTS (SELECT 1 FROM clan_memberships cm WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL) "
        "GROUP BY dm.player_tag "
        "HAVING spread >= ? "
        "ORDER BY spread DESC",
        (cutoff, min_drop),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["drop"] = item.pop("spread")
        result.append(item)
    return result


@managed_connection
def get_trophy_changes(since_hours: int = 24, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=since_hours)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT m.player_tag AS tag, m.current_name AS name, d.trophies, d.metric_date,
                ROW_NUMBER() OVER (PARTITION BY d.player_tag ORDER BY d.metric_date ASC) AS rn_asc,
                ROW_NUMBER() OVER (PARTITION BY d.player_tag ORDER BY d.metric_date DESC) AS rn_desc,
                d.player_tag
            FROM player_daily_metrics d
            JOIN players m ON m.player_tag = d.player_tag
            WHERE d.metric_date >= ?
        )
        SELECT a.tag, a.name,
               a.trophies AS old_trophies,
               b.trophies AS new_trophies,
               (b.trophies - a.trophies) AS change
        FROM ranked a
        JOIN ranked b ON a.player_tag = b.player_tag
        WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND a.trophies != b.trophies
        ORDER BY ABS(change) DESC
        """,
        (cutoff,),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_war_history(n: int = 10, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    rows = conn.execute(
        "SELECT (season_id * 100 + section_index) AS id, season_id, section_index, our_rank, our_fame, "
        "finish_time, created_date, NULL AS standings_json FROM war_weeks ORDER BY created_date DESC LIMIT ?",
        (n,),
    ).fetchall()
    return _rowdicts(rows)


def get_current_season_id(conn: Optional[sqlite3.Connection] = None) -> Optional[int]:
    current = get_current_war_status(conn=conn)
    return current.get("season_id") if current else None


@managed_connection
def count_war_races_for_season(season_id: int, conn: Optional[sqlite3.Connection] = None) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM war_weeks WHERE season_id = ?", (season_id,)
    ).fetchone()
    return int(row["cnt"]) if row and row["cnt"] is not None else 0


@managed_connection
def is_war_section_finalized(
    season_id: int, section_index: int, conn: Optional[sqlite3.Connection] = None
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM war_weeks WHERE season_id = ? AND section_index = ? "
        "AND (finish_time IS NOT NULL OR our_rank IS NOT NULL) LIMIT 1",
        (season_id, section_index),
    ).fetchone()
    return row is not None


@managed_connection
def get_latest_war_race_finish_time(
    season_id: int, conn: Optional[sqlite3.Connection] = None
) -> Optional[str]:
    """Most recent finalized week's finish_time (or created_date fallback)."""
    row = conn.execute(
        "SELECT finish_time, created_date FROM war_weeks "
        "WHERE season_id = ? ORDER BY section_index DESC LIMIT 1",
        (season_id,),
    ).fetchone()
    if not row:
        return None
    return row["finish_time"] or row["created_date"]


def _season_bounds(conn: sqlite3.Connection, season_id: int) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT MIN(created_date) AS start_date, MAX(created_date) AS end_date "
        "FROM war_weeks WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    if not row or not row["start_date"] or not row["end_date"]:
        return None, None
    start_dt = _parse_cr_time(row["start_date"])
    end_dt = _parse_cr_time(row["end_date"])
    if not start_dt or not end_dt:
        return None, None
    end_dt = end_dt + timedelta(days=7)
    return start_dt.strftime("%Y%m%dT%H%M%S.000Z"), end_dt.strftime("%Y%m%dT%H%M%S.000Z")


@managed_connection
def get_season_window(
    season_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Concrete River Race season frame: date bounds + week-by-week trajectory."""
    sid = season_id if season_id is not None else get_current_season_id(conn=conn)
    if sid is None:
        row = conn.execute("SELECT MAX(season_id) AS sid FROM war_seasons").fetchone()
        sid = row["sid"] if row else None
    if sid is None:
        return None
    start, end = _season_bounds(conn, int(sid))
    weeks = conn.execute(
        "SELECT ww.section_index, ww.our_rank, ww.our_fame, ww.trophy_change, "
        "(SELECT COUNT(*) FROM war_week_clans wwc "
        " WHERE wwc.season_id = ww.season_id AND wwc.section_index = ww.section_index) AS clans "
        "FROM war_weeks ww WHERE ww.season_id = ? ORDER BY ww.section_index ASC",
        (int(sid),),
    ).fetchall()
    trajectory = [
        {
            "section_index": row["section_index"],
            "rank": row["our_rank"],
            "fame": row["our_fame"],
            "trophy_change": row["trophy_change"],
            "clans": row["clans"] or None,
        }
        for row in weeks
    ]
    return {
        "season_id": int(sid),
        "start": start,
        "end": end,
        "weeks_recorded": len(trajectory),
        "week_trajectory": trajectory,
    }


def get_war_season_snapshot(conn: Optional[sqlite3.Connection] = None) -> dict | None:
    """Season-state snapshot for memory-synthesis, leadership reports, and
    get_elixir_state. Lean v5.1 rebuild of the retired storage.projects
    version: computed from the war clock + war tables on demand; the Gen C
    project machinery (active_risks, recent_communications,
    prior_cycle_comparison) is gone, so those keys return empty — callers
    render this dict generically. Returns None when no season is active."""
    current = get_current_war_status(conn=conn) or {}
    season_id = current.get("season_id")
    if season_id is None:
        return None

    @managed_connection
    def _build(*, conn: Optional[sqlite3.Connection] = None) -> dict:
        season = conn.execute(
            "SELECT started_at, weeks, final_rank FROM war_seasons WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        weeks = conn.execute(
            """SELECT section_index, our_rank, our_fame, trophy_change
               FROM war_weeks WHERE season_id = ? ORDER BY section_index""",
            (season_id,),
        ).fetchall()
        participation = conn.execute(
            """SELECT COUNT(DISTINCT player_tag) AS players,
                      COALESCE(SUM(fame), 0) AS fame,
                      COALESCE(SUM(decks_used), 0) AS decks_used
               FROM war_participation WHERE season_id = ?""",
            (season_id,),
        ).fetchone()
        phase = current.get("phase")
        summary_bits = [f"Season {season_id}"]
        if current.get("section_index") is not None:
            summary_bits.append(f"week {int(current['section_index']) + 1}")
        if phase:
            summary_bits.append(str(phase))
        if current.get("race_rank"):
            summary_bits.append(f"race rank {current['race_rank']}")
        return {
            "season_id": season_id,
            "summary": ", ".join(summary_bits),
            "started_at": season["started_at"] if season else None,
            "last_observed_at": current.get("observed_at"),
            "state": {
                "season_id": season_id,
                "week": current.get("section_index"),
                "phase": phase,
                "phase_display": current.get("phase_display") or phase,
                "day_number": current.get("day_number"),
                "race": {
                    "standings": current.get("standings") or [],
                    "our_fame": current.get("our_fame"),
                    "finish_line": current.get("finish_line"),
                },
                "participation_health": dict(participation) if participation else {},
                "season_summary": {
                    "weeks_recorded": len(weeks),
                    "week_trajectory": [dict(w) for w in weeks],
                },
                "active_risks": {},
                "recent_communications": [],
                "prior_cycle_comparison": {},
            },
        }

    return _build(conn=conn)
