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
            "SELECT exp_level, cards_json FROM player_profile_snapshots WHERE member_id = ? ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
            (member_id,),
        ).fetchone()
        fetched_at = _utcnow()
        current_deck = player_data.get("currentDeck") or []
        cards = player_data.get("cards") or []
        favourite = player_data.get("currentFavouriteCard") or {}
        conn.execute(
            "INSERT INTO player_profile_snapshots (member_id, fetched_at, exp_level, trophies, best_trophies, wins, losses, battle_count, total_donations, donations, donations_received, war_day_wins, challenge_max_wins, challenge_cards_won, tournament_battle_count, tournament_cards_won, three_crown_wins, current_favourite_card_id, current_favourite_card_name, league_statistics_json, current_deck_json, cards_json, badges_json, achievements_json, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member_id, fetched_at, player_data.get("expLevel"), player_data.get("trophies"), player_data.get("bestTrophies"), player_data.get("wins"), player_data.get("losses"), player_data.get("battleCount"), player_data.get("totalDonations"), player_data.get("donations"), player_data.get("donationsReceived"), player_data.get("warDayWins"), player_data.get("challengeMaxWins"), player_data.get("challengeCardsWon"), player_data.get("tournamentBattleCount"), player_data.get("tournamentCardsWon"), player_data.get("threeCrownWins"), favourite.get("id"), favourite.get("name"), _json_or_none(player_data.get("leagueStatistics")), _json_or_none(current_deck), _json_or_none(cards), _json_or_none(player_data.get("badges") or []), _json_or_none(player_data.get("achievements") or []), _json_or_none(player_data)
            ),
        )
        conn.execute(
            "INSERT INTO member_card_collection_snapshots (member_id, fetched_at, cards_json) VALUES (?, ?, ?)",
            (member_id, fetched_at, _json_or_none(cards) or "[]"),
        )
        deck_hash = _hash_payload(current_deck) if current_deck else None
        conn.execute(
            "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, sample_size) VALUES (?, ?, 'player_profile', 'overall', ?, ?, 1)",
            (member_id, fetched_at, deck_hash, _json_or_none(current_deck) or "[]"),
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

        previous_cards = {}
        if previous and previous["cards_json"]:
            for card in json.loads(previous["cards_json"] or "[]"):
                if card.get("name"):
                    previous_cards[card["name"]] = _card_level(card)
        milestones = (14, 15, 16)
        for card in cards:
            name = card.get("name")
            if not name:
                continue
            old_card_level = previous_cards.get(name)
            new_card_level = _card_level(card)
            if old_card_level is None or new_card_level is None or new_card_level <= old_card_level:
                continue
            for milestone in milestones:
                if old_card_level < milestone <= new_card_level:
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


def snapshot_player_battlelog(player_tag, battle_log, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        tag = _canon_tag(player_tag)
        member_id = _ensure_member(conn, tag, status=None)
        _store_raw_payload(conn, "player_battlelog", _tag_key(tag), battle_log)
        for battle in battle_log or []:
            team = (battle.get("team") or [{}])[0]
            opp = (battle.get("opponent") or [{}])[0]
            if not team:
                continue
            crowns_for = team.get("crowns")
            crowns_against = opp.get("crowns") if opp else None
            if crowns_for is None or crowns_against is None:
                outcome = None
            elif crowns_for > crowns_against:
                outcome = "W"
            elif crowns_for < crowns_against:
                outcome = "L"
            else:
                outcome = "D"
            arena = battle.get("arena") or {}
            classified = _classify_battle(battle)
            conn.execute(
                "INSERT OR IGNORE INTO member_battle_facts (member_id, battle_time, battle_type, game_mode_name, game_mode_id, deck_selection, arena_id, arena_name, crowns_for, crowns_against, outcome, trophy_change, starting_trophies, is_competitive, is_ladder, is_ranked, is_war, is_special_event, deck_json, support_cards_json, opponent_name, opponent_tag, opponent_clan_tag, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    _json_or_none(battle),
                ),
            )
        recent_rows = conn.execute(
            "SELECT deck_json, battle_time FROM member_battle_facts WHERE member_id = ? AND is_competitive = 1 ORDER BY battle_time DESC LIMIT 30",
            (member_id,),
        ).fetchall()
        sample_battles, card_usage = _aggregate_card_usage_from_battle_facts(recent_rows)
        conn.execute(
            "INSERT INTO member_card_usage_snapshots (member_id, fetched_at, source, mode_scope, sample_battles, cards_json) VALUES (?, ?, 'battle_log', 'overall', ?, ?)",
            (member_id, _utcnow(), sample_battles, _json_or_none(card_usage) or "[]"),
        )
        if recent_rows:
            latest_cards = json.loads(recent_rows[0]["deck_json"] or "[]")
            if latest_cards:
                conn.execute(
                    "INSERT INTO member_deck_snapshots (member_id, fetched_at, source, mode_scope, deck_hash, deck_json, sample_size) VALUES (?, ?, 'battle_log', 'recent', ?, ?, ?)",
                    (member_id, _utcnow(), _hash_payload(latest_cards), _json_or_none(latest_cards) or "[]", len(recent_rows)),
                )
        _recompute_member_recent_form(member_id, conn=conn)
        conn.commit()
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

