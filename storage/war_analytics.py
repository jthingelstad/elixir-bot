from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import (
    _canon_tag,
    _current_joined_at,
    _member_reference_fields,
    _parse_cr_time,
    _utcnow,
    managed_connection,
)
from storage.war_status import _season_bounds, get_current_season_id

from storage._formatting import format_member_reference as _format_member_reference


def _classify_war_player_rate(total: int, played: int) -> str:
    if total == 0:
        return "never"
    rate = played / total
    if rate >= 0.75:
        return "regular"
    if rate >= 0.25:
        return "occasional"
    if played > 0:
        return "rare"
    return "never"


def _war_player_type(conn, member_id):
    """Classify a member's war participation habit based on tracked race history.

    Returns one of: "regular" (75%+), "occasional" (25-74%),
    "rare" (1-24%), "never" (0%).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total_races, "
        "SUM(CASE WHEN COALESCE(wp.decks_used, 0) > 0 THEN 1 ELSE 0 END) AS races_played "
        "FROM war_participation wp "
        "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "WHERE wp.member_id = ?",
        (member_id,),
    ).fetchone()
    return _classify_war_player_rate(row["total_races"] or 0, row["races_played"] or 0)


def war_player_types_by_tag(conn, player_tags: list[str]) -> dict[str, str]:
    """Batch-classify war player types for a list of player tags.

    Returns {canonicalised_tag: "regular"|"occasional"|"rare"|"never"}.
    Tags the bot doesn't recognise are omitted from the result.
    """
    if not player_tags:
        return {}
    canon_tags = [tag if tag.startswith("#") else f"#{tag}" for tag in player_tags if tag]
    if not canon_tags:
        return {}
    placeholders = ",".join("?" * len(canon_tags))
    rows = conn.execute(
        f"SELECT m.player_tag, "
        f"COUNT(wr.war_race_id) AS total_races, "
        f"SUM(CASE WHEN COALESCE(wp.decks_used, 0) > 0 THEN 1 ELSE 0 END) AS races_played "
        f"FROM members m "
        f"LEFT JOIN war_participation wp ON wp.member_id = m.member_id "
        f"LEFT JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        f"WHERE m.player_tag IN ({placeholders}) "
        f"GROUP BY m.player_tag",
        canon_tags,
    ).fetchall()
    return {
        row["player_tag"]: _classify_war_player_rate(row["total_races"] or 0, row["races_played"] or 0)
        for row in rows
    }


def _get_account_age_years(conn, member_id):
    """Fetch CR account age in years from member_metadata, or None."""
    row = conn.execute(
        "SELECT cr_account_age_years FROM member_metadata WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    return row["cr_account_age_years"] if row else None


def _war_trend_anchor(conn):
    latest_state_row = conn.execute(
        "SELECT MAX(observed_at) AS observed_at FROM war_current_state"
    ).fetchone()
    latest_race_row = conn.execute(
        "SELECT MAX(created_date) AS created_date FROM war_races"
    ).fetchone()

    anchors = []
    observed_at = latest_state_row["observed_at"] if latest_state_row else None
    created_date = latest_race_row["created_date"] if latest_race_row else None

    if observed_at:
        try:
            anchors.append(datetime.fromisoformat(observed_at))
        except ValueError:
            pass
    if created_date:
        parsed = _parse_cr_time(created_date)
        if parsed:
            anchors.append(parsed)

    return max(anchors) if anchors else datetime.now(timezone.utc).replace(tzinfo=None)


def _member_activity_anchor(conn):
    latest_seen_row = conn.execute(
        "SELECT MAX(last_seen_api) AS last_seen_api FROM member_current_state"
    ).fetchone()
    last_seen_api = latest_seen_row["last_seen_api"] if latest_seen_row else None
    parsed_last_seen = _parse_cr_time(last_seen_api)
    return parsed_last_seen or datetime.now(timezone.utc).replace(tzinfo=None)


def _member_snapshot_anchor(conn):
    latest_snapshot_row = conn.execute(
        "SELECT MAX(observed_at) AS observed_at FROM member_state_snapshots"
    ).fetchone()
    observed_at = latest_snapshot_row["observed_at"] if latest_snapshot_row else None
    if observed_at:
        try:
            return datetime.fromisoformat(observed_at)
        except ValueError:
            pass
    return datetime.now(timezone.utc).replace(tzinfo=None)

@managed_connection
def get_members_without_war_participation(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return {"season_id": None, "members": []}
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.clan_rank "
        "FROM members m "
        "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
        "WHERE m.status = 'active' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM war_participation wp "
        "  JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "  WHERE wr.season_id = ? AND wp.member_id = m.member_id AND COALESCE(wp.decks_used, 0) > 0"
        ") "
        "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
        (season_id,),
    ).fetchall()
    members = []
    for row in rows:
        item = dict(row)
        item["joined_date"] = _current_joined_at(conn, row["member_id"])
        members.append(_member_reference_fields(conn, row["member_id"], item))
    return {"season_id": season_id, "members": members}

@managed_connection
def compare_member_war_to_clan_average(tag: str, season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    canon_tag = _canon_tag(tag)
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None
    member = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name "
        "FROM members m WHERE m.player_tag = ?",
        (canon_tag,),
    ).fetchone()
    if not member:
        return None
    total_races = conn.execute(
        "SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?",
        (season_id,),
    ).fetchone()["cnt"]
    active_members = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE status = 'active'"
    ).fetchone()["cnt"]
    member_stats = conn.execute(
        "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame, "
        "SUM(COALESCE(wp.decks_used, 0)) AS total_decks_used, AVG(COALESCE(wp.fame, 0)) AS avg_fame_per_race "
        "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "WHERE wr.season_id = ? AND wp.player_tag = ?",
        (season_id, canon_tag),
    ).fetchone()
    clan_avgs = conn.execute(
        "SELECT AVG(member_total_fame) AS avg_total_fame, AVG(member_races_played) AS avg_races_played, "
        "AVG(member_avg_fame) AS avg_fame_per_participant, AVG(member_total_decks) AS avg_total_decks "
        "FROM ("
        "  SELECT wp.player_tag, SUM(COALESCE(wp.fame, 0)) AS member_total_fame, "
        "         COUNT(*) AS member_races_played, AVG(COALESCE(wp.fame, 0)) AS member_avg_fame, "
        "         SUM(COALESCE(wp.decks_used, 0)) AS member_total_decks "
        "  FROM war_participation wp "
        "  JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "  JOIN members m ON m.member_id = wp.member_id "
        "  WHERE wr.season_id = ? AND m.status = 'active' "
        "  GROUP BY wp.player_tag"
        ")",
        (season_id,),
    ).fetchone()
    return {
        "season_id": season_id,
        "member": {
            "tag": member["tag"],
            "name": member["name"],
            "member_ref": _format_member_reference(member["tag"], conn=conn),
            "races_played": member_stats["races_played"] or 0,
            "total_fame": member_stats["total_fame"] or 0,
            "total_decks_used": member_stats["total_decks_used"] or 0,
            "avg_fame_per_race": round(member_stats["avg_fame_per_race"] or 0, 2),
            "participation_rate": round((member_stats["races_played"] or 0) / total_races, 4) if total_races else 0,
        },
        "clan_average": {
            "active_members": active_members,
            "participants_with_data": conn.execute(
                "SELECT COUNT(DISTINCT wp.player_tag) AS cnt "
                "FROM war_participation wp "
                "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "JOIN members m ON m.member_id = wp.member_id "
                "WHERE wr.season_id = ? AND m.status = 'active'",
                (season_id,),
            ).fetchone()["cnt"],
            "avg_total_fame": round(clan_avgs["avg_total_fame"] or 0, 2),
            "avg_races_played": round(clan_avgs["avg_races_played"] or 0, 2),
            "avg_fame_per_participant": round(clan_avgs["avg_fame_per_participant"] or 0, 2),
            "avg_total_decks": round(clan_avgs["avg_total_decks"] or 0, 2),
        },
    }

@managed_connection
def get_members_at_risk(inactivity_days: int = 7, min_donations_week: int = 20, require_war_participation: bool = False,
                        min_war_races: int = 1, tenure_grace_days: int = 14, include_leadership: bool = False,
                        season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    today = _member_activity_anchor(conn).date()
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, "
        "cs.clan_rank, cs.donations_week, cs.last_seen_api "
        "FROM members m "
        "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
        "WHERE m.status = 'active' "
        "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE"
    ).fetchall()

    flagged = []
    for row in rows:
        role = (row["role"] or "").strip()
        if not include_leadership and role in {"leader", "coLeader"}:
            continue
        joined_date = _current_joined_at(conn, row["member_id"])
        tenure_days = None
        if joined_date:
            try:
                tenure_days = (today - datetime.strptime(joined_date[:10], "%Y-%m-%d").date()).days
            except ValueError:
                tenure_days = None
        if tenure_days is not None and tenure_days < tenure_grace_days:
            continue

        reasons = []
        last_seen_dt = _parse_cr_time(row["last_seen_api"])
        if last_seen_dt is not None:
            days_inactive = (today - last_seen_dt.date()).days
            if days_inactive >= inactivity_days:
                reasons.append({
                    "type": "inactive",
                    "detail": f"last seen {days_inactive} days ago",
                    "value": days_inactive,
                })

        donations_week = row["donations_week"] or 0
        if donations_week < min_donations_week:
            reasons.append({
                "type": "low_donations",
                "detail": f"{donations_week} donations this week",
                "value": donations_week,
            })

        war_races_played = None
        if require_war_participation and season_id is not None:
            war_races_played = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_participation wp "
                "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                (season_id, row["member_id"]),
            ).fetchone()["cnt"]
            if war_races_played < min_war_races:
                reasons.append({
                    "type": "low_war_participation",
                    "detail": f"{war_races_played} war races played this season",
                    "value": war_races_played,
                })

        if reasons:
            item = dict(row)
            item["joined_date"] = joined_date
            item["tenure_days"] = tenure_days
            item["risk_score"] = len(reasons)
            item["reasons"] = reasons
            if war_races_played is not None:
                item["war_races_played"] = war_races_played
            item["cr_account_age_years"] = _get_account_age_years(conn, row["member_id"])
            item["war_player_type"] = _war_player_type(conn, row["member_id"])
            flagged.append(_member_reference_fields(conn, row["member_id"], item))

    flagged.sort(
        key=lambda item: (
            -item["risk_score"],
            item.get("clan_rank") if item.get("clan_rank") is not None else 999,
            (item.get("name") or "").lower(),
        )
    )
    return {
        "season_id": season_id,
        "criteria": {
            "inactivity_days": inactivity_days,
            "min_donations_week": min_donations_week,
            "require_war_participation": require_war_participation,
            "min_war_races": min_war_races,
            "tenure_grace_days": tenure_grace_days,
            "include_leadership": include_leadership,
        },
        "members": flagged,
    }

@managed_connection
def get_trending_war_contributors(season_id: Optional[str] = None, recent_races: int = 2, limit: int = 5, conn: Optional[sqlite3.Connection] = None) -> dict:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return {"season_id": None, "members": []}

    race_rows = conn.execute(
        "SELECT war_race_id, section_index FROM war_races WHERE season_id = ? ORDER BY section_index DESC",
        (season_id,),
    ).fetchall()
    if not race_rows:
        return {"season_id": season_id, "members": []}
    recent_ids = [row["war_race_id"] for row in race_rows[:recent_races]]
    prior_ids = [row["war_race_id"] for row in race_rows[recent_races:]]

    placeholders_recent = ",".join("?" for _ in recent_ids)
    recent_totals = conn.execute(
        f"SELECT wp.member_id, wp.player_tag AS tag, MAX(wp.player_name) AS name, "
        f"SUM(COALESCE(wp.fame, 0)) AS recent_fame, COUNT(*) AS recent_races "
        f"FROM war_participation wp "
        f"JOIN members m ON m.member_id = wp.member_id "
        f"WHERE wp.war_race_id IN ({placeholders_recent}) AND m.status = 'active' "
        f"GROUP BY wp.member_id, wp.player_tag",
        tuple(recent_ids),
    ).fetchall()

    prior_map = {}
    if prior_ids:
        placeholders_prior = ",".join("?" for _ in prior_ids)
        prior_rows = conn.execute(
            f"SELECT wp.member_id, wp.player_tag AS tag, SUM(COALESCE(wp.fame, 0)) AS prior_fame, COUNT(*) AS prior_races "
            f"FROM war_participation wp "
            f"JOIN members m ON m.member_id = wp.member_id "
            f"WHERE wp.war_race_id IN ({placeholders_prior}) AND m.status = 'active' "
            f"GROUP BY wp.member_id, wp.player_tag",
            tuple(prior_ids),
        ).fetchall()
        for row in prior_rows:
            prior_map[(row["member_id"], row["tag"])] = dict(row)

    members = []
    for row in recent_totals:
        recent_avg = (row["recent_fame"] or 0) / row["recent_races"] if row["recent_races"] else 0
        prior = prior_map.get((row["member_id"], row["tag"]), {})
        prior_avg = (prior.get("prior_fame") or 0) / prior.get("prior_races", 1) if prior.get("prior_races") else 0
        item = {
            "tag": row["tag"],
            "name": row["name"],
            "recent_fame": row["recent_fame"] or 0,
            "recent_races": row["recent_races"] or 0,
            "recent_avg_fame": round(recent_avg, 2),
            "prior_avg_fame": round(prior_avg, 2),
            "trend_delta": round(recent_avg - prior_avg, 2),
        }
        if row["member_id"] is not None:
            item = _member_reference_fields(conn, row["member_id"], item)
        members.append(item)

    members.sort(
        key=lambda item: (
            -item["trend_delta"],
            -item["recent_fame"],
            (item.get("name") or "").lower(),
        )
    )
    return {
        "season_id": season_id,
        "recent_races_considered": min(recent_races, len(race_rows)),
        "members": members[:limit],
    }

@managed_connection
def get_war_champ_standings(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    rows = conn.execute(
        "SELECT wp.player_tag AS tag, MAX(m.current_name) AS name, SUM(COALESCE(wp.fame, 0)) AS total_fame, COUNT(*) AS races_participated, ROUND(AVG(COALESCE(wp.fame, 0)), 0) AS avg_fame "
        "FROM war_participation wp "
        "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "JOIN members m ON m.member_id = wp.member_id "
        "WHERE wr.season_id = ? AND m.status = 'active' AND COALESCE(wp.fame, 0) > 0 "
        "GROUP BY wp.player_tag ORDER BY total_fame DESC, races_participated DESC",
        (season_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        member = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = ?",
            (_canon_tag(item["tag"]),),
        ).fetchone()
        if member:
            item = _member_reference_fields(conn, member["member_id"], item)
        result.append(item)
    return result

@managed_connection
def get_perfect_war_participants(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return []
    total_row = conn.execute("SELECT COUNT(*) AS cnt FROM war_races WHERE season_id = ?", (season_id,)).fetchone()
    total_races = total_row["cnt"] if total_row else 0
    if total_races == 0:
        return []
    rows = conn.execute(
        "SELECT wp.player_tag AS tag, MAX(m.current_name) AS name, COUNT(*) AS races_participated, SUM(COALESCE(wp.fame, 0)) AS total_fame "
        "FROM war_participation wp "
        "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
        "JOIN members m ON m.member_id = wp.member_id "
        "WHERE wr.season_id = ? AND m.status = 'active' AND COALESCE(wp.decks_used, 0) > 0 "
        "GROUP BY wp.player_tag HAVING COUNT(*) = ? ORDER BY total_fame DESC",
        (season_id, total_races),
    ).fetchall()
    result = []
    for row in rows:
        item = {**dict(row), "total_races_in_season": total_races}
        member = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = ?",
            (_canon_tag(item["tag"]),),
        ).fetchone()
        if member:
            item = _member_reference_fields(conn, member["member_id"], item)
        result.append(item)
    return result

@managed_connection
def get_recent_role_changes(days: int = 30, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (_member_snapshot_anchor(conn) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, "
        "curr.role AS new_role, prev.role AS old_role, curr.observed_at AS changed_at "
        "FROM member_state_snapshots curr "
        "JOIN member_state_snapshots prev ON prev.member_id = curr.member_id "
        "JOIN members m ON m.member_id = curr.member_id "
        "WHERE curr.observed_at >= ? "
        "AND prev.observed_at = ("
        "  SELECT MAX(p2.observed_at) FROM member_state_snapshots p2 "
        "  WHERE p2.member_id = curr.member_id AND p2.observed_at < curr.observed_at"
        ") "
        "AND COALESCE(curr.role, '') != COALESCE(prev.role, '') "
        "ORDER BY curr.observed_at DESC",
        (cutoff,),
    ).fetchall()
    seen = set()
    result = []
    for row in rows:
        if row["tag"] in seen:
            continue
        seen.add(row["tag"])
        result.append(_member_reference_fields(conn, row["member_id"], dict(row)))
    return result

@managed_connection
def get_war_battle_win_rates(season_id: Optional[str] = None, limit: int = 10, min_battles: int = 1, conn: Optional[sqlite3.Connection] = None) -> dict:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return {"season_id": None, "members": []}
    start_bound, end_bound = _season_bounds(conn, season_id)
    if not start_bound or not end_bound:
        return {"season_id": season_id, "members": []}
    rows = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, "
        "SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN bf.outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
        "SUM(CASE WHEN bf.outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
        "COUNT(*) AS battles "
        "FROM member_battle_facts bf "
        "JOIN members m ON m.member_id = bf.member_id "
        "WHERE m.status = 'active' AND bf.is_war = 1 AND bf.battle_time >= ? AND bf.battle_time < ? "
        "GROUP BY m.member_id "
        "HAVING COUNT(*) >= ? "
        "ORDER BY CAST(SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) DESC, COUNT(*) DESC, m.current_name COLLATE NOCASE",
        (start_bound, end_bound, min_battles),
    ).fetchall()
    members = []
    for row in rows[:limit]:
        item = dict(row)
        item["win_rate"] = round((item["wins"] or 0) / item["battles"], 4) if item["battles"] else 0
        members.append(_member_reference_fields(conn, row["member_id"], item))
    return {
        "season_id": season_id,
        "min_battles": min_battles,
        "members": members,
    }

@managed_connection
def get_clan_boat_battle_record(wars: int = 3, conn: Optional[sqlite3.Connection] = None) -> dict:
    race_rows = conn.execute(
        "SELECT war_race_id, season_id, section_index, created_date "
        "FROM war_races WHERE created_date IS NOT NULL "
        "ORDER BY created_date DESC LIMIT ?",
        (wars,),
    ).fetchall()
    if not race_rows:
        return {"wars_considered": 0, "wins": 0, "losses": 0, "draws": 0, "battles": 0, "per_war": []}

    selected = list(reversed(race_rows))
    per_war = []
    wins = losses = draws = battles = 0
    for idx, row in enumerate(selected):
        start_dt = _parse_cr_time(row["created_date"])
        if not start_dt:
            continue
        if idx + 1 < len(selected):
            end_dt = _parse_cr_time(selected[idx + 1]["created_date"])
        else:
            end_dt = start_dt + timedelta(days=7)
        if not end_dt:
            end_dt = start_dt + timedelta(days=7)
        start_key = start_dt.strftime("%Y%m%dT%H%M%S.000Z")
        end_key = end_dt.strftime("%Y%m%dT%H%M%S.000Z")
        stats = conn.execute(
            "SELECT "
            "SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
            "SUM(CASE WHEN outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
            "COUNT(*) AS battles "
            "FROM member_battle_facts "
            "WHERE battle_type = 'boatBattle' AND battle_time >= ? AND battle_time < ?",
            (start_key, end_key),
        ).fetchone()
        item = {
            "season_id": row["season_id"],
            "section_index": row["section_index"],
            "wins": stats["wins"] or 0,
            "losses": stats["losses"] or 0,
            "draws": stats["draws"] or 0,
            "battles": stats["battles"] or 0,
        }
        per_war.append(item)
        wins += item["wins"]
        losses += item["losses"]
        draws += item["draws"]
        battles += item["battles"]
    return {
        "wars_considered": len(per_war),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "battles": battles,
        "per_war": list(reversed(per_war)),
    }

@managed_connection
def get_war_score_trend(days: int = 30, conn: Optional[sqlite3.Connection] = None) -> dict:
    anchor = _war_trend_anchor(conn)
    cutoff = (anchor - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    first = conn.execute(
        "SELECT observed_at, clan_score, fame, war_state FROM war_current_state "
        "WHERE observed_at >= ? AND clan_score IS NOT NULL ORDER BY observed_at ASC LIMIT 1",
        (cutoff,),
    ).fetchone()
    last = conn.execute(
        "SELECT observed_at, clan_score, fame, war_state FROM war_current_state "
        "WHERE observed_at >= ? AND clan_score IS NOT NULL ORDER BY observed_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    race_cutoff = (anchor - timedelta(days=days)).strftime("%Y%m%dT%H%M%S.000Z")
    race_stats = conn.execute(
        "SELECT COUNT(*) AS races, SUM(COALESCE(trophy_change, 0)) AS trophy_change_total, "
        "AVG(COALESCE(our_rank, 0)) AS avg_rank, AVG(COALESCE(our_fame, 0)) AS avg_fame "
        "FROM war_races WHERE created_date >= ?",
        (race_cutoff,),
    ).fetchone()
    if not first or not last:
        return {
            "window_days": days,
            "direction": "unknown",
            "score_change": None,
            "trophy_change_total": race_stats["trophy_change_total"] or 0,
            "races": race_stats["races"] or 0,
        }
    score_change = (last["clan_score"] or 0) - (first["clan_score"] or 0)
    direction = "flat"
    if score_change > 0:
        direction = "up"
    elif score_change < 0:
        direction = "down"
    return {
        "window_days": days,
        "direction": direction,
        "start": dict(first),
        "end": dict(last),
        "score_change": score_change,
        "trophy_change_total": race_stats["trophy_change_total"] or 0,
        "races": race_stats["races"] or 0,
        "avg_rank": round(race_stats["avg_rank"] or 0, 2) if race_stats["races"] else None,
        "avg_fame": round(race_stats["avg_fame"] or 0, 2) if race_stats["races"] else None,
    }

@managed_connection
def compare_fame_per_member_to_previous_season(season_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    if season_id is None:
        season_id = get_current_season_id(conn=conn)
    if season_id is None:
        return None
    previous_row = conn.execute(
        "SELECT MAX(season_id) AS season_id FROM war_races WHERE season_id < ?",
        (season_id,),
    ).fetchone()
    previous_season_id = previous_row["season_id"] if previous_row else None
    if previous_season_id is None:
        return {
            "current_season_id": season_id,
            "previous_season_id": None,
            "current": None,
            "previous": None,
            "direction": "unknown",
            "delta": None,
        }

    def _season_stats(target_season_id):
        row = conn.execute(
            "SELECT COUNT(*) AS races, SUM(COALESCE(our_fame, 0)) AS total_fame "
            "FROM war_races WHERE season_id = ?",
            (target_season_id,),
        ).fetchone()
        participants = conn.execute(
            "SELECT COUNT(DISTINCT player_tag) AS cnt "
            "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
            "WHERE wr.season_id = ? AND COALESCE(wp.decks_used, 0) > 0",
            (target_season_id,),
        ).fetchone()["cnt"]
        total_fame = row["total_fame"] or 0
        return {
            "season_id": target_season_id,
            "races": row["races"] or 0,
            "participants": participants or 0,
            "total_fame": total_fame,
            "fame_per_member": round(total_fame / participants, 2) if participants else 0,
        }

    current = _season_stats(season_id)
    previous = _season_stats(previous_season_id)
    delta = current["fame_per_member"] - previous["fame_per_member"]
    direction = "flat"
    if delta > 0:
        direction = "up"
    elif delta < 0:
        direction = "down"
    return {
        "current_season_id": season_id,
        "previous_season_id": previous_season_id,
        "current": current,
        "previous": previous,
        "direction": direction,
        "delta": round(delta, 2),
    }

@managed_connection
def get_promotion_candidates(min_donations_week: int = 50, min_tenure_days: int = 14, active_within_days: int = 7,
                             min_war_races: int = 1, conn: Optional[sqlite3.Connection] = None) -> dict:
    season_id = get_current_season_id(conn=conn)
    counts = conn.execute(
        "SELECT "
        "SUM(CASE WHEN cs.role IN ('leader', 'coLeader') THEN 1 ELSE 0 END) AS leaders, "
        "SUM(CASE WHEN cs.role = 'elder' THEN 1 ELSE 0 END) AS elders, "
        "SUM(CASE WHEN cs.role = 'member' THEN 1 ELSE 0 END) AS members, "
        "COUNT(*) AS active_members "
        "FROM members m JOIN member_current_state cs ON cs.member_id = m.member_id "
        "WHERE m.status = 'active'"
    ).fetchone()
    active_members = counts["active_members"] or 0
    target_elder_min = max(0, round(active_members * 0.2))
    target_elder_max = max(target_elder_min, round(active_members * 0.3))

    rows = conn.execute(
        "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, cs.best_trophies, "
        "cs.clan_rank, cs.donations_week AS donations, cs.donations_received_week AS donations_received, cs.last_seen_api AS last_seen "
        "FROM members m "
        "JOIN member_current_state cs ON cs.member_id = m.member_id "
        "WHERE m.status = 'active' AND cs.role = 'member' "
        "ORDER BY cs.donations_week DESC, cs.trophies DESC, m.current_name COLLATE NOCASE",
    ).fetchall()
    recommended = []
    borderline = []
    today = _member_activity_anchor(conn).date()

    for row in rows:
        joined_date = _current_joined_at(conn, row["member_id"])
        tenure_days = None
        if joined_date:
            try:
                tenure_days = (today - datetime.strptime(joined_date[:10], "%Y-%m-%d").date()).days
            except ValueError:
                tenure_days = None
        last_seen = _parse_cr_time(row["last_seen"])
        days_inactive = (today - last_seen.date()).days if last_seen else None
        war_races_played = 0
        if season_id is not None:
            war_races_played = conn.execute(
                "SELECT COUNT(*) AS cnt FROM war_participation wp "
                "JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                "WHERE wr.season_id = ? AND wp.member_id = ? AND COALESCE(wp.decks_used, 0) > 0",
                (season_id, row["member_id"]),
            ).fetchone()["cnt"]

        checks = {
            "donations": (row["donations"] or 0) >= min_donations_week,
            "tenure": tenure_days is not None and tenure_days >= min_tenure_days,
            "activity": days_inactive is not None and days_inactive <= active_within_days,
            "war": season_id is None or war_races_played >= min_war_races,
        }
        score = sum(1 for passed in checks.values() if passed)
        item = {
            "tag": row["tag"],
            "name": row["name"],
            "exp_level": row["exp_level"],
            "trophies": row["trophies"],
            "best_trophies": row["best_trophies"],
            "clan_rank": row["clan_rank"],
            "donations": row["donations"] or 0,
            "donations_received": row["donations_received"] or 0,
            "joined_date": joined_date,
            "tenure_days": tenure_days,
            "days_inactive": days_inactive,
            "war_races_played": war_races_played,
            "score": score,
            "checks": checks,
            "missing": [key for key, passed in checks.items() if not passed],
        }
        item["cr_account_age_years"] = _get_account_age_years(conn, row["member_id"])
        item["war_player_type"] = _war_player_type(conn, row["member_id"])
        item = _member_reference_fields(conn, row["member_id"], item)
        if all(checks.values()):
            recommended.append(item)
        elif score >= 2:
            borderline.append(item)

    recommended.sort(key=lambda item: (-item["score"], -item["donations"], -item["war_races_played"], -item["trophies"]))
    borderline.sort(key=lambda item: (-item["score"], -item["donations"], -item["war_races_played"], -item["trophies"]))
    composition = {
        "active_members": active_members,
        "leaders": counts["leaders"] or 0,
        "elders": counts["elders"] or 0,
        "members": counts["members"] or 0,
        "target_elder_min": target_elder_min,
        "target_elder_max": target_elder_max,
        "elder_capacity_remaining": max(0, target_elder_max - (counts["elders"] or 0)),
    }
    return {
        "season_id": season_id,
        "criteria": {
            "min_donations_week": min_donations_week,
            "min_tenure_days": min_tenure_days,
            "active_within_days": active_within_days,
            "min_war_races": min_war_races,
        },
        "composition": composition,
        "recommended": recommended,
        "borderline": borderline,
    }


_WAR_DECK_BATTLE_TYPES = {"riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum"}


def _deck_card_summary(cards: list[dict]) -> list[dict]:
    """Strip a card list down to display-relevant fields."""
    summary = []
    for card in cards:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        summary.append({
            "name": card["name"],
            "level": card.get("level"),
            "max_level": card.get("maxLevel"),
            "elixir_cost": card.get("elixirCost"),
            "rarity": card.get("rarity"),
            "evolution_level": card.get("evolutionLevel"),
            "icon_url": (card.get("iconUrls") or {}).get("medium") if isinstance(card.get("iconUrls"), dict) else None,
        })
    return summary


def _extract_deck_candidates(rows: list[sqlite3.Row]) -> list[dict]:
    """Walk war battle rows and yield candidate decks (one per duel round, one per riverRacePvP).

    Returns a list of dicts with: cards (list), key (frozenset of names), battle_time, source.
    """
    candidates = []
    for row in rows:
        battle_type = row["battle_type"]
        battle_time = row["battle_time"]
        if battle_type in ("riverRaceDuel", "riverRaceDuelColosseum"):
            try:
                rounds = json.loads(row["team_rounds_json"] or "[]")
            except (TypeError, ValueError):
                rounds = []
            for idx, rnd in enumerate(rounds):
                cards = rnd.get("cards") if isinstance(rnd, dict) else None
                if not isinstance(cards, list) or len(cards) != 8:
                    continue
                names = [c.get("name") for c in cards if isinstance(c, dict) and c.get("name")]
                if len(names) != 8 or len(set(names)) != 8:
                    continue
                candidates.append({
                    "cards": cards,
                    "key": frozenset(names),
                    "battle_time": battle_time,
                    "source": f"{battle_type}#round{idx + 1}",
                })
        elif battle_type == "riverRacePvP":
            try:
                cards = json.loads(row["deck_json"] or "[]")
            except (TypeError, ValueError):
                cards = []
            if len(cards) != 8:
                continue
            names = [c.get("name") for c in cards if isinstance(c, dict) and c.get("name")]
            if len(names) != 8 or len(set(names)) != 8:
                continue
            candidates.append({
                "cards": cards,
                "key": frozenset(names),
                "battle_time": battle_time,
                "source": "riverRacePvP",
            })
    return candidates


def _group_candidates(candidates: list[dict]) -> list[dict]:
    """Group candidates by exact deck composition. Returns list sorted by recency then frequency."""
    grouped: dict[frozenset, dict] = {}
    for cand in candidates:
        bucket = grouped.get(cand["key"])
        if bucket is None:
            grouped[cand["key"]] = {
                "key": cand["key"],
                "cards": cand["cards"],
                "occurrences": 1,
                "latest_battle_time": cand["battle_time"],
                "earliest_battle_time": cand["battle_time"],
                "sources": [cand["source"]],
            }
        else:
            bucket["occurrences"] += 1
            if cand["battle_time"] and (not bucket["latest_battle_time"] or cand["battle_time"] > bucket["latest_battle_time"]):
                bucket["latest_battle_time"] = cand["battle_time"]
                bucket["cards"] = cand["cards"]  # keep most-recent card-level data
            if cand["battle_time"] and (not bucket["earliest_battle_time"] or cand["battle_time"] < bucket["earliest_battle_time"]):
                bucket["earliest_battle_time"] = cand["battle_time"]
            if cand["source"] not in bucket["sources"]:
                bucket["sources"].append(cand["source"])
    return sorted(
        grouped.values(),
        key=lambda d: (d["latest_battle_time"] or "", d["occurrences"]),
        reverse=True,
    )


def _select_war_decks(distinct_decks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Greedy partition: pick up to 4 non-overlapping decks; return (selected, skipped_due_to_overlap)."""
    selected: list[dict] = []
    skipped: list[dict] = []
    used_cards: set[str] = set()
    for deck in distinct_decks:
        if len(selected) >= 4:
            break
        if used_cards.isdisjoint(deck["key"]):
            selected.append(deck)
            used_cards |= deck["key"]
        else:
            skipped.append(deck)
    return selected, skipped


