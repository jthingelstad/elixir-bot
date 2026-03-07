from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import (
    _canon_tag,
    _member_reference_fields,
    _parse_cr_time,
    _utcnow,
    get_connection,
)
from storage.war_status import _season_bounds, get_current_season_id

def _format_member_reference(*args, **kwargs):
    from storage.identity import format_member_reference

    return format_member_reference(*args, **kwargs)

def get_member_war_status(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        current_day = None
        today = _utcnow()[:10]
        current_day_row = conn.execute(
            "SELECT w.battle_date, w.decks_used_today, w.decks_used_total, w.fame, w.repair_points "
            "FROM war_day_status w JOIN members m ON m.member_id = w.member_id "
            "WHERE m.player_tag = ? AND w.battle_date = ?",
            (canon_tag, today),
        ).fetchone()
        if current_day_row:
            current_day = dict(current_day_row)
            current_day["decks_left_today"] = max(0, 4 - (current_day["decks_used_today"] or 0))

        summary = {
            "season_id": season_id,
            "member_ref": _format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "current_day": current_day,
            "season": None,
        }
        if season_id is not None:
            season_row = conn.execute(
                "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
                "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used, AVG(COALESCE(wp.fame, 0)) AS avg_fame "
                "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.player_tag = ?",
                (season_id, canon_tag),
            ).fetchone()
            total_races = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?",
                (season_id,),
            ).fetchone()["cnt"]
            season = dict(season_row)
            season["total_races_in_season"] = total_races
            season["participation_rate"] = round((season["races_played"] or 0) / total_races, 4) if total_races else 0
            summary["season"] = season
        return summary
    finally:
        if close:
            conn.close()

def get_member_war_stats(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT wp.participation_id AS id, wp.player_tag AS tag, wp.player_name AS name, wp.fame, wp.repair_points, wp.decks_used, wr.season_id, wr.section_index, wr.our_rank, wr.created_date FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id WHERE wp.player_tag = ? ORDER BY wr.created_date DESC",
            (_canon_tag(tag),),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(tag),),
            ).fetchone()
            if member_id:
                item = _member_reference_fields(conn, member_id["member_id"], item)
            result.append(item)
        return result
    finally:
        if close:
            conn.close()

def get_member_war_attendance(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        member = conn.execute(
            "SELECT member_id, current_name FROM members WHERE player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member:
            return None
        total_races = 0
        season_row = None
        if season_id is not None:
            total_races = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?",
                (season_id,),
            ).fetchone()["cnt"]
            season_row = conn.execute(
                "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
                "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used "
                "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                (season_id, member["member_id"]),
            ).fetchone()

        four_week_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=28)).strftime("%Y%m%dT%H%M%S.000Z")
        recent_total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM war_races WHERE created_date >= ?",
            (four_week_cutoff,),
        ).fetchone()["cnt"]
        recent_played = conn.execute(
            "SELECT COUNT(*) AS cnt "
            "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "WHERE wr.created_date >= ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
            (four_week_cutoff, member["member_id"]),
        ).fetchone()["cnt"]
        return {
            "season_id": season_id,
            "tag": canon_tag,
            "name": member["current_name"],
            "member_ref": _format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "season": {
                "races_played": season_row["races_played"] if season_row else 0,
                "total_races": total_races,
                "participation_rate": round((season_row["races_played"] or 0) / total_races, 4) if season_row and total_races else 0,
                "total_fame": season_row["total_fame"] if season_row else 0,
                "total_decks_used": season_row["total_decks_used"] if season_row else 0,
                "races_missed": max(0, total_races - (season_row["races_played"] or 0)) if season_row else total_races,
            },
            "last_4_weeks": {
                "races_played": recent_played or 0,
                "total_races": recent_total or 0,
                "participation_rate": round((recent_played or 0) / recent_total, 4) if recent_total else 0,
            },
        }
    finally:
        if close:
            conn.close()

def get_member_war_battle_record(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        member = conn.execute(
            "SELECT member_id, current_name FROM members WHERE player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member:
            return None
        start_bound, end_bound = _season_bounds(conn, season_id) if season_id is not None else (None, None)
        where = ["member_id = ?", "is_war = 1"]
        params = [member["member_id"]]
        if start_bound and end_bound:
            where.extend(["battle_time >= ?", "battle_time < ?"])
            params.extend([start_bound, end_bound])
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
            "SUM(CASE WHEN outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
            "COUNT(*) AS battles "
            f"FROM member_battle_facts WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        draws = row["draws"] or 0
        battles = row["battles"] or 0
        return {
            "season_id": season_id,
            "tag": canon_tag,
            "name": member["current_name"],
            "member_ref": _format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "battles": battles,
            "win_rate": round(wins / battles, 4) if battles else 0,
        }
    finally:
        if close:
            conn.close()

def get_member_missed_war_days(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        canon_tag = _canon_tag(tag)
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        member = conn.execute(
            "SELECT member_id, current_name FROM members WHERE player_tag = ?",
            (canon_tag,),
        ).fetchone()
        if not member or season_id is None:
            return None
        start_bound, end_bound = _season_bounds(conn, season_id)
        if not start_bound or not end_bound:
            return None
        start_dt = _parse_cr_time(start_bound)
        end_dt = _parse_cr_time(end_bound)
        tracked_days = conn.execute(
            "SELECT DISTINCT battle_date FROM war_day_status WHERE battle_date >= ? AND battle_date < ? ORDER BY battle_date",
            (start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")),
        ).fetchall()
        missed = []
        participated = 0
        for row in tracked_days:
            status = conn.execute(
                "SELECT decks_used_today FROM war_day_status WHERE member_id = ? AND battle_date = ?",
                (member["member_id"], row["battle_date"]),
            ).fetchone()
            if status and (status["decks_used_today"] or 0) > 0:
                participated += 1
            else:
                missed.append(row["battle_date"])
        return {
            "season_id": season_id,
            "tag": canon_tag,
            "name": member["current_name"],
            "member_ref": _format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "tracked_days": len(tracked_days),
            "days_participated": participated,
            "days_missed": len(missed),
            "missed_dates": missed,
        }
    finally:
        if close:
            conn.close()
