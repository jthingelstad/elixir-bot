def get_current_war_status(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        war = conn.execute(
            "SELECT observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score "
            "FROM war_current_state ORDER BY observed_at DESC, war_id DESC LIMIT 1"
        ).fetchone()
        if not war:
            return None
        season_id = get_current_season_id(conn=conn)
        current_race = None
        if season_id is not None:
            current_race = conn.execute(
                "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time "
                "FROM war_races WHERE season_id = ? ORDER BY section_index DESC LIMIT 1",
                (season_id,),
            ).fetchone()
        result = dict(war)
        if current_race:
            result["season_id"] = current_race["season_id"]
            result["section_index"] = current_race["section_index"]
            result["week"] = current_race["section_index"] + 1 if current_race["section_index"] is not None else None
            result["race_rank"] = current_race["our_rank"]
            result["trophy_change"] = current_race["trophy_change"]
        return result
    finally:
        if close:
            conn.close()


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
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
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
                "member_ref": format_member_reference(member["tag"], style="name_with_handle", conn=conn),
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


# -- War --------------------------------------------------------------------

def store_war_log(race_log, clan_tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        clan_tag = _tag_key(clan_tag)
        _store_raw_payload(conn, "clan_war_log", clan_tag, race_log)
        stored = 0
        for entry in (race_log or {}).get("items", []):
            season_id = entry.get("seasonId")
            section_index = entry.get("sectionIndex")
            standings = entry.get("standings", [])
            our = None
            for standing in standings:
                clan = standing.get("clan", {})
                if _tag_key(clan.get("tag")) == clan_tag:
                    our = standing
                    break
            total_clans = len(standings)
            trophy_change = our.get("trophyChange") if our else None
            our_rank = our.get("rank") if our else None
            clan = (our or {}).get("clan", {})
            cur = conn.execute(
                "INSERT OR IGNORE INTO war_races (season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (season_id, section_index, entry.get("createdDate"), our_rank, trophy_change, clan.get("fame"), total_clans, clan.get("finishTime"), _json_or_none(entry)),
            )
            if cur.rowcount == 0:
                race_row = conn.execute("SELECT war_race_id FROM war_races WHERE season_id = ? AND section_index = ?", (season_id, section_index)).fetchone()
                war_race_id = race_row["war_race_id"]
            else:
                war_race_id = cur.lastrowid
                stored += 1

            if our:
                for participant in clan.get("participants", []):
                    ptag = _canon_tag(participant.get("tag"))
                    member_id = _ensure_member(conn, ptag, participant.get("name"), status=None) if ptag else None
                    conn.execute(
                        "INSERT OR REPLACE INTO war_participation (war_race_id, member_id, player_tag, player_name, fame, repair_points, boat_attacks, decks_used, decks_used_today, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (war_race_id, member_id, ptag, participant.get("name"), participant.get("fame", 0), participant.get("repairPoints", 0), participant.get("boatAttacks", 0), participant.get("decksUsed", 0), participant.get("decksUsedToday", 0), _json_or_none(participant)),
                    )
        conn.commit()
        return stored
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


def get_current_season_id(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute("SELECT MAX(season_id) AS sid FROM war_races").fetchone()
        return row["sid"] if row else None
    finally:
        if close:
            conn.close()


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
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
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
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "battles": battles,
            "win_rate": round(wins / battles, 4) if battles else 0,
        }
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
            "member_ref": format_member_reference(canon_tag, style="name_with_handle", conn=conn),
            "tracked_days": len(tracked_days),
            "days_participated": participated,
            "days_missed": len(missed),
            "missed_dates": missed,
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


def upsert_war_current_state(war_data, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        observed_at = _utcnow()
        clan = (war_data or {}).get("clan", {})
        conn.execute(
            "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observed_at, war_data.get("state"), _canon_tag(clan.get("tag")), clan.get("name"), clan.get("fame"), clan.get("repairPoints"), clan.get("periodPoints"), clan.get("clanScore"), _json_or_none(war_data)),
        )
        battle_date = observed_at[:10]
        for participant in clan.get("participants", []):
            member_id = _ensure_member(conn, participant.get("tag"), participant.get("name"), status=None)
            conn.execute(
                "INSERT INTO war_day_status (member_id, battle_date, observed_at, fame, repair_points, boat_attacks, decks_used_total, decks_used_today, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, battle_date) DO UPDATE SET observed_at = excluded.observed_at, fame = excluded.fame, repair_points = excluded.repair_points, boat_attacks = excluded.boat_attacks, decks_used_total = excluded.decks_used_total, decks_used_today = excluded.decks_used_today, raw_json = excluded.raw_json",
                (member_id, battle_date, observed_at, participant.get("fame", 0), participant.get("repairPoints", 0), participant.get("boatAttacks", 0), participant.get("decksUsed", 0), participant.get("decksUsedToday", 0), _json_or_none(participant)),
            )
        conn.commit()
    finally:
        if close:
            conn.close()


# -- Player profiles and battle facts --------------------------------------