def _war_decks_confidence(
    selected: list[dict],
    skipped: list[dict],
    war_battles_seen: int,
    rows: list[sqlite3.Row],
) -> str:
    """Classify confidence: high / medium / low."""
    if len(selected) < 4:
        # Confidence rules only matter if we're returning decks at all.
        return "low" if skipped else "medium"
    # Look for a recent (top-3) duel that contributed >= 3 selected decks.
    recent_duels = [r for r in rows[:3] if r["battle_type"] in ("riverRaceDuel", "riverRaceDuelColosseum")]
    selected_keys = {d["key"] for d in selected}
    for duel in recent_duels:
        try:
            rounds = json.loads(duel["team_rounds_json"] or "[]")
        except (TypeError, ValueError):
            continue
        round_keys = []
        for rnd in rounds:
            cards = rnd.get("cards") if isinstance(rnd, dict) else None
            if not isinstance(cards, list) or len(cards) != 8:
                continue
            names = [c.get("name") for c in cards if isinstance(c, dict) and c.get("name")]
            if len(names) == 8 and len(set(names)) == 8:
                round_keys.append(frozenset(names))
        matched = sum(1 for k in round_keys if k in selected_keys)
        if matched >= 3 and not skipped:
            return "high"
    if skipped:
        return "low" if len(skipped) >= len(selected) else "medium"
    return "medium"


