from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional
import sqlite3

from cr_knowledge import TROPHY_MILESTONES
from db import (
    _canon_tag,
    _current_joined_at,
    _member_reference_fields,
    _parse_cr_time,
    _rowdicts,
    _utcnow,
    get_connection,
)

BATTLE_PERIOD_TYPE = "warDay"
FIRST_BATTLE_PERIOD_INDEX = 3
FINAL_BATTLE_PERIOD_INDEX = 6
FINAL_PRACTICE_PERIOD_INDEX = FIRST_BATTLE_PERIOD_INDEX - 1

def _format_member_reference(*args, **kwargs):
    from storage.identity import format_member_reference

    return format_member_reference(*args, **kwargs)


def _load_war_payload(raw_json) -> dict:
    if not raw_json:
        return {}
    if isinstance(raw_json, dict):
        return raw_json
    try:
        return json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return {}


def _get_latest_logged_race(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time "
        "FROM war_races ORDER BY season_id DESC, section_index DESC, war_race_id DESC LIMIT 1"
    ).fetchone()


def _infer_current_season_id_from_live_state(payload: dict, latest_logged_race) -> Optional[int]:
    live_season_id = payload.get("seasonId")
    if live_season_id is not None:
        return live_season_id
    if not latest_logged_race:
        return None
    live_section_index = payload.get("sectionIndex")
    logged_section_index = latest_logged_race["section_index"]
    if (
        live_section_index is not None
        and logged_section_index is not None
        and live_section_index < logged_section_index
    ):
        return latest_logged_race["season_id"] + 1
    return latest_logged_race["season_id"]


def _resolve_phase(period_type: Optional[str], period_index: Optional[int]) -> Optional[str]:
    if period_type == BATTLE_PERIOD_TYPE:
        return "battle"
    if period_type:
        return "practice"
    if period_index is None:
        return None
    if FIRST_BATTLE_PERIOD_INDEX <= period_index <= FINAL_BATTLE_PERIOD_INDEX:
        return "battle"
    return "practice"


def _phase_day_number(phase: Optional[str], period_index: Optional[int]) -> Optional[int]:
    if period_index is None or phase not in {"battle", "practice"}:
        return None
    if phase == "battle":
        return period_index - FIRST_BATTLE_PERIOD_INDEX + 1
    return period_index + 1


def _resolve_live_race_rank(payload: dict, clan_tag: Optional[str]) -> Optional[int]:
    clans = payload.get("clans") or []
    canon_clan_tag = _canon_tag(clan_tag) if clan_tag else None
    if not clans or not canon_clan_tag:
        return None
    ranked = sorted(
        clans,
        key=lambda clan: (
            clan.get("fame") or 0,
            clan.get("repairPoints") or 0,
            clan.get("periodPoints") or 0,
            clan.get("clanScore") or 0,
        ),
        reverse=True,
    )
    for index, clan in enumerate(ranked, start=1):
        if _canon_tag(clan.get("tag")) == canon_clan_tag:
            return index
    return None


def _build_live_war_state(row, latest_logged_race) -> Optional[dict]:
    if not row:
        return None
    payload = _load_war_payload(row["raw_json"])
    result = dict(row)
    result.pop("raw_json", None)

    season_id = _infer_current_season_id_from_live_state(payload, latest_logged_race)
    section_index = payload.get("sectionIndex")
    period_index = payload.get("periodIndex")
    period_type = payload.get("periodType")
    phase = _resolve_phase(period_type, period_index)

    if season_id is not None:
        result["season_id"] = season_id
    if section_index is not None:
        result["section_index"] = section_index
        result["week"] = section_index + 1
    elif latest_logged_race and season_id == latest_logged_race["season_id"]:
        result["section_index"] = latest_logged_race["section_index"]
        result["week"] = (
            latest_logged_race["section_index"] + 1
            if latest_logged_race["section_index"] is not None
            else None
        )
        result["trophy_change"] = latest_logged_race["trophy_change"]

    result["period_index"] = period_index
    result["period_type"] = period_type
    result["phase"] = phase
    result["battle_phase_active"] = phase == "battle"
    result["practice_phase_active"] = phase == "practice"
    result["final_practice_day_active"] = (
        phase == "practice" and period_index == FINAL_PRACTICE_PERIOD_INDEX
    )
    result["final_battle_day_active"] = phase == "battle" and period_index == FINAL_BATTLE_PERIOD_INDEX
    result["battle_day_number"] = _phase_day_number(phase, period_index) if phase == "battle" else None
    result["battle_day_total"] = 4 if phase == "battle" else None
    result["practice_day_number"] = _phase_day_number(phase, period_index) if phase == "practice" else None
    result["practice_day_total"] = FIRST_BATTLE_PERIOD_INDEX if phase == "practice" else None
    result["phase_display"] = (
        f"Battle Day {result['battle_day_number']}"
        if result["battle_day_number"] is not None
        else f"Practice Day {result['practice_day_number']}"
        if result["practice_day_number"] is not None
        else phase.title() if phase else None
    )
    result["season_week_label"] = (
        f"Season {season_id} Week {result['week']}"
        if season_id is not None and result.get("week") is not None
        else None
    )
    result["race_rank"] = _resolve_live_race_rank(payload, result.get("clan_tag")) or result.get("race_rank")
    result["period_logs_count"] = len(payload.get("periodLogs") or [])
    return result


