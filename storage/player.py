import json
from datetime import datetime, timedelta, timezone

from db import (
    _aggregate_card_usage_from_battle_facts,
    _build_form_label,
    _build_form_summary,
    _canon_tag,
    _card_level,
    _ensure_member,
    _hash_payload,
    _json_or_none,
    _member_reference_fields,
    _store_raw_payload,
    _tag_key,
    _utcnow,
    get_connection,
)

def snapshot_player_profile(player_data, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        tag = _canon_tag(player_data.get("tag"))
        member_id = _ensure_member(conn, tag, player_data.get("name"), status=None)
        previous = conn.execute(
            "SELECT exp_level, wins, cards_json, current_path_of_legend_season_result_json "
            "FROM player_profile_snapshots WHERE member_id = ? "
            "ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
            (member_id,),
        ).fetchone()
        fetched_at = _utcnow()
        current_deck = player_data.get("currentDeck") or []
        current_deck_support_cards = player_data.get("currentDeckSupportCards") or []
        cards = player_data.get("cards") or []
        support_cards = player_data.get("supportCards") or []
        favourite = player_data.get("currentFavouriteCard") or {}
        conn.execute(
            "INSERT INTO player_profile_snapshots (member_id, fetched_at, exp_level, exp_points, total_exp_points, star_points, trophies, best_trophies, wins, losses, battle_count, total_donations, donations, donations_received, war_day_wins, challenge_max_wins, challenge_cards_won, tournament_battle_count, tournament_cards_won, three_crown_wins, clan_cards_collected, current_favourite_card_id, current_favourite_card_name, league_statistics_json, current_deck_json, current_deck_support_cards_json, cards_json, support_cards_json, badges_json, achievements_json, current_path_of_legend_season_result_json, last_path_of_legend_season_result_json, best_path_of_legend_season_result_json, legacy_trophy_road_high_score, progress_json, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member_id, fetched_at, player_data.get("expLevel"), player_data.get("expPoints"), player_data.get("totalExpPoints"), player_data.get("starPoints"), player_data.get("trophies"), player_data.get("bestTrophies"), player_data.get("wins"), player_data.get("losses"), player_data.get("battleCount"), player_data.get("totalDonations"), player_data.get("donations"), player_data.get("donationsReceived"), player_data.get("warDayWins"), player_data.get("challengeMaxWins"), player_data.get("challengeCardsWon"), player_data.get("tournamentBattleCount"), player_data.get("tournamentCardsWon"), player_data.get("threeCrownWins"), player_data.get("clanCardsCollected"), favourite.get("id"), favourite.get("name"), _json_or_none(player_data.get("leagueStatistics")), _json_or_none(current_deck), _json_or_none(current_deck_support_cards), _json_or_none(cards), _json_or_none(support_cards), _json_or_none(player_data.get("badges") or []), _json_or_none(player_data.get("achievements") or []), _json_or_none(player_data.get("currentPathOfLegendSeasonResult")), _json_or_none(player_data.get("lastPathOfLegendSeasonResult")), _json_or_none(player_data.get("bestPathOfLegendSeasonResult")), player_data.get("legacyTrophyRoadHighScore"), _json_or_none(player_data.get("progress")), _json_or_none(player_data)
            ),
        )
        conn.execute(
            "INSERT INTO member_card_collection_snapshots (member_id, fetched_at, cards_json, support_cards_json) VALUES (?, ?, ?, ?)",
            (member_id, fetched_at, _json_or_none(cards) or "[]", _json_or_none(support_cards) or "[]"),
        )
        deck_payload = {
            "cards": current_deck,
            "support_cards": current_deck_support_cards,
        }
        deck_hash = _hash_payload(deck_payload) if (current_deck or current_deck_support_cards) else None
        conn.execute(
            "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, support_cards_json, sample_size) VALUES (?, ?, 'player_profile', 'overall', ?, ?, ?, 1)",
            (member_id, fetched_at, deck_hash, _json_or_none(current_deck) or "[]", _json_or_none(current_deck_support_cards) or "[]"),
        )
        _store_raw_payload(conn, "player", _tag_key(tag), player_data)
        conn.commit()
        signals = []
        old_level = previous["exp_level"] if previous else None
        new_level = player_data.get("expLevel")
        if isinstance(old_level, int) and isinstance(new_level, int) and new_level > old_level:
            signals.append({
                "type": "player_level_up",
                "tag": tag,
                "name": player_data.get("name"),
                "old_level": old_level,
                "new_level": new_level,
            })

        old_wins = previous["wins"] if previous else None
        new_wins = player_data.get("wins")
        if isinstance(old_wins, int) and isinstance(new_wins, int) and new_wins > old_wins:
            first_milestone = ((old_wins // 500) + 1) * 500
            for milestone in range(first_milestone, new_wins + 1, 500):
                signals.append({
                    "type": "career_wins_milestone",
                    "tag": tag,
                    "name": player_data.get("name"),
                    "old_wins": old_wins,
                    "new_wins": new_wins,
                    "milestone": milestone,
                })

        old_pol = {}
        if previous and previous["current_path_of_legend_season_result_json"]:
            old_pol = json.loads(previous["current_path_of_legend_season_result_json"] or "{}")
        new_pol = player_data.get("currentPathOfLegendSeasonResult") or {}
        old_league = old_pol.get("leagueNumber")
        new_league = new_pol.get("leagueNumber")
        if isinstance(old_league, int) and isinstance(new_league, int) and new_league > old_league:
            signals.append({
                "type": "path_of_legend_promotion",
                "tag": tag,
                "name": player_data.get("name"),
                "old_league_number": old_league,
                "new_league_number": new_league,
                "trophies": new_pol.get("trophies"),
                "rank": new_pol.get("rank"),
            })

        previous_cards = {}
        if previous and previous["cards_json"]:
            for card in json.loads(previous["cards_json"] or "[]"):
                if card.get("name"):
                    previous_cards[card["name"]] = _card_level(card)
        for card in cards:
            name = card.get("name")
            if not name:
                continue
            old_card_level = previous_cards.get(name)
            new_card_level = _card_level(card)
            if old_card_level is None:
                signals.append({
                    "type": "new_card_unlocked",
                    "tag": tag,
                    "name": player_data.get("name"),
                    "card_name": name,
                    "new_level": new_card_level,
                })
                continue
            if new_card_level is None or new_card_level <= old_card_level:
                continue
            for milestone in range(old_card_level + 1, new_card_level + 1):
                signals.append({
                    "type": "card_level_milestone",
                    "tag": tag,
                    "name": player_data.get("name"),
                    "card_name": name,
                    "old_level": old_card_level,
                    "new_level": new_card_level,
                    "milestone": milestone,
                })
        return signals
    finally:
        if close:
            conn.close()


def get_player_intel_refresh_targets(limit=12, stale_after_hours=6, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        stale_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=stale_after_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "WITH latest_profiles AS ("
            "  SELECT member_id, MAX(fetched_at) AS last_profile_at FROM player_profile_snapshots GROUP BY member_id"
            "), latest_battles AS ("
            "  SELECT member_id, MAX(battle_time) AS last_battle_at FROM member_battle_facts GROUP BY member_id"
            ") "
            "SELECT m.member_id, m.player_tag AS tag, m.current_name AS name, cs.role, cs.clan_rank, "
            "lp.last_profile_at, lb.last_battle_at "
            "FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN latest_profiles lp ON lp.member_id = m.member_id "
            "LEFT JOIN latest_battles lb ON lb.member_id = m.member_id "
            "WHERE m.status = 'active' "
            "ORDER BY "
            "CASE cs.role WHEN 'leader' THEN 0 WHEN 'coLeader' THEN 1 WHEN 'elder' THEN 2 ELSE 3 END, "
            "CASE WHEN lp.last_profile_at IS NULL OR lp.last_profile_at < ? THEN 0 ELSE 1 END, "
            "CASE WHEN lb.last_battle_at IS NULL OR lb.last_battle_at < ? THEN 0 ELSE 1 END, "
            "COALESCE(lp.last_profile_at, '') ASC, "
            "COALESCE(lb.last_battle_at, '') ASC, "
            "COALESCE(cs.clan_rank, 999) ASC, "
            "m.current_name COLLATE NOCASE",
            (stale_cutoff, stale_cutoff),
        ).fetchall()
        targets = []
        for row in rows:
            item = dict(row)
            item["needs_profile_refresh"] = item["last_profile_at"] is None or item["last_profile_at"] < stale_cutoff
            item["needs_battle_refresh"] = item["last_battle_at"] is None or item["last_battle_at"] < stale_cutoff
            if not item["needs_profile_refresh"] and not item["needs_battle_refresh"]:
                continue
            targets.append(_member_reference_fields(conn, row["member_id"], item))
        return targets[:limit]
    finally:
        if close:
            conn.close()


def _classify_battle(battle: dict) -> dict:
    battle_type = battle.get("type") or ""
    mode_name = (battle.get("gameMode") or {}).get("name") or ""
    is_war = battle_type in {"riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum", "boatBattle"}
    is_ladder = mode_name == "Ladder" or battle_type == "PvP"
    is_ranked = battle_type == "pathOfLegend" or "Ranked" in mode_name
    is_competitive = battle_type in {"PvP", "pathOfLegend", "trail", "riverRacePvP", "riverRaceDuel", "riverRaceDuelColosseum"}
    is_special_event = battle_type == "trail"
    return {
        "is_war": int(is_war),
        "is_ladder": int(is_ladder),
        "is_ranked": int(is_ranked),
        "is_competitive": int(is_competitive),
        "is_special_event": int(is_special_event),
    }


def _resolve_battle_outcome(battle: dict, team: dict, opp: dict | None) -> str | None:
    boat_battle_won = battle.get("boatBattleWon")
    if isinstance(boat_battle_won, bool):
        return "W" if boat_battle_won else "L"

    trophy_change = team.get("trophyChange") if team else None
    if isinstance(trophy_change, (int, float)):
        if trophy_change > 0:
            return "W"
        if trophy_change < 0:
            return "L"
        return "D"

    crowns_for = team.get("crowns") if team else None
    crowns_against = opp.get("crowns") if opp else None
    if crowns_for is None or crowns_against is None:
        return None
    if crowns_for > crowns_against:
        return "W"
    if crowns_for < crowns_against:
        return "L"
    return "D"


def _latest_ladder_ranked_battle_time(member_id: int, conn=None):
    row = conn.execute(
        "SELECT MAX(battle_time) AS battle_time "
        "FROM member_battle_facts WHERE member_id = ? AND (is_ladder = 1 OR is_ranked = 1)",
        (member_id,),
    ).fetchone()
    return row["battle_time"] if row else None


def _current_ladder_ranked_streak(member_id: int, conn=None) -> dict:
    rows = conn.execute(
        "SELECT outcome FROM member_battle_facts "
        "WHERE member_id = ? AND (is_ladder = 1 OR is_ranked = 1) "
        "ORDER BY battle_time DESC LIMIT 20",
        (member_id,),
    ).fetchall()
    streak_type = rows[0]["outcome"] if rows and rows[0]["outcome"] else None
    current_streak = 0
    for row in rows:
        if streak_type and row["outcome"] == streak_type:
            current_streak += 1
        else:
            break
    return {
        "current_streak_type": streak_type,
        "current_streak": current_streak,
    }


def _detect_battle_pulse_signals(member_id: int, tag: str, name: str | None, previous_streak: dict, previous_latest_battle_time: str | None, conn=None) -> list[dict]:
    if not previous_latest_battle_time:
        return []

    current_form = conn.execute(
        "SELECT wins, losses, draws, sample_size, current_streak, current_streak_type, "
        "win_rate, avg_trophy_change, form_label, summary "
        "FROM member_recent_form WHERE member_id = ? AND scope = 'ladder_ranked_10'",
        (member_id,),
    ).fetchone()
    new_rows = conn.execute(
        "SELECT battle_time, battle_type, game_mode_name, outcome, trophy_change, starting_trophies, is_ranked "
        "FROM member_battle_facts "
        "WHERE member_id = ? AND (is_ladder = 1 OR is_ranked = 1) AND battle_time > ? "
        "ORDER BY battle_time DESC",
        (member_id, previous_latest_battle_time),
    ).fetchall()
    if not new_rows:
        return []

    signals = []
    current_streak = dict(current_form) if current_form else _current_ladder_ranked_streak(member_id, conn=conn)
    previous_count = int(previous_streak.get("current_streak") or 0)
    previous_type = previous_streak.get("current_streak_type")
    if (
        current_streak.get("current_streak_type") == "W"
        and int(current_streak.get("current_streak") or 0) >= 4
        and not (previous_type == "W" and previous_count >= 4)
    ):
        signals.append({
            "type": "battle_hot_streak",
            "tag": tag,
            "name": name,
            "streak": int(current_streak.get("current_streak") or 0),
            "sample_size": current_streak.get("sample_size"),
            "wins": current_streak.get("wins"),
            "losses": current_streak.get("losses"),
            "draws": current_streak.get("draws"),
            "win_rate": current_streak.get("win_rate"),
            "avg_trophy_change": current_streak.get("avg_trophy_change"),
            "form_label": current_streak.get("form_label"),
            "summary": current_streak.get("summary"),
            "new_battle_count": len(new_rows),
            "latest_battle_type": new_rows[0]["battle_type"],
            "latest_mode_name": new_rows[0]["game_mode_name"],
        })

    trophy_rows = [row for row in new_rows if row["trophy_change"] is not None and row["starting_trophies"] is not None]
    trophy_delta = int(sum((row["trophy_change"] or 0) for row in trophy_rows))
    if len(trophy_rows) >= 3 and trophy_delta >= 100:
        chronological = list(reversed(trophy_rows))
        from_trophies = chronological[0]["starting_trophies"]
        newest = trophy_rows[0]
        to_trophies = int(newest["starting_trophies"] + newest["trophy_change"])
        wins = sum(1 for row in new_rows if row["outcome"] == "W")
        losses = sum(1 for row in new_rows if row["outcome"] == "L")
        draws = sum(1 for row in new_rows if row["outcome"] == "D")
        signals.append({
            "type": "battle_trophy_push",
            "tag": tag,
            "name": name,
            "battle_count": len(trophy_rows),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "trophy_delta": trophy_delta,
            "from_trophies": from_trophies,
            "to_trophies": to_trophies,
            "ranked_battle_count": sum(1 for row in trophy_rows if row["is_ranked"]),
            "latest_battle_type": new_rows[0]["battle_type"],
            "latest_mode_name": new_rows[0]["game_mode_name"],
        })
    return signals


def snapshot_player_battlelog(player_tag, battle_log, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        tag = _canon_tag(player_tag)
        member_id = _ensure_member(conn, tag, status=None)
        previous_streak = _current_ladder_ranked_streak(member_id, conn=conn)
        previous_latest_ladder_ranked_battle_time = _latest_ladder_ranked_battle_time(member_id, conn=conn)
        _store_raw_payload(conn, "player_battlelog", _tag_key(tag), battle_log)
        latest_name = None
        for battle in battle_log or []:
            team = (battle.get("team") or [{}])[0]
            opp = (battle.get("opponent") or [{}])[0]
            if not team:
                continue
            latest_name = latest_name or team.get("name")
            crowns_for = team.get("crowns")
            crowns_against = opp.get("crowns") if opp else None
            outcome = _resolve_battle_outcome(battle, team, opp)
            arena = battle.get("arena") or {}
            classified = _classify_battle(battle)
            conn.execute(
                "INSERT OR IGNORE INTO member_battle_facts (member_id, battle_time, battle_type, game_mode_name, game_mode_id, deck_selection, arena_id, arena_name, crowns_for, crowns_against, outcome, trophy_change, starting_trophies, is_competitive, is_ladder, is_ranked, is_war, is_special_event, deck_json, support_cards_json, opponent_name, opponent_tag, opponent_clan_tag, event_tag, league_number, is_hosted_match, modifiers_json, team_rounds_json, opponent_rounds_json, boat_battle_side, boat_battle_won, new_towers_destroyed, prev_towers_destroyed, remaining_towers, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    member_id,
                    battle.get("battleTime"),
                    battle.get("type"),
                    (battle.get("gameMode") or {}).get("name"),
                    (battle.get("gameMode") or {}).get("id"),
                    battle.get("deckSelection"),
                    arena.get("id") if isinstance(arena, dict) else None,
                    arena.get("name") if isinstance(arena, dict) else str(arena or ""),
                    crowns_for,
                    crowns_against,
                    outcome,
                    team.get("trophyChange"),
                    team.get("startingTrophies"),
                    classified["is_competitive"],
                    classified["is_ladder"],
                    classified["is_ranked"],
                    classified["is_war"],
                    classified["is_special_event"],
                    _json_or_none(team.get("cards") or []),
                    _json_or_none(team.get("supportCards") or []),
                    opp.get("name") if opp else None,
                    _canon_tag(opp.get("tag")) if opp and opp.get("tag") else None,
                    _canon_tag((opp.get("clan") or {}).get("tag")) if opp else None,
                    battle.get("eventTag"),
                    battle.get("leagueNumber"),
                    int(bool(battle.get("isHostedMatch"))) if battle.get("isHostedMatch") is not None else None,
                    _json_or_none(battle.get("modifiers") or []),
                    _json_or_none(team.get("rounds") or []),
                    _json_or_none(opp.get("rounds") or []),
                    battle.get("boatBattleSide"),
                    int(bool(battle.get("boatBattleWon"))) if battle.get("boatBattleWon") is not None else None,
                    battle.get("newTowersDestroyed"),
                    battle.get("prevTowersDestroyed"),
                    battle.get("remainingTowers"),
                    _json_or_none(battle),
                ),
            )
        recent_rows = conn.execute(
            "SELECT deck_json, support_cards_json, battle_time FROM member_battle_facts WHERE member_id = ? AND is_competitive = 1 ORDER BY battle_time DESC LIMIT 30",
            (member_id,),
        ).fetchall()
        sample_battles, card_usage = _aggregate_card_usage_from_battle_facts(recent_rows)
        conn.execute(
            "INSERT INTO member_card_usage_snapshots (member_id, fetched_at, source, mode_scope, sample_battles, cards_json) VALUES (?, ?, 'battle_log', 'overall', ?, ?)",
            (member_id, _utcnow(), sample_battles, _json_or_none(card_usage) or "[]"),
        )
        if recent_rows:
            latest_cards = json.loads(recent_rows[0]["deck_json"] or "[]")
            latest_support_cards = json.loads(recent_rows[0]["support_cards_json"] or "[]")
            if latest_cards or latest_support_cards:
                deck_payload = {
                    "cards": latest_cards,
                    "support_cards": latest_support_cards,
                }
                conn.execute(
                    "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, support_cards_json, sample_size) VALUES (?, ?, 'battle_log', 'recent', ?, ?, ?, ?)",
                    (member_id, _utcnow(), _hash_payload(deck_payload), _json_or_none(latest_cards) or "[]", _json_or_none(latest_support_cards) or "[]", len(recent_rows)),
                )
        _recompute_member_recent_form(member_id, conn=conn)
        name = latest_name or conn.execute(
            "SELECT current_name FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()["current_name"]
        signals = _detect_battle_pulse_signals(
            member_id,
            tag,
            name,
            previous_streak,
            previous_latest_ladder_ranked_battle_time,
            conn=conn,
        )
        conn.commit()
        return signals
    finally:
        if close:
            conn.close()


def _recompute_member_recent_form(member_id: int, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        scopes = {
            "overall_10": "1=1",
            "competitive_10": "is_competitive = 1",
            "ladder_ranked_10": "(is_ladder = 1 OR is_ranked = 1)",
            "war_10": "is_war = 1",
        }
        for scope, predicate in scopes.items():
            rows = conn.execute(
                f"SELECT outcome, crowns_for, crowns_against, trophy_change FROM member_battle_facts WHERE member_id = ? AND {predicate} ORDER BY battle_time DESC LIMIT 10",
                (member_id,),
            ).fetchall()
            sample_size = len(rows)
            wins = sum(1 for r in rows if r["outcome"] == "W")
            losses = sum(1 for r in rows if r["outcome"] == "L")
            draws = sum(1 for r in rows if r["outcome"] == "D")
            streak_type = rows[0]["outcome"] if rows and rows[0]["outcome"] else None
            current_streak = 0
            for row in rows:
                if streak_type and row["outcome"] == streak_type:
                    current_streak += 1
                else:
                    break
            diffs = [(r["crowns_for"] or 0) - (r["crowns_against"] or 0) for r in rows if r["crowns_for"] is not None and r["crowns_against"] is not None]
            trophy_changes = [r["trophy_change"] for r in rows if r["trophy_change"] is not None]
            avg_crown_diff = round(sum(diffs) / len(diffs), 2) if diffs else None
            avg_trophy_change = round(sum(trophy_changes) / len(trophy_changes), 2) if trophy_changes else None
            label = _build_form_label(wins, losses, sample_size)
            summary = _build_form_summary(wins, losses, draws, sample_size, label)
            conn.execute(
                "INSERT INTO member_recent_form (member_id, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(member_id, scope) DO UPDATE SET computed_at = excluded.computed_at, sample_size = excluded.sample_size, wins = excluded.wins, losses = excluded.losses, draws = excluded.draws, current_streak = excluded.current_streak, current_streak_type = excluded.current_streak_type, win_rate = excluded.win_rate, avg_crown_diff = excluded.avg_crown_diff, avg_trophy_change = excluded.avg_trophy_change, form_label = excluded.form_label, summary = excluded.summary",
                (member_id, _utcnow(), scope, sample_size, wins, losses, draws, current_streak, streak_type, round(wins / sample_size, 4) if sample_size else 0, avg_crown_diff, avg_trophy_change, label, summary),
            )
        conn.commit()
    finally:
        if close:
            conn.close()
