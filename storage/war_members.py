from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import (
    _canon_tag,
    managed_connection,
)
from storage._enrichment import _member_reference_fields, _membership_status_for
from storage._formatting import format_member_reference as _format_member_reference
from storage.war_status import (
    _season_bounds,
    get_current_season_id,
    get_current_war_status,
)


@managed_connection
def member_roster_status(tag, conn=None) -> dict:
    """Roster status for one member: {roster_status: active|departed|unknown,
    left_at}. QA H6/M1/M20/L18 — lets read paths flag a departed member rather
    than reporting them as an active roster player / war no-show."""
    return _membership_status_for(conn, _canon_tag(tag))


# v5.1 sources: war_participation is keyed (season_id, section_index,
# player_tag); the per-day source is war_attendance_days; war battle records
# read battle_events war keys (schema.md §9).


@managed_connection
def get_member_war_status(tag, season_id=None, conn=None):
    canon_tag = _canon_tag(tag)
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    current_day = None
    current_war = get_current_war_status(conn=conn)
    if (
        current_war
        and season_id is not None
        and current_war.get("section_index") is not None
    ):
        day_row = conn.execute(
            "SELECT war_day_index, decks_used, decks_available, fame_delta "
            "FROM war_attendance_days "
            "WHERE season_id = ? AND section_index = ? AND player_tag = ? "
            "ORDER BY war_day_index DESC LIMIT 1",
            (season_id, current_war.get("section_index"), canon_tag),
        ).fetchone()
        totals = conn.execute(
            "SELECT fame, repair_points, decks_used, decks_used_today FROM war_participation "
            "WHERE season_id = ? AND section_index = ? AND player_tag = ?",
            (season_id, current_war.get("section_index"), canon_tag),
        ).fetchone()
        if day_row or totals:
            decks_today = (
                (totals["decks_used_today"] if totals else None)
                if totals and totals["decks_used_today"] is not None
                else (day_row["decks_used"] if day_row else 0)
            ) or 0
            current_day = {
                "battle_date": current_war.get("war_day_key"),
                "decks_used_today": decks_today,
                "decks_used_total": (totals["decks_used"] if totals else None) or 0,
                "points": (totals["fame"] if totals else None) or 0,
                "repair_points": (totals["repair_points"] if totals else None) or 0,
                "decks_left_today": max(0, 4 - decks_today),
            }

    summary = {
        "season_id": season_id,
        "player_tag": canon_tag,
        "member_ref": _format_member_reference(canon_tag, conn=conn),
        "current_day": current_day,
        "season": None,
    }
    if season_id is not None:
        season_row = conn.execute(
            "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_points, "
            "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used, AVG(COALESCE(wp.fame, 0)) AS avg_points "
            "FROM war_participation wp "
            "WHERE wp.season_id = ? AND wp.player_tag = ?",
            (season_id, canon_tag),
        ).fetchone()
        total_races = conn.execute(
            "SELECT COUNT(*) AS cnt FROM war_weeks WHERE season_id = ?",
            (season_id,),
        ).fetchone()["cnt"]
        season = dict(season_row)
        season["total_races_in_season"] = total_races
        season["participation_rate"] = (
            round((season["races_played"] or 0) / total_races, 4) if total_races else 0
        )
        summary["season"] = season
    return summary


@managed_connection
def get_member_war_stats(tag, conn=None):
    canon = _canon_tag(tag)
    rows = conn.execute(
        "SELECT (wp.season_id * 100 + wp.section_index) AS id, wp.player_tag AS tag, "
        "COALESCE(p.display_name, p.current_name) AS name, wp.fame AS points, wp.repair_points, wp.decks_used, "
        "wp.season_id, wp.section_index, ww.our_rank, ww.created_date "
        "FROM war_participation wp "
        "LEFT JOIN war_weeks ww ON ww.season_id = wp.season_id AND ww.section_index = wp.section_index "
        "LEFT JOIN players p ON p.player_tag = wp.player_tag "
        "WHERE wp.player_tag = ? ORDER BY wp.season_id DESC, wp.section_index DESC",
        (canon,),
    ).fetchall()
    out = []
    for row in rows:
        d = _member_reference_fields(conn, canon, dict(row))
        # One row per RACE WEEK — "fame 0" here means this week, not the
        # season (battery case 53: the model read the top row as a season).
        d["scope"] = "single_race_week"
        out.append(d)
    return out