@managed_connection
def reconstruct_member_war_decks(
    tag: str,
    lookback_battles: int = 80,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Reconstruct a player's four war decks from their River Race battle history.

    The Clash Royale API exposes only the trophy-road `currentDeck`, not the four
    war decks. We approximate them by extracting decks from recent war battles —
    duels reveal up to 3 decks per battle (one per round), and riverRacePvP
    battles reveal one deck each. We then greedily partition the most-recent
    distinct decks into 4 non-overlapping decks (the no-overlap constraint of
    the war deck pool).
    """
    member_tag = _canon_tag(tag)
    member_row = conn.execute(
        "SELECT member_id, current_name FROM members WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return {
            "status": "insufficient_data",
            "member_tag": member_tag,
            "reason": "Member not found in roster.",
            "decks": [],
            "evidence": {"war_battles_seen": 0, "distinct_decks_observed": 0},
            "guidance": "Resolve the member tag first or ask the user to confirm who they meant.",
        }
    member_id = member_row["member_id"]
    placeholders = ",".join("?" for _ in _WAR_DECK_BATTLE_TYPES)
    rows = conn.execute(
        f"SELECT battle_time, battle_type, deck_json, team_rounds_json, deck_selection "
        f"FROM member_battle_facts "
        f"WHERE member_id = ? AND is_war = 1 AND battle_type IN ({placeholders}) "
        f"ORDER BY battle_time DESC LIMIT ?",
        (member_id, *_WAR_DECK_BATTLE_TYPES, lookback_battles),
    ).fetchall()
    war_battles_seen = len(rows)
    candidates = _extract_deck_candidates(rows)
    distinct_decks = _group_candidates(candidates)

    base_payload = {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "evidence": {
            "war_battles_seen": war_battles_seen,
            "distinct_decks_observed": len(distinct_decks),
            "candidate_decks_extracted": len(candidates),
            "duel_battles_seen": sum(1 for r in rows if r["battle_type"] in ("riverRaceDuel", "riverRaceDuelColosseum")),
            "earliest_battle_time": rows[-1]["battle_time"] if rows else None,
            "latest_battle_time": rows[0]["battle_time"] if rows else None,
        },
    }

    # A single duel yields up to 3 candidate decks, so we gate on candidate
    # count rather than raw war_battles_seen — that way a duel-heavy player
    # with few battles can still be reconstructed.
    if len(candidates) < 3 or len(distinct_decks) < 2:
        return {
            **base_payload,
            "status": "insufficient_data",
            "decks": [],
            "gaps": [
                f"Only {war_battles_seen} war battle(s) recorded for this member; "
                f"{len(distinct_decks)} distinct deck(s) observed across {len(candidates)} candidate(s). "
                "Need at least 3 candidate decks (e.g. 1 duel or 3 river-race battles) and "
                "2 distinct compositions before reconstruction is meaningful."
            ],
            "guidance": (
                "Do not present a half-built reconstruction. Tell the user there isn't "
                "enough recent war battle data, and offer to either build them four war "
                "decks from their card collection (suggest mode) or ask them to paste "
                "the four decks manually."
            ),
        }

    selected, skipped = _select_war_decks(distinct_decks)
    confidence = _war_decks_confidence(selected, skipped, war_battles_seen, rows)
    decks_payload = [
        {
            "deck_index": i + 1,
            "cards": _deck_card_summary(deck["cards"]),
            "occurrences": deck["occurrences"],
            "latest_used_at": deck["latest_battle_time"],
            "earliest_used_at": deck["earliest_battle_time"],
            "sources": deck["sources"],
        }
        for i, deck in enumerate(selected)
    ]
    gaps: list[str] = []
    if len(selected) < 4:
        gaps.append(
            f"Only {len(selected)} of 4 war decks could be reconstructed from {war_battles_seen} "
            f"recent war battle(s). Ask the user to confirm or fill in the missing deck(s)."
        )
    if skipped:
        gaps.append(
            f"{len(skipped)} candidate deck(s) were skipped because they shared cards with already-"
            "selected decks. This often means the player has changed their war decks recently — "
            "ask the user to confirm the reconstruction is current."
        )

    status = "reconstructed" if len(selected) == 4 else "partial"
    return {
        **base_payload,
        "status": status,
        "confidence": confidence,
        "decks": decks_payload,
        "skipped_candidates": [
            {
                "cards": [c.get("name") for c in deck["cards"] if isinstance(c, dict)],
                "occurrences": deck["occurrences"],
                "latest_used_at": deck["latest_battle_time"],
                "sources": deck["sources"],
            }
            for deck in skipped[:5]
        ],
        "gaps": gaps,
        "guidance": (
            "If status is 'reconstructed' with confidence='high', proceed straight to per-deck "
            "review. Otherwise present the reconstructed decks to the user and ask them to "
            "confirm or correct before reviewing. Always enforce the no-overlap rule when "
            "suggesting swaps: a card moved into one deck must come out of wherever it currently "
            "lives across the other three."
        ),
    }
