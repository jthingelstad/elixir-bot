from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import (
    _aggregate_card_usage_from_battle_facts,
    _build_form_label,
    _build_form_summary,
    _canon_tag,
    _card_level,
    _rowdicts,
    _ensure_member,
    _hash_payload,
    _json_or_none,
    _member_reference_fields,
    _store_raw_payload,
    _tag_key,
    _upsert_member_metadata,
    _utcnow,
    chicago_today,
    chicago_date_for_cr_timestamp,
    chicago_day_bounds_utc,
    get_connection,
    managed_connection,
)

CARD_UPGRADE_SIGNAL_MIN_LEVEL = 15
MASTERY_BADGE_SIGNAL_MIN_LEVEL = 5
CARD_UNLOCK_SIGNAL_RARITIES = {"epic", "legendary", "champion"}
GAMES_PER_DAY_WINDOW_DAYS = 14
BADGE_NAME_OVERRIDES = {
    "Classic12Wins": "Classic Challenge 12 Wins",
    "Grand12Wins": "Grand Challenge 12 Wins",
    "2xElixir": "2x Elixir",
}


def _split_identifier_words(value: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value or "")
    text = text.replace("_", " ").strip()
    return re.sub(r"\s+", " ", text)


def _is_champion_card(card: dict) -> bool:
    rarity = str(card.get("rarity") or "").strip().lower()
    if rarity == "champion":
        return True
    max_level = card.get("maxLevel")
    return isinstance(max_level, int) and max_level == 6


def _badge_category(name: str | None) -> str:
    badge_name = str(name or "").strip()
    if not badge_name:
        return "general"
    if badge_name.startswith("Mastery"):
        return "mastery"
    if badge_name in {"Classic12Wins", "Grand12Wins"}:
        return "challenge"
    if badge_name in {"2v2", "RampUp", "SuddenDeath", "Draft", "2xElixir"}:
        return "mode"
    if badge_name in {"EmoteCollection", "BannerCollection", "CollectionLevel", "ClanDonations"}:
        return "collection"
    if badge_name.startswith("SeasonalBadge_") or badge_name.startswith("MergeTacticsBadge_"):
        return "seasonal"
    if badge_name.startswith("Crl") or badge_name in {"EasterEgg"}:
        return "event"
    if badge_name in {"YearsPlayed", "BattleWins", "ClanWarsVeteran", "LadderTop1000"}:
        return "career"
    return "general"


def _badge_label(name: str | None) -> str | None:
    badge_name = str(name or "").strip()
    if not badge_name:
        return None
    if badge_name in BADGE_NAME_OVERRIDES:
        return BADGE_NAME_OVERRIDES[badge_name]
    if badge_name.startswith("Mastery") and len(badge_name) > len("Mastery"):
        return f"{_split_identifier_words(badge_name[len('Mastery'):])} Mastery"
    return _split_identifier_words(badge_name)


def _badge_card_name(name: str | None) -> str | None:
    badge_name = str(name or "").strip()
    if not badge_name.startswith("Mastery") or len(badge_name) <= len("Mastery"):
        return None
    return _split_identifier_words(badge_name[len("Mastery"):])


def _badge_signal_fields(badge: dict | None) -> dict:
    badge = badge or {}
    name = badge.get("name")
    fields = {
        "badge_name": name,
        "badge_label": _badge_label(name),
        "badge_category": _badge_category(name),
        "badge_level": badge.get("level"),
        "badge_max_level": badge.get("maxLevel"),
        "progress": badge.get("progress"),
        "target": badge.get("target"),
        "is_one_time": badge.get("level") is None,
    }
    card_name = _badge_card_name(name)
    if card_name:
        fields["badge_card_name"] = card_name
    return fields


def _achievement_signal_fields(achievement: dict | None) -> dict:
    achievement = achievement or {}
    return {
        "achievement_name": achievement.get("name"),
        "achievement_stars": achievement.get("stars"),
        "achievement_value": achievement.get("value"),
        "achievement_target": achievement.get("target"),
        "achievement_info": achievement.get("info"),
        "completion_info": achievement.get("completionInfo"),
    }


def _indexed_items(items: list[dict] | None) -> dict[str, dict]:
    indexed = {}
    for item in items or []:
        name = str(item.get("name") or "").strip()
        if name:
            indexed[name] = item
    return indexed