def get_recent_live_war_states(limit=2, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        latest_logged_race = _get_latest_logged_race(conn)
        rows = conn.execute(
            "SELECT war_id, observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json "
            "FROM war_current_state ORDER BY war_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            state for state in (
                _build_live_war_state(row, latest_logged_race)
                for row in rows
            )
            if state
        ]
    finally:
        if close:
            conn.close()

def get_current_war_status(conn=None):
    states = get_recent_live_war_states(limit=1, conn=conn)
    return states[0] if states else None

def get_war_deck_status_today(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        today = _utcnow()[:10]
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, w.decks_used_today, w.decks_used_total, w.fame "
            "FROM war_day_status w JOIN members m ON m.member_id = w.member_id "
            "WHERE w.battle_date = ? AND m.status = 'active' "
            "ORDER BY COALESCE(w.decks_used_today, 0) DESC, m.current_name COLLATE NOCASE",
            (today,),
        ).fetchall()
        used_all = []
        used_some = []
        used_none = []
        for row in rows:
            item = dict(row)
            decks_today = item.get("decks_used_today") or 0
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(item["tag"]),),
            ).fetchone()["member_id"]
            item = _member_reference_fields(conn, member_id, item)
            if decks_today >= 4:
                used_all.append(item)
            elif decks_today > 0:
                used_some.append(item)
            else:
                used_none.append(item)
        return {
            "battle_date": today,
            "used_all_4": used_all,
            "used_some": used_some,
            "used_none": used_none,
            "total_participants": len(rows),
        }
    finally:
        if close:
            conn.close()

def get_war_season_summary(season_id=None, top_n=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
            "FROM war_races WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        top = get_war_champ_standings(season_id=season_id, conn=conn)[:top_n]
        nonparticipants = get_members_without_war_participation(season_id=season_id, conn=conn)["members"]
        active_members = conn.execute(
            "SELECT COUNT(*) AS cnt FROM members WHERE status = 'active'"
        ).fetchone()["cnt"]
        return {
            "season_id": season_id,
            "races": total_races["cnt"],
            "total_clan_fame": total_races["total_clan_fame"] or 0,
            "fame_per_active_member": round((total_races["total_clan_fame"] or 0) / active_members, 2) if active_members else 0,
            "top_contributors": top,
            "nonparticipants": nonparticipants,
        }
    finally:
        if close:
            conn.close()

def get_trophy_drops(days=7, min_drop=100, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, "
            "MIN(dm.trophies) AS min_trophies, MAX(dm.trophies) AS max_trophies, "
            "MAX(dm.metric_date) AS latest_metric_date, "
            "(MAX(dm.trophies) - MIN(dm.trophies)) AS spread "
            "FROM member_daily_metrics dm "
            "JOIN members m ON m.member_id = dm.member_id "
            "WHERE dm.metric_date >= ? AND m.status = 'active' "
            "GROUP BY dm.member_id "
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
    finally:
        if close:
            conn.close()

def get_trophy_changes(since_hours=24, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT m.player_tag AS tag, s.name, s.trophies, s.observed_at,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at ASC) AS rn_asc,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn_desc,
                    s.member_id
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
                WHERE s.observed_at >= ?
            )
            SELECT a.tag, a.name,
                   a.trophies AS old_trophies,
                   b.trophies AS new_trophies,
                   (b.trophies - a.trophies) AS change
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND a.trophies != b.trophies
            ORDER BY ABS(change) DESC
            """,
            (cutoff,),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()

def detect_milestones(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT s.*, m.player_tag AS tag,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
            )
            SELECT a.tag, a.name,
                   b.trophies AS old_trophies, a.trophies AS new_trophies,
                   b.arena_name AS old_arena, a.arena_name AS new_arena
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn = 1 AND b.rn = 2
            """
        ).fetchall()
        milestones = []
        for row in rows:
            old_t = row["old_trophies"] or 0
            new_t = row["new_trophies"] or 0
            for threshold in TROPHY_MILESTONES:
                if old_t < threshold <= new_t:
                    milestones.append({
                        "tag": row["tag"],
                        "name": row["name"],
                        "type": "trophy_milestone",
                        "old_value": old_t,
                        "new_value": new_t,
                        "milestone": threshold,
                    })
            if row["old_arena"] and row["new_arena"] and row["old_arena"] != row["new_arena"]:
                milestones.append({
                    "tag": row["tag"],
                    "name": row["name"],
                    "type": "arena_change",
                    "old_value": row["old_arena"],
                    "new_value": row["new_arena"],
                })
        return milestones
    finally:
        if close:
            conn.close()

def detect_role_changes(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT s.*, m.player_tag AS tag,
                    ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
            )
            SELECT a.tag, a.name, b.role AS old_role, a.role AS new_role
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn = 1 AND b.rn = 2 AND COALESCE(a.role, '') != COALESCE(b.role, '')
            """
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()

def get_war_history(n=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT war_race_id AS id, season_id, section_index, our_rank, our_fame, finish_time, created_date, raw_json AS standings_json FROM war_races ORDER BY created_date DESC LIMIT ?",
            (n,),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()

def get_current_season_id(conn=None):
    current = get_current_war_status(conn=conn)
    return current.get("season_id") if current else None

def _season_bounds(conn: sqlite3.Connection, season_id: int) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT MIN(created_date) AS start_date, MAX(created_date) AS end_date "
        "FROM war_races WHERE season_id = ?",
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
