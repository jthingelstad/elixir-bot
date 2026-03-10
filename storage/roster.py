import json
from datetime import datetime, timedelta, timezone

from db import (
    _canon_tag,
    _card_level,
    _current_joined_at,
    _ensure_member,
    _get_current_membership,
    _json_or_none,
    _member_reference_fields,
    _rowdicts,
    _utcnow,
    get_connection,
)
def snapshot_members(member_list, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        observed_at = _utcnow()
        today = observed_at[:10]
        bootstrap_snapshot = conn.execute(
            "SELECT COUNT(*) AS cnt FROM member_current_state"
        ).fetchone()["cnt"] == 0
        seen_tags = set()
        for member in member_list:
            tag = _canon_tag(member.get("tag"))
            if not tag:
                continue
            seen_tags.add(tag)
            name = member.get("name") or ""
            member_id = _ensure_member(conn, tag, name=name, status="active")
            previous = conn.execute(
                "SELECT role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api "
                "FROM member_current_state WHERE member_id = ?",
                (member_id,),
            ).fetchone()
            arena = member.get("arena") or {}
            arena_id = arena.get("id") if isinstance(arena, dict) else None
            arena_name = arena.get("name") if isinstance(arena, dict) else str(arena or "")
            arena_raw_name = arena.get("rawName") if isinstance(arena, dict) else None
            last_seen_api = member.get("lastSeen", member.get("last_seen"))
            state = {
                "observed_at": observed_at,
                "role": member.get("role", "member"),
                "exp_level": member.get("expLevel", member.get("exp_level")),
                "trophies": member.get("trophies", 0),
                "best_trophies": member.get("bestTrophies", member.get("best_trophies")),
                "clan_rank": member.get("clanRank", member.get("clan_rank")),
                "previous_clan_rank": member.get("previousClanRank"),
                "donations_week": member.get("donations", 0),
                "donations_received_week": member.get("donationsReceived", member.get("donations_received", 0)),
                "arena_id": arena_id,
                "arena_name": arena_name,
                "arena_raw_name": arena_raw_name,
                "last_seen_api": last_seen_api,
                "source": "clan_api",
                "raw_json": _json_or_none(member),
            }
            state_changed = (
                previous is None
                or previous["role"] != state["role"]
                or previous["exp_level"] != state["exp_level"]
                or previous["trophies"] != state["trophies"]
                or previous["best_trophies"] != state["best_trophies"]
                or previous["clan_rank"] != state["clan_rank"]
                or previous["previous_clan_rank"] != state["previous_clan_rank"]
                or previous["donations_week"] != state["donations_week"]
                or previous["donations_received_week"] != state["donations_received_week"]
                or previous["arena_id"] != state["arena_id"]
                or previous["arena_name"] != state["arena_name"]
                or previous["arena_raw_name"] != state["arena_raw_name"]
                or previous["last_seen_api"] != state["last_seen_api"]
            )
            conn.execute(
                "INSERT INTO member_current_state (member_id, observed_at, role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api, source, raw_json) "
                "VALUES (:member_id, :observed_at, :role, :exp_level, :trophies, :best_trophies, :clan_rank, :previous_clan_rank, :donations_week, :donations_received_week, :arena_id, :arena_name, :arena_raw_name, :last_seen_api, :source, :raw_json) "
                "ON CONFLICT(member_id) DO UPDATE SET observed_at = excluded.observed_at, role = excluded.role, exp_level = excluded.exp_level, trophies = excluded.trophies, best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank, previous_clan_rank = excluded.previous_clan_rank, donations_week = excluded.donations_week, donations_received_week = excluded.donations_received_week, arena_id = excluded.arena_id, arena_name = excluded.arena_name, arena_raw_name = excluded.arena_raw_name, last_seen_api = excluded.last_seen_api, source = excluded.source, raw_json = excluded.raw_json",
                {"member_id": member_id, **state},
            )
            if state_changed:
                conn.execute(
                    "INSERT INTO member_state_snapshots (member_id, observed_at, name, role, exp_level, trophies, best_trophies, clan_rank, previous_clan_rank, donations_week, donations_received_week, arena_id, arena_name, arena_raw_name, last_seen_api, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        member_id,
                        observed_at,
                        name,
                        state["role"],
                        state["exp_level"],
                        state["trophies"],
                        state["best_trophies"],
                        state["clan_rank"],
                        state["previous_clan_rank"],
                        state["donations_week"],
                        state["donations_received_week"],
                        state["arena_id"],
                        state["arena_name"],
                        state["arena_raw_name"],
                        state["last_seen_api"],
                        state["raw_json"],
                    ),
                )
            conn.execute(
                "INSERT INTO member_daily_metrics (member_id, metric_date, exp_level, trophies, best_trophies, clan_rank, donations_week, donations_received_week, last_seen_api) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, metric_date) DO UPDATE SET exp_level = excluded.exp_level, trophies = excluded.trophies, best_trophies = excluded.best_trophies, clan_rank = excluded.clan_rank, donations_week = excluded.donations_week, donations_received_week = excluded.donations_received_week, last_seen_api = excluded.last_seen_api",
                (member_id, today, state["exp_level"], state["trophies"], state["best_trophies"], state["clan_rank"], state["donations_week"], state["donations_received_week"], state["last_seen_api"]),
            )
            if not _get_current_membership(conn, member_id):
                conn.execute(
                    "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source, leave_source) VALUES (?, ?, NULL, ?, NULL)",
                    (member_id, today, "bootstrap_seed" if bootstrap_snapshot else "clan_api_snapshot"),
                )

        if seen_tags:
            placeholders = ",".join("?" for _ in seen_tags)
            conn.execute(
                f"UPDATE members SET status = CASE WHEN player_tag IN ({placeholders}) THEN 'active' ELSE status END",
                tuple(seen_tags),
            )
        conn.commit()
        return len(seen_tags)
    finally:
        if close:
            conn.close()