def _years_played_metadata_fields(player_data: dict, *, fetched_at: str) -> dict:
    badges = player_data.get("badges") or []
    years_played = next((badge for badge in badges if badge.get("name") == "YearsPlayed"), None)
    if not years_played:
        return {
            "cr_account_age_days": None,
            "cr_account_age_years": None,
            "cr_account_age_updated_at": fetched_at,
        }
    age_days = years_played.get("progress")
    age_years = years_played.get("level")
    return {
        "cr_account_age_days": age_days if isinstance(age_days, int) and age_days >= 0 else None,
        "cr_account_age_years": age_years if isinstance(age_years, int) and age_years >= 0 else None,
        "cr_account_age_updated_at": fetched_at,
    }


def _games_per_day_metadata_fields(member_id: int, *, computed_at: str, conn) -> dict:
    cutoff = (datetime.fromisoformat(chicago_today()) - timedelta(days=max(GAMES_PER_DAY_WINDOW_DAYS - 1, 0))).date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(battles), 0) AS total_battles "
        "FROM member_daily_battle_rollups WHERE member_id = ? AND battle_date >= ?",
        (member_id, cutoff),
    ).fetchone()
    total_battles = int((row["total_battles"] or 0) if row else 0)
    return {
        "cr_games_per_day": round(total_battles / GAMES_PER_DAY_WINDOW_DAYS, 2),
        "cr_games_per_day_window_days": GAMES_PER_DAY_WINDOW_DAYS,
        "cr_games_per_day_updated_at": computed_at,
    }


def _card_display_max_level(card: dict) -> int | None:
    max_level = card.get("maxLevel")
    if not isinstance(max_level, int) or max_level <= 0:
        return None
    if max_level > 16:
        return max_level
    return max_level + max(0, 16 - max_level)


def _normalize_cards_for_storage(cards: list[dict] | None) -> list[dict]:
    normalized = []
    for raw_card in cards or []:
        if not isinstance(raw_card, dict):
            continue
        card = dict(raw_card)
        raw_level = card.get("level")
        raw_max_level = card.get("maxLevel")
        display_level = _card_level(card)
        display_max_level = _card_display_max_level(card)
        if isinstance(raw_level, int):
            card["api_level"] = raw_level
        if isinstance(raw_max_level, int):
            card["api_max_level"] = raw_max_level
        if display_level is not None:
            card["level"] = display_level
        if display_max_level is not None:
            card["maxLevel"] = display_max_level
        if isinstance(card.get("level"), int) and isinstance(card.get("maxLevel"), int):
            card["levels_to_max"] = max(0, card["maxLevel"] - card["level"])
            card["is_max_level"] = card["level"] >= card["maxLevel"]
        normalized.append(card)
    return normalized