@managed_connection
def get_member_war_attendance(tag, season_id=None, conn=None):
    canon_tag = _canon_tag(tag)
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    member = conn.execute(
        "SELECT player_tag, COALESCE(display_name, current_name) AS name FROM players WHERE player_tag = ?",
        (canon_tag,),
    ).fetchone()
    if not member:
        return None
    total_races = 0
    season_row = None
    if season_id is not None:
        total_races = conn.execute(
            "SELECT COUNT(*) AS cnt FROM war_weeks WHERE season_id = ?",
            (season_id,),
        ).fetchone()["cnt"]
        season_row = conn.execute(
            "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_points, "
            "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used "
            "FROM war_participation wp "
            "WHERE wp.season_id = ? AND wp.player_tag = ? AND COALESCE(wp.decks_used, 0) > 0",
            (season_id, canon_tag),
        ).fetchone()

    # QA H5: war_weeks.created_date is stored in two formats — compact
    # '20260629T093706.000Z' and ISO '2026-07-06'. A raw string compare against a
    # compact cutoff drops the ISO-format rows (the two NEWEST weeks) because '-'
    # sorts below digits, understating recent attendance and feeding kick reads.
    # Normalise both sides to a bare YYYYMMDD date prefix before comparing.
    four_week_cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=28)
    ).strftime("%Y%m%d")
    recent_total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM war_weeks WHERE substr(REPLACE(created_date, '-', ''), 1, 8) >= ?",
        (four_week_cutoff,),
    ).fetchone()["cnt"]
    recent_played = conn.execute(
        "SELECT COUNT(*) AS cnt "
        "FROM war_participation wp "
        "JOIN war_weeks ww ON ww.season_id = wp.season_id AND ww.section_index = wp.section_index "
        "WHERE substr(REPLACE(ww.created_date, '-', ''), 1, 8) >= ? AND wp.player_tag = ? AND COALESCE(wp.decks_used, 0) > 0",
        (four_week_cutoff, canon_tag),
    ).fetchone()["cnt"]
    return {
        "season_id": season_id,
        "tag": canon_tag,
        "name": member["name"],
        "member_ref": _format_member_reference(canon_tag, conn=conn),
        "season": {
            "races_played": season_row["races_played"] if season_row else 0,
            "total_races": total_races,
            "participation_rate": round(
                (season_row["races_played"] or 0) / total_races, 4
            )
            if season_row and total_races
            else 0,
            "total_points": season_row["total_points"] if season_row else 0,
            "total_decks_used": season_row["total_decks_used"] if season_row else 0,
            "races_missed": max(0, total_races - (season_row["races_played"] or 0))
            if season_row
            else total_races,
        },
        "last_4_weeks": {
            "races_played": recent_played or 0,
            "total_races": recent_total or 0,
            "participation_rate": round((recent_played or 0) / recent_total, 4)
            if recent_total
            else 0,
        },
    }


@managed_connection
def get_member_war_battle_record(tag, season_id=None, conn=None):
    canon_tag = _canon_tag(tag)
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    member = conn.execute(
        "SELECT player_tag, COALESCE(display_name, current_name) AS name FROM players WHERE player_tag = ?",
        (canon_tag,),
    ).fetchone()
    if not member:
        return None
    where = ["player_tag = ?", "is_war = 1"]
    params: list = [canon_tag]
    if season_id is not None:
        # battle_events carry war keys directly (schema.md §5.1) — prefer them;
        # fall back to season date bounds for battles that predate a key stamp.
        where.append("(season_id = ? OR season_id IS NULL)")
        params.append(season_id)
        bounds = _season_bounds(conn, season_id)
        if bounds[0] and bounds[1]:
            where.extend(["battle_time >= ?", "battle_time < ?"])
            params.extend(list(bounds))
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
        "SUM(CASE WHEN outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
        "COUNT(*) AS battles, "
        # QA M4: war_day_index NULL = a training/practice-day war game, not a
        # counted battle-day attack — split them so this reconciles with
        # missed_days (which only counts battle days).
        "SUM(CASE WHEN war_day_index IS NOT NULL THEN 1 ELSE 0 END) AS battle_day_battles, "
        "COUNT(DISTINCT section_index) AS weeks_covered, "
        "MIN(battle_time) AS first_battle_time, MAX(battle_time) AS last_battle_time "
        f"FROM battle_events WHERE {' AND '.join(where)}",
        tuple(params),
    ).fetchone()
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    draws = row["draws"] or 0
    battles = row["battles"] or 0
    battle_day = row["battle_day_battles"] or 0
    return {
        "season_id": season_id,
        "tag": canon_tag,
        "name": member["name"],
        "member_ref": _format_member_reference(canon_tag, conn=conn),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "battles": battles,
        "battle_day_battles": battle_day,
        "training_or_practice_battles": battles - battle_day,
        "win_rate": round(wins / battles, 4) if battles else 0,
        # QA M5: this is a WHOLE-SEASON total, not a single week.
        "weeks_covered": row["weeks_covered"] or 0,
        "first_battle_time": row["first_battle_time"],
        "last_battle_time": row["last_battle_time"],
        "scope_note": "whole-season war battles (all weeks); battle_day_battles are the counted battle-day attacks that reconcile with attendance/missed_days.",
    }


@managed_connection
def get_member_missed_war_days(tag, season_id=None, conn=None):
    canon_tag = _canon_tag(tag)
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    member = conn.execute(
        "SELECT player_tag, COALESCE(display_name, current_name) AS name FROM players WHERE player_tag = ?",
        (canon_tag,),
    ).fetchone()
    if not member or season_id is None:
        return None
    tracked_days = conn.execute(
        "SELECT DISTINCT section_index, war_day_index FROM war_attendance_days "
        "WHERE season_id = ? ORDER BY section_index ASC, war_day_index ASC",
        (season_id,),
    ).fetchall()
    missed = []
    participated = 0
    for row in tracked_days:
        status = conn.execute(
            "SELECT decks_used FROM war_attendance_days "
            "WHERE season_id = ? AND section_index = ? AND war_day_index = ? AND player_tag = ?",
            (season_id, row["section_index"], row["war_day_index"], canon_tag),
        ).fetchone()
        if status and (status["decks_used"] or 0) > 0:
            participated += 1
        else:
            missed.append(
                f"Week {row['section_index'] + 1} Battle Day {row['war_day_index'] + 1}"
            )
    return {
        "season_id": season_id,
        "tag": canon_tag,
        "name": member["name"],
        "member_ref": _format_member_reference(canon_tag, conn=conn),
        "tracked_days": len(tracked_days),
        "days_participated": participated,
        "days_missed": len(missed),
        "missed_dates": missed,
    }


__all__ = [
    "get_member_missed_war_days",
    "get_member_war_attendance",
    "get_member_war_battle_record",
    "get_member_war_stats",
    "get_member_war_status",
    "member_roster_status",
]