def get_active_roster_map(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT player_tag, current_name FROM members WHERE status = 'active' ORDER BY current_name COLLATE NOCASE"
        ).fetchall()
        return {r["player_tag"]: r["current_name"] for r in rows}
    finally:
        if close:
            conn.close()


def get_member_history(tag, days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "SELECT m.player_tag AS tag, s.name, s.trophies, s.best_trophies, s.donations_week AS donations, s.donations_received_week AS donations_received, s.role, s.arena_id, s.arena_name, s.exp_level, s.clan_rank, s.last_seen_api AS last_seen, s.observed_at AS recorded_at "
            "FROM member_state_snapshots s JOIN members m ON m.member_id = s.member_id "
            "WHERE m.player_tag = ? AND s.observed_at >= ? ORDER BY s.observed_at ASC",
            (_canon_tag(tag), cutoff),
        ).fetchall()
        return _rowdicts(rows)
    finally:
        if close:
            conn.close()


def resolve_member(query, status="active", limit=5, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        query = (query or "").strip()
        if not query:
            return []
        query_lower = query.lower()
        query_handle = query_lower.lstrip("@")
        query_tag = _canon_tag(query) if query.startswith("#") else ""

        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name, m.status, cs.role, cs.exp_level, cs.trophies, cs.clan_rank, "
            "dl.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "WHERE (? IS NULL OR m.status = ?) "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (status, status),
        ).fetchall()
        aliases = {}
        for row in conn.execute(
            "SELECT member_id, alias FROM member_aliases"
        ).fetchall():
            aliases.setdefault(row["member_id"], []).append(row["alias"])

        candidates = []
        for row in rows:
            member = dict(row)
            member["joined_date"] = _current_joined_at(conn, row["member_id"])
            member["in_discord"] = 1 if row["discord_user_id"] else 0
            member_aliases = aliases.get(row["member_id"], [])
            score = 0
            source = None

            name = (member.get("current_name") or "").lower()
            discord_username = (member.get("discord_username") or "").lower()
            discord_display = (member.get("discord_display_name") or "").lower()
            alias_lowers = [a.lower() for a in member_aliases]

            if query_tag and member["player_tag"] == query_tag:
                score, source = 1000, "player_tag_exact"
            elif name == query_lower:
                score, source = 950, "current_name_exact"
            elif query_lower in alias_lowers:
                score, source = 900, "alias_exact"
            elif discord_username == query_handle:
                score, source = 875, "discord_username_exact"
            elif discord_display == query_lower or discord_display == query_handle:
                score, source = 850, "discord_display_exact"
            elif name.startswith(query_lower):
                score, source = 775, "current_name_prefix"
            elif any(a.startswith(query_lower) for a in alias_lowers):
                score, source = 750, "alias_prefix"
            elif discord_username.startswith(query_handle) and query_handle:
                score, source = 725, "discord_username_prefix"
            elif query_lower in name:
                score, source = 650, "current_name_contains"
            elif any(query_lower in a for a in alias_lowers):
                score, source = 625, "alias_contains"
            elif query_handle and query_handle in discord_username:
                score, source = 600, "discord_username_contains"
            elif query_lower and query_lower in discord_display:
                score, source = 575, "discord_display_contains"

            if score:
                member["match_score"] = score
                member["match_source"] = source
                member["aliases"] = member_aliases
                candidates.append(_member_reference_fields(conn, row["member_id"], member))

        candidates.sort(
            key=lambda item: (
                -item["match_score"],
                item.get("clan_rank") if item.get("clan_rank") is not None else 999,
                (item.get("current_name") or "").lower(),
            )
        )
        return candidates[:limit]
    finally:
        if close:
            conn.close()