@managed_connection
def snapshot_player_profile(player_data: dict, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    tag = _canon_tag(player_data.get("tag"))
    member_id = _ensure_member(conn, tag, player_data.get("name"), status=None)
    previous = conn.execute(
        "SELECT exp_level, wins, cards_json, badges_json, achievements_json, current_path_of_legend_season_result_json "
        "FROM player_profile_snapshots WHERE member_id = ? "
        "ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
        (member_id,),
    ).fetchone()
    fetched_at = _utcnow()
    current_deck = _normalize_cards_for_storage(player_data.get("currentDeck") or [])
    current_deck_support_cards = _normalize_cards_for_storage(player_data.get("currentDeckSupportCards") or [])
    cards = _normalize_cards_for_storage(player_data.get("cards") or [])
    support_cards = _normalize_cards_for_storage(player_data.get("supportCards") or [])
    favourite = player_data.get("currentFavouriteCard") or {}
    conn.execute(
        "INSERT INTO player_profile_snapshots (member_id, fetched_at, exp_level, exp_points, total_exp_points, star_points, trophies, best_trophies, wins, losses, battle_count, total_donations, donations, donations_received, war_day_wins, challenge_max_wins, challenge_cards_won, tournament_battle_count, tournament_cards_won, three_crown_wins, clan_cards_collected, current_favourite_card_id, current_favourite_card_name, league_statistics_json, current_deck_json, current_deck_support_cards_json, cards_json, support_cards_json, badges_json, achievements_json, current_path_of_legend_season_result_json, last_path_of_legend_season_result_json, best_path_of_legend_season_result_json, legacy_trophy_road_high_score, progress_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            member_id, fetched_at, player_data.get("expLevel"), player_data.get("expPoints"), player_data.get("totalExpPoints"), player_data.get("starPoints"), player_data.get("trophies"), player_data.get("bestTrophies"), player_data.get("wins"), player_data.get("losses"), player_data.get("battleCount"), player_data.get("totalDonations"), player_data.get("donations"), player_data.get("donationsReceived"), player_data.get("warDayWins"), player_data.get("challengeMaxWins"), player_data.get("challengeCardsWon"), player_data.get("tournamentBattleCount"), player_data.get("tournamentCardsWon"), player_data.get("threeCrownWins"), player_data.get("clanCardsCollected"), favourite.get("id"), favourite.get("name"), _json_or_none(player_data.get("leagueStatistics")), _json_or_none(current_deck), _json_or_none(current_deck_support_cards), _json_or_none(cards), _json_or_none(support_cards), _json_or_none(player_data.get("badges") or []), _json_or_none(player_data.get("achievements") or []), _json_or_none(player_data.get("currentPathOfLegendSeasonResult")), _json_or_none(player_data.get("lastPathOfLegendSeasonResult")), _json_or_none(player_data.get("bestPathOfLegendSeasonResult")), player_data.get("legacyTrophyRoadHighScore"), _json_or_none(player_data.get("progress"))
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
    _upsert_member_metadata(conn, member_id, **_years_played_metadata_fields(player_data, fetched_at=fetched_at))
    conn.commit()
    if previous is None:
        return []
    signals = []
    old_level = previous["exp_level"] if previous else None
    new_level = player_data.get("expLevel")
    if isinstance(old_level, int) and isinstance(new_level, int) and new_level > old_level:
        if new_level % 5 == 0 or (old_level // 5) < (new_level // 5):
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
        rarity = str(card.get("rarity") or "").strip().lower() or None
        is_champion = _is_champion_card(card)
        if old_card_level is None:
            if rarity in CARD_UNLOCK_SIGNAL_RARITIES or is_champion:
                signals.append({
                    "type": "new_card_unlocked",
                    "tag": tag,
                    "name": player_data.get("name"),
                    "card_name": name,
                    "rarity": rarity,
                    "is_champion": is_champion,
                    "new_level": new_card_level,
                })
            if is_champion:
                signals.append({
                    "type": "new_champion_unlocked",
                    "tag": tag,
                    "name": player_data.get("name"),
                    "card_name": name,
                    "rarity": rarity,
                    "is_champion": True,
                    "new_level": new_card_level,
                })
            continue
        if new_card_level is None or new_card_level <= old_card_level:
            continue
        for milestone in range(old_card_level + 1, new_card_level + 1):
            if milestone < CARD_UPGRADE_SIGNAL_MIN_LEVEL:
                continue
            signals.append({
                "type": "card_level_milestone",
                "tag": tag,
                "name": player_data.get("name"),
                "card_name": name,
                "old_level": old_card_level,
                "new_level": new_card_level,
                "milestone": milestone,
            })

    previous_badges = _indexed_items(json.loads(previous["badges_json"] or "[]")) if previous and previous["badges_json"] else {}
    current_badges = _indexed_items(player_data.get("badges") or [])
    for badge_name, badge in current_badges.items():
        previous_badge = previous_badges.get(badge_name)
        if previous_badge is None:
            if _badge_category(badge_name) != "mastery":
                signals.append({
                    "type": "badge_earned",
                    "tag": tag,
                    "name": player_data.get("name"),
                    **_badge_signal_fields(badge),
                })
            continue
        old_level = previous_badge.get("level")
        new_level = badge.get("level")
        if isinstance(old_level, int) and isinstance(new_level, int) and new_level > old_level:
            if _badge_category(badge_name) == "mastery" and new_level < MASTERY_BADGE_SIGNAL_MIN_LEVEL:
                continue
            signals.append({
                "type": "badge_level_milestone",
                "tag": tag,
                "name": player_data.get("name"),
                "old_level": old_level,
                "new_level": new_level,
                **_badge_signal_fields(badge),
            })

    previous_achievements = _indexed_items(json.loads(previous["achievements_json"] or "[]")) if previous and previous["achievements_json"] else {}
    current_achievements = _indexed_items(player_data.get("achievements") or [])
    for achievement_name, achievement in current_achievements.items():
        previous_achievement = previous_achievements.get(achievement_name) or {}
        old_stars = previous_achievement.get("stars")
        new_stars = achievement.get("stars")
        prior_stars = old_stars if isinstance(old_stars, int) else 0
        if isinstance(new_stars, int) and new_stars > prior_stars:
            signals.append({
                "type": "achievement_star_milestone",
                "tag": tag,
                "name": player_data.get("name"),
                "old_stars": prior_stars,
                "new_stars": new_stars,
                "completed": new_stars >= 3,
                **_achievement_signal_fields(achievement),
            })
    return signals


@managed_connection
def get_player_intel_refresh_targets(limit: int = 12, stale_after_hours: int = 6, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
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


def _battle_mode_group(*, is_war=0, is_ladder=0, is_ranked=0, is_special_event=0, is_hosted_match=None) -> str:
    if is_war:
        return "war"
    if is_ranked:
        return "ranked"
    if is_ladder:
        return "ladder"
    if is_special_event:
        return "special_event"
    if is_hosted_match:
        return "friendly"
    return "other"


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


def _expected_battle_delta_for_day(member_id: int, battle_date: str, conn=None):
    start_utc, end_utc = chicago_day_bounds_utc(battle_date)
    before_row = conn.execute(
        "SELECT battle_count FROM player_profile_snapshots "
        "WHERE member_id = ? AND fetched_at < ? AND battle_count IS NOT NULL "
        "ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
        (member_id, start_utc),
    ).fetchone()
    end_row = conn.execute(
        "SELECT battle_count FROM player_profile_snapshots "
        "WHERE member_id = ? AND fetched_at < ? AND battle_count IS NOT NULL "
        "ORDER BY fetched_at DESC, snapshot_id DESC LIMIT 1",
        (member_id, end_utc),
    ).fetchone()
    if not before_row or not end_row:
        return None
    before_count = before_row["battle_count"]
    end_count = end_row["battle_count"]
    if before_count is None or end_count is None:
        return None
    return max(0, int(end_count) - int(before_count))


def _recompute_member_daily_battle_rollups(member_id: int, battle_dates=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if battle_dates is None:
            rows = conn.execute(
                "SELECT DISTINCT battle_time FROM member_battle_facts WHERE member_id = ?",
                (member_id,),
            ).fetchall()
            battle_dates = sorted(
                {
                    chicago_date_for_cr_timestamp(row["battle_time"])
                    for row in rows
                    if chicago_date_for_cr_timestamp(row["battle_time"])
                }
            )
        else:
            battle_dates = sorted({date for date in (battle_dates or []) if date})

        for battle_date in battle_dates:
            start_utc, end_utc = chicago_day_bounds_utc(battle_date)
            rows = conn.execute(
                "SELECT battle_time, game_mode_id, game_mode_name, outcome, crowns_for, crowns_against, trophy_change, is_war, is_ladder, is_ranked, is_special_event, is_hosted_match "
                "FROM member_battle_facts "
                "WHERE member_id = ? AND REPLACE(REPLACE(battle_time, '.000Z', ''), 'Z', '') >= REPLACE(REPLACE(?, '.000Z', ''), 'Z', '') "
                "AND REPLACE(REPLACE(battle_time, '.000Z', ''), 'Z', '') < REPLACE(REPLACE(?, '.000Z', ''), 'Z', '')",
                (member_id, start_utc.replace("-", "").replace(":", "").replace("T", "T"), end_utc.replace("-", "").replace(":", "").replace("T", "T")),
            ).fetchall()
            conn.execute(
                "DELETE FROM member_daily_battle_rollups WHERE member_id = ? AND battle_date = ?",
                (member_id, battle_date),
            )
            if not rows:
                continue

            buckets = {}
            for row in rows:
                mode_group = _battle_mode_group(
                    is_war=row["is_war"],
                    is_ladder=row["is_ladder"],
                    is_ranked=row["is_ranked"],
                    is_special_event=row["is_special_event"],
                    is_hosted_match=row["is_hosted_match"],
                )
                key = (mode_group, row["game_mode_id"])
                bucket = buckets.setdefault(
                    key,
                    {
                        "mode_group": mode_group,
                        "game_mode_id": row["game_mode_id"],
                        "game_mode_name": row["game_mode_name"],
                        "battles": 0,
                        "wins": 0,
                        "losses": 0,
                        "draws": 0,
                        "crowns_for": 0,
                        "crowns_against": 0,
                        "trophy_change_total": 0,
                        "first_battle_at": None,
                        "last_battle_at": None,
                    },
                )
                bucket["battles"] += 1
                if row["outcome"] == "W":
                    bucket["wins"] += 1
                elif row["outcome"] == "L":
                    bucket["losses"] += 1
                elif row["outcome"] == "D":
                    bucket["draws"] += 1
                bucket["crowns_for"] += int(row["crowns_for"] or 0)
                bucket["crowns_against"] += int(row["crowns_against"] or 0)
                bucket["trophy_change_total"] += int(row["trophy_change"] or 0)
                if bucket["first_battle_at"] is None or row["battle_time"] < bucket["first_battle_at"]:
                    bucket["first_battle_at"] = row["battle_time"]
                if bucket["last_battle_at"] is None or row["battle_time"] > bucket["last_battle_at"]:
                    bucket["last_battle_at"] = row["battle_time"]

            expected_battle_delta = _expected_battle_delta_for_day(member_id, battle_date, conn=conn)
            captured_battles = len(rows)
            completeness_ratio = None
            is_complete = 0
            if expected_battle_delta is not None:
                denominator = max(expected_battle_delta, 1)
                completeness_ratio = min(1.0, round(captured_battles / denominator, 4))
                is_complete = 1 if captured_battles >= expected_battle_delta else 0

            aggregated_at = _utcnow()
            for bucket in buckets.values():
                conn.execute(
                    "INSERT INTO member_daily_battle_rollups (member_id, battle_date, mode_group, game_mode_id, game_mode_name, battles, wins, losses, draws, crowns_for, crowns_against, trophy_change_total, first_battle_at, last_battle_at, captured_battles, expected_battle_delta, completeness_ratio, is_complete, last_aggregated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        member_id,
                        battle_date,
                        bucket["mode_group"],
                        bucket["game_mode_id"],
                        bucket["game_mode_name"],
                        bucket["battles"],
                        bucket["wins"],
                        bucket["losses"],
                        bucket["draws"],
                        bucket["crowns_for"],
                        bucket["crowns_against"],
                        bucket["trophy_change_total"],
                        bucket["first_battle_at"],
                        bucket["last_battle_at"],
                        captured_battles,
                        expected_battle_delta,
                        completeness_ratio,
                        is_complete,
                        aggregated_at,
                    ),
                )
        if close:
            conn.commit()
    finally:
        if close:
            conn.close()


def _current_clan_identity_for_rollups(battle_date: str, conn=None) -> tuple[str, str]:
    row = conn.execute(
        "SELECT clan_tag, clan_name FROM clan_daily_metrics WHERE metric_date = ? ORDER BY observed_at DESC, metric_id DESC LIMIT 1",
        (battle_date,),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT clan_tag, clan_name FROM clan_daily_metrics ORDER BY metric_date DESC, observed_at DESC, metric_id DESC LIMIT 1"
        ).fetchone()
    if row:
        return row["clan_tag"], row["clan_name"] or "POAP KINGS"
    return "#J2RGCRVG", "POAP KINGS"


def _recompute_clan_daily_battle_rollups(battle_dates=None, conn=None):
    close = conn is None
    conn = conn or get_connection()
    try:
        if battle_dates is None:
            rows = conn.execute(
                "SELECT DISTINCT battle_date FROM member_daily_battle_rollups"
            ).fetchall()
            battle_dates = sorted(row["battle_date"] for row in rows if row["battle_date"])
        else:
            battle_dates = sorted({date for date in (battle_dates or []) if date})

        for battle_date in battle_dates:
            clan_tag, clan_name = _current_clan_identity_for_rollups(battle_date, conn=conn)
            member_rows = conn.execute(
                "SELECT member_id, mode_group, game_mode_id, game_mode_name, battles, wins, losses, draws, crowns_for, crowns_against, trophy_change_total, captured_battles, expected_battle_delta, completeness_ratio, is_complete "
                "FROM member_daily_battle_rollups "
                "WHERE battle_date = ?",
                (battle_date,),
            ).fetchall()
            conn.execute(
                "DELETE FROM clan_daily_battle_rollups WHERE clan_tag = ? AND battle_date = ?",
                (clan_tag, battle_date),
            )
            if not member_rows:
                continue

            member_day_summary = {}
            for row in member_rows:
                member_day_summary.setdefault(
                    row["member_id"],
                    {
                        "captured_battles": row["captured_battles"],
                        "expected_battle_delta": row["expected_battle_delta"],
                        "is_complete": row["is_complete"],
                    },
                )

            buckets = {}
            for row in member_rows:
                key = (row["mode_group"], row["game_mode_id"])
                bucket = buckets.setdefault(
                    key,
                    {
                        "mode_group": row["mode_group"],
                        "game_mode_id": row["game_mode_id"],
                        "game_mode_name": row["game_mode_name"],
                        "members": set(),
                        "battles": 0,
                        "wins": 0,
                        "losses": 0,
                        "draws": 0,
                        "crowns_for": 0,
                        "crowns_against": 0,
                        "trophy_change_total": 0,
                    },
                )
                bucket["members"].add(row["member_id"])
                bucket["battles"] += int(row["battles"] or 0)
                bucket["wins"] += int(row["wins"] or 0)
                bucket["losses"] += int(row["losses"] or 0)
                bucket["draws"] += int(row["draws"] or 0)
                bucket["crowns_for"] += int(row["crowns_for"] or 0)
                bucket["crowns_against"] += int(row["crowns_against"] or 0)
                bucket["trophy_change_total"] += int(row["trophy_change_total"] or 0)

            aggregated_at = _utcnow()
            for bucket in buckets.values():
                contributing = [member_day_summary[member_id] for member_id in bucket["members"]]
                expected_known = all(item.get("expected_battle_delta") is not None for item in contributing)
                captured_battles = None
                expected_battle_delta = None
                completeness_ratio = None
                is_complete = 0
                if expected_known:
                    captured_battles = sum(int(item.get("captured_battles") or 0) for item in contributing)
                    expected_battle_delta = sum(int(item.get("expected_battle_delta") or 0) for item in contributing)
                    denominator = max(expected_battle_delta, 1)
                    completeness_ratio = min(1.0, round(captured_battles / denominator, 4))
                    is_complete = 1 if all(int(item.get("is_complete") or 0) == 1 for item in contributing) else 0

                conn.execute(
                    "INSERT INTO clan_daily_battle_rollups (battle_date, clan_tag, clan_name, mode_group, game_mode_id, game_mode_name, members_active, battles, wins, losses, draws, crowns_for, crowns_against, trophy_change_total, captured_battles, expected_battle_delta, completeness_ratio, is_complete, last_aggregated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        battle_date,
                        clan_tag,
                        clan_name,
                        bucket["mode_group"],
                        bucket["game_mode_id"],
                        bucket["game_mode_name"],
                        len(bucket["members"]),
                        bucket["battles"],
                        bucket["wins"],
                        bucket["losses"],
                        bucket["draws"],
                        bucket["crowns_for"],
                        bucket["crowns_against"],
                        bucket["trophy_change_total"],
                        captured_battles,
                        expected_battle_delta,
                        completeness_ratio,
                        is_complete,
                        aggregated_at,
                    ),
                )
        if close:
            conn.commit()
    finally:
        if close:
            conn.close()


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
            "trophies_per_battle": round(trophy_delta / max(1, len(trophy_rows)), 1),
            "latest_battle_type": new_rows[0]["battle_type"],
            "latest_mode_name": new_rows[0]["game_mode_name"],
        })
    return signals


@managed_connection
def snapshot_player_battlelog(player_tag: str, battle_log: list[dict], conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    tag = _canon_tag(player_tag)
    member_id = _ensure_member(conn, tag, status=None)
    previous_streak = _current_ladder_ranked_streak(member_id, conn=conn)
    previous_latest_ladder_ranked_battle_time = _latest_ladder_ranked_battle_time(member_id, conn=conn)
    _store_raw_payload(conn, "player_battlelog", _tag_key(tag), battle_log)
    latest_name = None
    affected_dates = set()
    for battle in battle_log or []:
        team = (battle.get("team") or [{}])[0]
        opp = (battle.get("opponent") or [{}])[0]
        if not team:
            continue
        latest_name = latest_name or team.get("name")
        battle_date = chicago_date_for_cr_timestamp(battle.get("battleTime"))
        if battle_date:
            affected_dates.add(battle_date)
        crowns_for = team.get("crowns")
        crowns_against = opp.get("crowns") if opp else None
        outcome = _resolve_battle_outcome(battle, team, opp)
        arena = battle.get("arena") or {}
        classified = _classify_battle(battle)
        conn.execute(
            "INSERT OR IGNORE INTO member_battle_facts (member_id, battle_time, battle_type, game_mode_name, game_mode_id, deck_selection, arena_id, arena_name, crowns_for, crowns_against, outcome, trophy_change, starting_trophies, is_competitive, is_ladder, is_ranked, is_war, is_special_event, deck_json, support_cards_json, opponent_deck_json, opponent_support_cards_json, opponent_name, opponent_tag, opponent_clan_tag, event_tag, league_number, is_hosted_match, modifiers_json, team_rounds_json, opponent_rounds_json, boat_battle_side, boat_battle_won, new_towers_destroyed, prev_towers_destroyed, remaining_towers, tournament_tag, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                _json_or_none(_normalize_cards_for_storage(team.get("cards") or [])),
                _json_or_none(_normalize_cards_for_storage(team.get("supportCards") or [])),
                _json_or_none(_normalize_cards_for_storage(opp.get("cards") or [])) if opp else None,
                _json_or_none(_normalize_cards_for_storage(opp.get("supportCards") or [])) if opp else None,
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
                battle.get("tournamentTag"),
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
    _recompute_member_daily_battle_rollups(member_id, affected_dates, conn=conn)
    _recompute_clan_daily_battle_rollups(affected_dates, conn=conn)
    _recompute_member_recent_form(member_id, conn=conn)
    _upsert_member_metadata(conn, member_id, **_games_per_day_metadata_fields(member_id, computed_at=_utcnow(), conn=conn))
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


_LOSSES_SCOPE_PREDICATES = {
    "overall_10": "1=1",
    "competitive_10": "is_competitive = 1",
    "ladder_ranked_10": "(is_ladder = 1 OR is_ranked = 1)",
    "war_10": "is_war = 1",
}


@managed_connection
def get_member_recent_losses(
    tag: str,
    scope: str = "competitive_10",
    limit: int = 30,
    top_cards: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Aggregate the cards a player has been losing to recently.

    Looks at the most recent `limit` battles in the given scope, filters to
    losses, and returns the top opponent cards seen alongside crown deficit
    and current loss-streak context. Powers the `losses` include on get_member.
    """
    member_tag = _canon_tag(tag)
    predicate = _LOSSES_SCOPE_PREDICATES.get(scope, _LOSSES_SCOPE_PREDICATES["competitive_10"])
    member_row = conn.execute(
        "SELECT member_id, current_name FROM members WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return None
    member_id = member_row["member_id"]
    rows = conn.execute(
        f"SELECT outcome, crowns_for, crowns_against, opponent_deck_json, battle_time, battle_type, game_mode_name, "
        f"opponent_tag, opponent_name, opponent_clan_tag "
        f"FROM member_battle_facts WHERE member_id = ? AND {predicate} "
        f"ORDER BY battle_time DESC LIMIT ?",
        (member_id, limit),
    ).fetchall()
    sample_battles = len(rows)
    losses = [r for r in rows if r["outcome"] == "L"]
    losses_examined = len(losses)
    counts: dict[str, int] = {}
    icons: dict[str, str] = {}
    opponent_agg: dict[str, dict] = {}
    for row in losses:
        opp_tag = row["opponent_tag"]
        if opp_tag:
            entry = opponent_agg.get(opp_tag)
            if entry is None:
                entry = {
                    "tag": opp_tag,
                    "name": row["opponent_name"],
                    "clan_tag": row["opponent_clan_tag"],
                    "losses_count": 0,
                }
                opponent_agg[opp_tag] = entry
            entry["losses_count"] += 1
        try:
            opp_cards = json.loads(row["opponent_deck_json"] or "[]")
        except (TypeError, ValueError):
            continue
        for card in opp_cards:
            name = card.get("name")
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
            icon = (card.get("iconUrls") or {}).get("medium") if isinstance(card.get("iconUrls"), dict) else None
            if icon and name not in icons:
                icons[name] = icon
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:top_cards]
    top_opponent_cards = [
        {
            "name": name,
            "icon_url": icons.get(name, ""),
            "appearances": count,
            "pct_of_losses": round(count / losses_examined * 100) if losses_examined else 0,
        }
        for name, count in ordered
    ]
    crown_diffs = [
        (r["crowns_for"] or 0) - (r["crowns_against"] or 0)
        for r in losses
        if r["crowns_for"] is not None and r["crowns_against"] is not None
    ]
    avg_crown_deficit = round(sum(crown_diffs) / len(crown_diffs), 2) if crown_diffs else None
    current_loss_streak = 0
    for row in rows:
        if row["outcome"] == "L":
            current_loss_streak += 1
        else:
            break
    losses_with_opponent_data = sum(
        1 for r in losses if (r["opponent_deck_json"] or "").strip() not in ("", "[]", "null")
    )
    coverage_note = None
    if losses_examined and losses_with_opponent_data < losses_examined:
        coverage_note = (
            f"{losses_with_opponent_data}/{losses_examined} losses had opponent deck data captured "
            "(older battles may pre-date opponent-deck capture)."
        )
    opponent_tags = sorted(
        opponent_agg.values(),
        key=lambda o: (o["losses_count"], o.get("name") or ""),
        reverse=True,
    )
    return {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "scope": scope,
        "lookback_battles": sample_battles,
        "losses_examined": losses_examined,
        "losses_with_opponent_data": losses_with_opponent_data,
        "current_loss_streak": current_loss_streak,
        "avg_crown_deficit": avg_crown_deficit,
        "top_opponent_cards": top_opponent_cards,
        "opponent_tags": opponent_tags,
        "coverage_note": coverage_note,
        "guidance": (
            "Use top_opponent_cards to ground swap suggestions: cite specific cards that have "
            "appeared most often in this player's recent losses, then propose counters they own. "
            "Use opponent_tags to chain into cr_api (aspect='player' / 'clan') if the user asks "
            "to scout a specific opponent they lost to."
        ),
    }


@managed_connection
def list_member_daily_battle_rollups(tag: str, days: int = 30, mode_group: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    member_tag = _canon_tag(tag)
    cutoff = (datetime.fromisoformat(chicago_today()) - timedelta(days=max(days - 1, 0))).date().isoformat()
    where = ["m.player_tag = ?", "r.battle_date >= ?"]
    params = [member_tag, cutoff]
    if mode_group:
        where.append("r.mode_group = ?")
        params.append(mode_group)
    rows = conn.execute(
        "SELECT r.battle_date, r.mode_group, r.game_mode_id, r.game_mode_name, r.battles, r.wins, r.losses, r.draws, r.crowns_for, r.crowns_against, r.trophy_change_total, r.first_battle_at, r.last_battle_at, r.captured_battles, r.expected_battle_delta, r.completeness_ratio, r.is_complete "
        "FROM member_daily_battle_rollups r "
        "JOIN members m ON m.member_id = r.member_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY r.battle_date ASC, r.mode_group ASC, COALESCE(r.game_mode_id, 0) ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def list_clan_daily_battle_rollups(days: int = 30, clan_tag: Optional[str] = None, mode_group: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    cutoff = (datetime.fromisoformat(chicago_today()) - timedelta(days=max(days - 1, 0))).date().isoformat()
    where = ["battle_date >= ?"]
    params = [cutoff]
    if clan_tag:
        where.append("clan_tag = ?")
        params.append(_canon_tag(clan_tag))
    if mode_group:
        where.append("mode_group = ?")
        params.append(mode_group)
    rows = conn.execute(
        "SELECT battle_date, clan_tag, clan_name, mode_group, game_mode_id, game_mode_name, members_active, battles, wins, losses, draws, crowns_for, crowns_against, trophy_change_total, captured_battles, expected_battle_delta, completeness_ratio, is_complete "
        f"FROM clan_daily_battle_rollups WHERE {' AND '.join(where)} "
        "ORDER BY battle_date ASC, mode_group ASC, COALESCE(game_mode_id, 0) ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def _recompute_member_recent_form(member_id: int, conn=None):
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
