from __future__ import annotations

from datetime import datetime, timedelta, timezone

from db import (
    _canon_tag,
    _current_joined_at,
    _member_reference_fields,
    _parse_cr_time,
    _utcnow,
    get_connection,
)
from storage.war_status import _season_bounds, get_current_season_id

def _format_member_reference(*args, **kwargs):
    from storage.identity import format_member_reference

    return format_member_reference(*args, **kwargs)

def get_members_without_war_participation(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def compare_member_war_to_clan_average(tag, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
                "member_ref": _format_member_reference(member["tag"], style="name_with_handle", conn=conn),
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
    finally:
        if close:
            conn.close()

def get_members_at_risk(inactivity_days=7, min_donations_week=20, require_war_participation=False,
                        min_war_races=1, tenure_grace_days=14, season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            season_id = get_current_season_id(conn=conn)
        today = datetime.now(timezone.utc).date()
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
            },
            "members": flagged,
        }
    finally:
        if close:
            conn.close()

def get_trending_war_contributors(season_id=None, recent_races=2, limit=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def get_war_champ_standings(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def get_perfect_war_participants(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def get_recent_role_changes(days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
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
    finally:
        if close:
            conn.close()

def get_war_battle_win_rates(season_id=None, limit=10, min_battles=1, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def get_clan_boat_battle_record(wars=3, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def get_war_score_trend(days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
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
        race_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S.000Z")
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
    finally:
        if close:
            conn.close()

def compare_fame_per_member_to_previous_season(season_id=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
    finally:
        if close:
            conn.close()

def get_promotion_candidates(min_donations_week=50, min_tenure_days=14, active_within_days=7,
                             min_war_races=1, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
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
        today = datetime.now(timezone.utc).date()

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
    finally:
        if close:
            conn.close()