def list_members(status="active", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name, m.status, cs.role, cs.exp_level, cs.trophies, "
            "cs.best_trophies, cs.clan_rank, cs.donations_week, cs.donations_received_week, cs.arena_name, "
            "md.note, md.profile_url, md.poap_address, dl.discord_user_id, dl.discord_username, dl.discord_display_name "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN member_metadata md ON md.member_id = m.member_id "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "WHERE m.status = ? "
            "ORDER BY COALESCE(cs.clan_rank, 999), m.current_name COLLATE NOCASE",
            (status,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["joined_date"] = _current_joined_at(conn, row["member_id"])
            item["in_discord"] = 1 if row["discord_user_id"] else 0
            result.append(_member_reference_fields(conn, row["member_id"], item))
        return result
    finally:
        if close:
            conn.close()


def get_clan_roster_summary(conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        from storage.war import get_current_war_status
        row = conn.execute(
            "SELECT COUNT(*) AS active_members, "
            "ROUND(AVG(COALESCE(cs.exp_level, 0)), 2) AS avg_exp_level, "
            "ROUND(AVG(COALESCE(cs.trophies, 0)), 2) AS avg_trophies, "
            "SUM(COALESCE(cs.donations_week, 0)) AS donations_week_total, "
            "MAX(COALESCE(cs.trophies, 0)) AS top_trophies "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchone()
        war = get_current_war_status(conn=conn)
        result = dict(row)
        result["open_slots"] = max(0, 50 - (result["active_members"] or 0))
        if war:
            result["current_war"] = war
        return result
    finally:
        if close:
            conn.close()


def get_member_profile(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT m.member_id, m.player_tag, m.current_name AS member_name, m.status, "
            "cs.observed_at, cs.role, cs.exp_level, cs.trophies, cs.best_trophies, cs.clan_rank, "
            "cs.previous_clan_rank, cs.donations_week, cs.donations_received_week, cs.arena_name, cs.last_seen_api, "
            "md.birth_month, md.birth_day, md.profile_url, md.poap_address, md.note, "
            "md.generated_bio AS bio, md.generated_highlight AS profile_highlight, md.generated_profile_updated_at, "
            "pp.fetched_at AS player_profile_at, pp.wins AS career_wins, pp.losses AS career_losses, "
            "pp.battle_count AS career_battle_count, pp.total_donations AS career_total_donations, "
            "pp.war_day_wins, pp.challenge_max_wins, pp.three_crown_wins, pp.current_favourite_card_name, "
            "dl.discord_user_id, dl.discord_username, dl.discord_display_name, du.last_seen_at AS discord_last_seen_at "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN member_metadata md ON md.member_id = m.member_id "
            "LEFT JOIN player_profile_snapshots pp ON pp.snapshot_id = ("
            "  SELECT p2.snapshot_id FROM player_profile_snapshots p2 "
            "  WHERE p2.member_id = m.member_id "
            "  ORDER BY p2.fetched_at DESC, p2.snapshot_id DESC LIMIT 1"
            ") "
            "LEFT JOIN discord_links dl ON dl.member_id = m.member_id AND dl.is_primary = 1 "
            "LEFT JOIN discord_users du ON du.discord_user_id = dl.discord_user_id "
            "WHERE m.player_tag = ?",
            (_canon_tag(tag),),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["joined_date"] = _current_joined_at(conn, row["member_id"])
        result["in_discord"] = 1 if row["discord_user_id"] else 0
        _member_reference_fields(conn, row["member_id"], result)
        recent_form = get_member_recent_form(tag, conn=conn)
        if recent_form:
            result["recent_form"] = recent_form
        deck = get_member_current_deck(tag, conn=conn)
        if deck:
            result["current_deck"] = deck
        cards = get_member_signature_cards(tag, conn=conn)
        if cards:
            result["signature_cards"] = cards
        return result
    finally:
        if close:
            conn.close()


def get_member_overview(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        from storage.war import get_member_war_status

        profile = get_member_profile(tag, conn=conn)
        if not profile:
            return None
        overview = dict(profile)
        overview["war_status"] = get_member_war_status(tag, conn=conn)
        return overview
    finally:
        if close:
            conn.close()


def list_longest_tenure_members(limit=10, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        today = datetime.now(timezone.utc).date()
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, cs.clan_rank "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchall()
        result = []
        for row in rows:
            joined_date = _current_joined_at(conn, row["member_id"])
            if not joined_date:
                continue
            joined_day = joined_date[:10]
            try:
                tenure_days = (today - datetime.strptime(joined_day, "%Y-%m-%d").date()).days
            except ValueError:
                tenure_days = None
            item = dict(row)
            item["joined_date"] = joined_day
            item["tenure_days"] = tenure_days
            result.append(_member_reference_fields(conn, row["member_id"], item))
        result.sort(
            key=lambda item: (
                item["joined_date"],
                (item.get("name") or "").lower(),
            )
        )
        return result[:limit]
    finally:
        if close:
            conn.close()


def list_recent_joins(days=30, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        from storage.war import get_current_season_id
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days))
        season_id = get_current_season_id(conn=conn)
        rows = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.exp_level, cs.trophies, cs.clan_rank "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active'"
        ).fetchall()
        result = []
        for row in rows:
            joined_date = _current_joined_at(conn, row["member_id"])
            if not joined_date:
                continue
            joined_day = joined_date[:10]
            try:
                joined_dt = datetime.strptime(joined_day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if joined_dt < cutoff:
                continue
            item = dict(row)
            item["joined_date"] = joined_day
            form = conn.execute(
                "SELECT wins, losses, sample_size, form_label FROM member_recent_form WHERE member_id = ? AND scope = 'competitive_10'",
                (row["member_id"],),
            ).fetchone()
            if form:
                item["recent_form"] = dict(form)
            if season_id is not None:
                war = conn.execute(
                    "SELECT COUNT(*) AS races_played, SUM(COALESCE(wp.fame, 0)) AS total_fame "
                    "FROM war_participation wp JOIN war_races wr ON wr.war_race_id = wp.war_race_id "
                    "WHERE wr.season_id = ? AND wp.member_id = ?",
                    (season_id, row["member_id"]),
                ).fetchone()
                item["current_season_war"] = dict(war)
            result.append(_member_reference_fields(conn, row["member_id"], item))
        result.sort(
            key=lambda item: (
                item["joined_date"],
                (item.get("name") or "").lower(),
            ),
            reverse=True,
        )
        return result
    finally:
        if close:
            conn.close()


def get_member_current_deck(tag, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT p.current_deck_json, p.current_deck_support_cards_json, p.fetched_at "
            "FROM player_profile_snapshots p "
            "JOIN members m ON m.member_id = p.member_id "
            "WHERE m.player_tag = ? "
            "ORDER BY p.fetched_at DESC LIMIT 1",
            (_canon_tag(tag),),
        ).fetchone()
        if not row or not row["current_deck_json"]:
            return None
        cards = []
        for raw_card in json.loads(row["current_deck_json"]):
            card = dict(raw_card)
            display_level = _card_level(card)
            if display_level is not None:
                card["api_level"] = card.get("level")
                card["level"] = display_level
            cards.append(card)
        support_cards = []
        for raw_card in json.loads(row["current_deck_support_cards_json"] or "[]"):
            card = dict(raw_card)
            display_level = _card_level(card)
            if display_level is not None:
                card["api_level"] = card.get("level")
                card["level"] = display_level
            support_cards.append(card)
        return {
            "fetched_at": row["fetched_at"],
            "cards": cards,
            "support_cards": support_cards,
        }
    finally:
        if close:
            conn.close()


def get_member_signature_cards(tag, mode_scope="overall", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT cards_json, sample_battles, fetched_at FROM member_card_usage_snapshots s "
            "JOIN members m ON m.member_id = s.member_id "
            "WHERE m.player_tag = ? AND s.mode_scope = ? "
            "ORDER BY s.fetched_at DESC LIMIT 1",
            (_canon_tag(tag), mode_scope),
        ).fetchone()
        if not row:
            return None
        return {
            "mode_scope": mode_scope,
            "sample_battles": row["sample_battles"],
            "fetched_at": row["fetched_at"],
            "cards": json.loads(row["cards_json"]),
        }
    finally:
        if close:
            conn.close()


def get_member_recent_form(tag, scope="competitive_10", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT f.scope, f.sample_size, f.wins, f.losses, f.draws, f.current_streak, "
            "f.current_streak_type, f.win_rate, f.avg_crown_diff, f.avg_trophy_change, f.form_label, f.summary, f.computed_at "
            "FROM member_recent_form f "
            "JOIN members m ON m.member_id = f.member_id "
            "WHERE m.player_tag = ? AND f.scope = ?",
            (_canon_tag(tag), scope),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def get_members_on_losing_streak(min_streak=3, scope="competitive_10", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, cs.clan_rank, cs.role, "
            "f.current_streak, f.current_streak_type, f.wins, f.losses, f.sample_size, f.form_label, f.summary "
            "FROM member_recent_form f "
            "JOIN members m ON m.member_id = f.member_id "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' AND f.scope = ? AND f.current_streak_type = 'L' AND f.current_streak >= ? "
            "ORDER BY f.current_streak DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE",
            (scope, min_streak),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            tag = item.get("tag")
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(tag),),
            ).fetchone()["member_id"]
            result.append(_member_reference_fields(conn, member_id, item))
        return result
    finally:
        if close:
            conn.close()


def get_members_on_hot_streak(min_streak=4, scope="ladder_ranked_10", conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT m.player_tag AS tag, m.current_name AS name, cs.clan_rank, cs.role, "
            "f.current_streak, f.current_streak_type, f.wins, f.losses, f.draws, f.sample_size, "
            "f.form_label, f.summary, f.avg_trophy_change "
            "FROM member_recent_form f "
            "JOIN members m ON m.member_id = f.member_id "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' AND f.scope = ? AND f.current_streak_type = 'W' AND f.current_streak >= ? "
            "ORDER BY f.current_streak DESC, COALESCE(f.avg_trophy_change, 0) DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE",
            (scope, min_streak),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            member_id = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(item["tag"]),),
            ).fetchone()["member_id"]
            result.append(_member_reference_fields(conn, member_id, item))
        return result
    finally:
        if close:
            conn.close()


def get_weekly_digest_summary(days=7, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        from storage.war import get_current_season_id
        from storage.war_analytics import get_trending_war_contributors, get_war_score_trend
        from storage.war_status import get_trophy_changes, get_war_season_summary

        roster = get_clan_roster_summary(conn=conn)
        season_id = get_current_season_id(conn=conn)
        cutoff_ts = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        cutoff_race = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S.000Z")

        top_donors = conn.execute(
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.clan_rank, cs.donations_week "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "WHERE m.status = 'active' AND COALESCE(cs.donations_week, 0) > 0 "
            "ORDER BY COALESCE(cs.donations_week, 0) DESC, cs.clan_rank ASC, m.current_name COLLATE NOCASE "
            "LIMIT 5"
        ).fetchall()
        donors = [_member_reference_fields(conn, row["member_id"], dict(row)) for row in top_donors]

        race_rows = conn.execute(
            "SELECT war_race_id, season_id, section_index, our_rank, trophy_change, our_fame, total_clans, finish_time, created_date, raw_json "
            "FROM war_races WHERE created_date >= ? "
            "ORDER BY created_date DESC LIMIT 3",
            (cutoff_race,),
        ).fetchall()
        recent_war_races = []
        for row in race_rows:
            payload = json.loads(row["raw_json"] or "{}")
            standings = []
            for standing in (payload.get("standings") or [])[:3]:
                clan = standing.get("clan") or {}
                standings.append({
                    "rank": standing.get("rank"),
                    "name": clan.get("name"),
                    "tag": clan.get("tag"),
                    "fame": clan.get("fame"),
                    "trophy_change": standing.get("trophyChange"),
                })
            top_participants = conn.execute(
                "SELECT wp.member_id, wp.player_tag AS tag, wp.player_name AS name, wp.fame, wp.repair_points, wp.decks_used "
                "FROM war_participation wp "
                "WHERE wp.war_race_id = ? "
                "ORDER BY COALESCE(wp.fame, 0) DESC, COALESCE(wp.decks_used, 0) DESC, wp.player_name COLLATE NOCASE "
                "LIMIT 3",
                (row["war_race_id"],),
            ).fetchall()
            participants = []
            for participant in top_participants:
                item = dict(participant)
                member_id = item.pop("member_id", None)
                if member_id:
                    participants.append(_member_reference_fields(conn, member_id, item))
                else:
                    participants.append(item)
            recent_war_races.append({
                "season_id": row["season_id"],
                "week": (row["section_index"] + 1) if row["section_index"] is not None else None,
                "section_index": row["section_index"],
                "created_date": row["created_date"],
                "our_rank": row["our_rank"],
                "trophy_change": row["trophy_change"],
                "our_fame": row["our_fame"],
                "total_clans": row["total_clans"],
                "finish_time": row["finish_time"],
                "top_participants": participants,
                "standings_preview": standings,
            })

        trophy_changes = get_trophy_changes(since_hours=max(24, days * 24), conn=conn)
        trophy_risers = [item for item in trophy_changes if (item.get("change") or 0) > 0][:5]
        trophy_drops = [item for item in trophy_changes if (item.get("change") or 0) < 0][:3]

        progression = []
        active_members = conn.execute(
            "SELECT member_id, player_tag AS tag, current_name AS name FROM members WHERE status = 'active'"
        ).fetchall()
        for row in active_members:
            snapshots = conn.execute(
                "SELECT fetched_at, exp_level, wins, trophies, best_trophies, current_favourite_card_name, current_path_of_legend_season_result_json "
                "FROM player_profile_snapshots WHERE member_id = ? AND fetched_at >= ? "
                "ORDER BY fetched_at ASC, snapshot_id ASC",
                (row["member_id"], cutoff_ts),
            ).fetchall()
            if len(snapshots) < 2:
                continue
            first = snapshots[0]
            latest = snapshots[-1]
            first_pol = json.loads(first["current_path_of_legend_season_result_json"] or "{}")
            latest_pol = json.loads(latest["current_path_of_legend_season_result_json"] or "{}")
            item = {
                "tag": row["tag"],
                "name": row["name"],
                "level_gain": (latest["exp_level"] or 0) - (first["exp_level"] or 0) if latest["exp_level"] is not None and first["exp_level"] is not None else 0,
                "wins_gain": (latest["wins"] or 0) - (first["wins"] or 0) if latest["wins"] is not None and first["wins"] is not None else 0,
                "trophies_change": (latest["trophies"] or 0) - (first["trophies"] or 0) if latest["trophies"] is not None and first["trophies"] is not None else 0,
                "best_trophies_gain": (latest["best_trophies"] or 0) - (first["best_trophies"] or 0) if latest["best_trophies"] is not None and first["best_trophies"] is not None else 0,
                "pol_league_gain": (latest_pol.get("leagueNumber") or 0) - (first_pol.get("leagueNumber") or 0),
                "pol_trophies_change": (latest_pol.get("trophies") or 0) - (first_pol.get("trophies") or 0),
                "favorite_card": latest["current_favourite_card_name"],
            }
            if any(
                item[key]
                for key in ("level_gain", "wins_gain", "trophies_change", "best_trophies_gain", "pol_league_gain", "pol_trophies_change")
            ):
                progression.append(_member_reference_fields(conn, row["member_id"], item))
        progression.sort(
            key=lambda item: (
                -(item.get("pol_league_gain") or 0),
                -(item.get("level_gain") or 0),
                -(item.get("best_trophies_gain") or 0),
                -(item.get("trophies_change") or 0),
                -(item.get("wins_gain") or 0),
                (item.get("name") or "").lower(),
            )
        )

        recent_joins = list_recent_joins(days=days, conn=conn)[:5]
        hot_streaks = get_members_on_hot_streak(min_streak=4, conn=conn)[:5]
        war_score_trend = get_war_score_trend(days=days, conn=conn)
        season_summary = get_war_season_summary(season_id=season_id, top_n=5, conn=conn) if season_id is not None else None
        recent_race_count = len(recent_war_races)
        trending_war = get_trending_war_contributors(
            season_id=season_id,
            recent_races=max(1, min(3, recent_race_count)) if recent_race_count else 1,
            limit=5,
            conn=conn,
        ) if season_id is not None else {"members": []}

        return {
            "window_days": days,
            "roster": roster,
            "season_id": season_id,
            "top_donors": donors,
            "recent_war_races": recent_war_races,
            "war_score_trend": war_score_trend,
            "war_season_summary": season_summary,
            "trending_war_contributors": trending_war,
            "trophy_risers": trophy_risers,
            "trophy_drops": trophy_drops,
            "progression_highlights": progression[:8],
            "hot_streaks": hot_streaks,
            "recent_joins": recent_joins,
        }
    finally:
        if close:
            conn.close()
