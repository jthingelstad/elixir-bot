"""LLM-facing Clash Royale API bridge.

This module keeps the external CR API filtering/envelope logic out of the
general tool dispatcher. Local clan/member questions should still prefer the
structured storage tools; this bridge is for arbitrary external tags and live
API-only surfaces.
"""

from __future__ import annotations

import cr_api
from storage.game_modes import battle_matches_mode


_CR_BATTLES_DEFAULT_LIMIT = 15
_CR_BATTLES_MAX_LIMIT = 25
_CR_MEMBERS_DEFAULT_LIMIT = 15
_CR_MEMBERS_MAX_LIMIT = 30
_CR_TOURNAMENT_DEFAULT_LIMIT = 15
_CR_TOURNAMENT_MAX_LIMIT = 30
_CR_CHESTS_LIMIT = 10
_CR_DESCRIPTION_MAX = 500


def _clamp_limit(raw, default, maximum):
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, maximum)


def _filter_cr_player(payload):
    clan = payload.get("clan") or {}
    arena = payload.get("arena") or {}
    fav = payload.get("currentFavouriteCard") or {}
    current_deck = payload.get("currentDeck") or []
    achievements = payload.get("achievements") or []
    return {
        "name": payload.get("name"),
        "tag": payload.get("tag"),
        "expLevel": payload.get("expLevel"),
        "trophies": payload.get("trophies"),
        "bestTrophies": payload.get("bestTrophies"),
        "wins": payload.get("wins"),
        "losses": payload.get("losses"),
        "battleCount": payload.get("battleCount"),
        "threeCrownWins": payload.get("threeCrownWins"),
        "donations": payload.get("donations"),
        "donationsReceived": payload.get("donationsReceived"),
        "role": payload.get("role"),
        "clan": {"tag": clan.get("tag"), "name": clan.get("name")} if clan else None,
        "arena": {"name": arena.get("name")} if arena else None,
        "currentFavouriteCard": fav.get("name") if fav else None,
        "currentDeck": [
            {
                "name": card.get("name"),
                "level": card.get("level"),
                "maxLevel": card.get("maxLevel"),
                "elixirCost": card.get("elixirCost"),
            }
            for card in current_deck
        ],
        "achievements": [
            a.get("name") for a in achievements if a.get("name")
        ],
    }


def _filter_cr_player_battles(payload, *, limit, mode):
    limit = _clamp_limit(limit, _CR_BATTLES_DEFAULT_LIMIT, _CR_BATTLES_MAX_LIMIT)
    battles = payload if isinstance(payload, list) else payload.get("items") or []
    trimmed = []
    for battle in battles:
        game_mode_payload = battle.get("gameMode") or {}
        game_mode = game_mode_payload.get("name")
        game_mode_id = game_mode_payload.get("id")
        battle_type = battle.get("type") or ""
        if mode and not battle_matches_mode(
            mode,
            battle_type=battle_type,
            game_mode_id=game_mode_id,
            game_mode_name=game_mode,
            deck_selection=battle.get("deckSelection"),
            event_tag=battle.get("eventTag"),
            tournament_tag=battle.get("tournamentTag"),
            is_hosted_match=battle.get("isHostedMatch"),
            team_size=len(battle.get("team") or []),
            opponent_size=len(battle.get("opponent") or []),
        ):
            continue
        trimmed.append({
            "battleTime": battle.get("battleTime"),
            "type": battle_type,
            "gameMode": game_mode,
            "gameModeId": game_mode_id,
            "deckSelection": battle.get("deckSelection"),
            "eventTag": battle.get("eventTag"),
            "tournamentTag": battle.get("tournamentTag"),
            "arena": (battle.get("arena") or {}).get("name"),
            "team": [_filter_cr_battle_participant(p) for p in (battle.get("team") or [])],
            "opponent": [_filter_cr_battle_participant(p) for p in (battle.get("opponent") or [])],
        })
        if len(trimmed) >= limit:
            break
    return {"battles": trimmed, "count": len(trimmed)}


def _filter_cr_battle_participant(p):
    clan = p.get("clan") or {}
    return {
        "tag": p.get("tag"),
        "name": p.get("name"),
        "crowns": p.get("crowns"),
        "trophyChange": p.get("trophyChange"),
        "clan": {"tag": clan.get("tag"), "name": clan.get("name")} if clan else None,
    }


def _filter_cr_player_chests(items):
    names = [chest.get("name") for chest in (items or []) if isinstance(chest, dict)]
    return {"upcoming": names[:_CR_CHESTS_LIMIT], "count": min(len(names), _CR_CHESTS_LIMIT)}


def _clan_member_summary(member_list):
    if not member_list:
        return {
            "total_members": 0,
            "role_counts": {},
            "avg_trophies": 0,
            "median_trophies": 0,
            "total_donations_week": 0,
        }
    role_counts = {}
    trophies = []
    total_donations = 0
    for member in member_list:
        role = member.get("role") or "unknown"
        role_counts[role] = role_counts.get(role, 0) + 1
        if isinstance(member.get("trophies"), (int, float)):
            trophies.append(member["trophies"])
        if isinstance(member.get("donations"), (int, float)):
            total_donations += member["donations"]
    trophies.sort()
    count = len(trophies)
    avg = round(sum(trophies) / count) if count else 0
    median = trophies[count // 2] if count else 0
    return {
        "total_members": len(member_list),
        "role_counts": role_counts,
        "avg_trophies": avg,
        "median_trophies": median,
        "total_donations_week": total_donations,
    }


def _truncated_description(payload):
    description = payload.get("description") or ""
    if len(description) > _CR_DESCRIPTION_MAX:
        return description[:_CR_DESCRIPTION_MAX] + "..."
    return description


def _filter_cr_clan(payload):
    location = payload.get("location") or {}
    member_list = payload.get("memberList") or []
    return {
        "name": payload.get("name"),
        "tag": payload.get("tag"),
        "description": _truncated_description(payload),
        "type": payload.get("type"),
        "clanScore": payload.get("clanScore"),
        "clanWarTrophies": payload.get("clanWarTrophies"),
        "requiredTrophies": payload.get("requiredTrophies"),
        "members_count": payload.get("members"),
        "location": location.get("name"),
        "badgeId": payload.get("badgeId"),
        "members_summary": _clan_member_summary(member_list),
    }


def _filter_cr_clan_members(payload, *, limit):
    limit = _clamp_limit(limit, _CR_MEMBERS_DEFAULT_LIMIT, _CR_MEMBERS_MAX_LIMIT)
    member_list = list(payload.get("memberList") or [])
    member_list.sort(key=lambda member: member.get("trophies") or 0, reverse=True)
    trimmed = [
        {
            "tag": member.get("tag"),
            "name": member.get("name"),
            "role": member.get("role"),
            "trophies": member.get("trophies"),
            "expLevel": member.get("expLevel"),
            "donations": member.get("donations"),
            "donationsReceived": member.get("donationsReceived"),
            "lastSeen": member.get("lastSeen"),
            "clanRank": member.get("clanRank"),
        }
        for member in member_list[:limit]
    ]
    return {
        "clan_name": payload.get("name"),
        "clan_tag": payload.get("tag"),
        "total_members": len(member_list),
        "members_returned": len(trimmed),
        "members": trimmed,
    }


def _filter_cr_clan_war(payload):
    clans = payload.get("clans") or []
    clan_summaries = []
    top_participants = []
    for clan in clans:
        participants = clan.get("participants") or []
        clan_summaries.append({
            "tag": clan.get("tag"),
            "name": clan.get("name"),
            "fame": clan.get("fame"),
            "repairPoints": clan.get("repairPoints"),
            "participants_count": len(participants),
        })
        for participant in participants:
            top_participants.append({
                "tag": participant.get("tag"),
                "name": participant.get("name"),
                "clan_tag": clan.get("tag"),
                "fame": participant.get("fame"),
                "decksUsed": participant.get("decksUsed"),
            })
    top_participants.sort(key=lambda participant: participant.get("fame") or 0, reverse=True)
    return {
        "state": payload.get("state"),
        "sectionIndex": payload.get("sectionIndex"),
        "periodIndex": payload.get("periodIndex"),
        "periodType": payload.get("periodType"),
        "clans": clan_summaries,
        "top_participants": top_participants[:5],
    }


def _filter_cr_clan_war_log(payload, *, focal_tag):
    items = payload.get("items") or []
    focal_tag_hash = f"#{focal_tag}" if focal_tag else None
    log = []
    for entry in items[:10]:
        standings = entry.get("standings") or []
        focal_rank = None
        focal_fame = None
        for standing in standings:
            clan = standing.get("clan") or {}
            if clan.get("tag") == focal_tag_hash:
                focal_rank = standing.get("rank")
                focal_fame = clan.get("fame")
                break
        log.append({
            "seasonId": entry.get("seasonId"),
            "sectionIndex": entry.get("sectionIndex"),
            "createdDate": entry.get("createdDate"),
            "finishRank": focal_rank,
            "fame": focal_fame,
        })
    return {"clan_tag": focal_tag_hash, "races": log, "count": len(log)}


def _filter_cr_tournament(payload, *, limit):
    limit = _clamp_limit(limit, _CR_TOURNAMENT_DEFAULT_LIMIT, _CR_TOURNAMENT_MAX_LIMIT)
    members = list(payload.get("membersList") or [])
    members.sort(key=lambda member: member.get("score") or 0, reverse=True)
    trimmed = [
        {
            "tag": member.get("tag"),
            "name": member.get("name"),
            "score": member.get("score"),
            "rank": member.get("rank"),
            "previousRank": member.get("previousRank"),
            "clan": {
                "tag": (member.get("clan") or {}).get("tag"),
                "name": (member.get("clan") or {}).get("name"),
            } if member.get("clan") else None,
        }
        for member in members[:limit]
    ]
    return {
        "tag": payload.get("tag"),
        "name": payload.get("name"),
        "description": _truncated_description(payload),
        "type": payload.get("type"),
        "status": payload.get("status"),
        "createdTime": payload.get("createdTime"),
        "startedTime": payload.get("startedTime"),
        "endedTime": payload.get("endedTime"),
        "firstPlaceCardPrize": payload.get("firstPlaceCardPrize"),
        "maxCapacity": payload.get("maxCapacity"),
        "levelCap": payload.get("levelCap"),
        "preparationDuration": payload.get("preparationDuration"),
        "duration": payload.get("duration"),
        "members_count": payload.get("membersCount"),
        "members_returned": len(trimmed),
        "members": trimmed,
    }


def _filter_cr_events(payload):
    events = []
    for event in payload or []:
        if not isinstance(event, dict):
            continue
        events.append({
            "eventTag": event.get("eventTag"),
            "title": event.get("title"),
            "description": event.get("description"),
        })
    return {"events": events, "count": len(events)}


def _filter_cr_ranking_list(payload, *, limit, score_field="eloRating"):
    limit = _clamp_limit(limit, 20, 50)
    items = (payload or {}).get("items") if isinstance(payload, dict) else []
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        clan = item.get("clan") or {}
        rows.append({
            "rank": item.get("rank"),
            "tag": item.get("tag"),
            "name": item.get("name"),
            "expLevel": item.get("expLevel"),
            score_field: item.get(score_field),
            "clan": {"tag": clan.get("tag"), "name": clan.get("name")} if clan else None,
        })
        if len(rows) >= limit:
            break
    return {"items": rows, "count": len(rows)}


def _filter_cr_leaderboards(payload):
    items = (payload or {}).get("items") if isinstance(payload, dict) else []
    boards = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        boards.append({"id": item.get("id"), "name": item.get("name")})
    return {"leaderboards": boards, "count": len(boards)}


def _filter_cr_leaderboard(payload, *, limit):
    limit = _clamp_limit(limit, 20, 50)
    items = (payload or {}).get("items") if isinstance(payload, dict) else []
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        clan = item.get("clan") or {}
        rows.append({
            "rank": item.get("rank"),
            "score": item.get("score"),
            "tag": item.get("tag"),
            "name": item.get("name"),
            "clan": {"tag": clan.get("tag"), "name": clan.get("name")} if clan else None,
        })
        if len(rows) >= limit:
            break
    return {"items": rows, "count": len(rows)}


def _execute_cr_api(arguments):
    """Unified CR API bridge. One aspect per call; returns a filtered payload."""
    aspect = arguments.get("aspect")
    if not aspect:
        return {"error": "aspect is required"}
    limit = arguments.get("limit")
    if aspect == "events":
        payload = cr_api.get_events()
        return _filter_cr_events(payload) if payload is not None else {"error": "not_found_or_unavailable"}
    if aspect == "pathoflegend_location_rankings":
        payload = cr_api.get_pathoflegend_location_rankings(arguments.get("location_id") or "global", limit=limit or 20)
        return _filter_cr_ranking_list(payload, limit=limit) if payload is not None else {"error": "not_found_or_unavailable"}
    if aspect == "pathoflegend_season_rankings":
        season_id = arguments.get("season_id")
        if not season_id:
            return {"error": "season_id is required"}
        try:
            payload = cr_api.get_pathoflegend_season_rankings(season_id, limit=limit or 20)
        except cr_api.InvalidTagError as exc:
            return {"error": "invalid_season_id", "detail": str(exc)}
        return _filter_cr_ranking_list(payload, limit=limit) if payload is not None else {"error": "not_found_or_unavailable"}
    if aspect == "leaderboards":
        payload = cr_api.get_leaderboards()
        return _filter_cr_leaderboards(payload) if payload is not None else {"error": "not_found_or_unavailable"}
    if aspect == "leaderboard":
        leaderboard_id = arguments.get("leaderboard_id")
        if leaderboard_id is None:
            return {"error": "leaderboard_id is required"}
        try:
            payload = cr_api.get_leaderboard(leaderboard_id, limit=limit or 20)
        except cr_api.InvalidTagError as exc:
            return {"error": "invalid_leaderboard_id", "detail": str(exc)}
        return _filter_cr_leaderboard(payload, limit=limit) if payload is not None else {"error": "not_found_or_unavailable"}

    raw_tag = arguments.get("tag")
    try:
        normalized_tag = cr_api._normalize_cr_tag(raw_tag)
    except cr_api.InvalidTagError as exc:
        return {"error": "invalid_tag", "detail": str(exc)}

    mode = arguments.get("mode")

    if aspect in ("clan", "clan_members") and normalized_tag == cr_api.CLAN_TAG:
        return {
            "error": "our_clan_use_local_tools",
            "hint": "Use get_clan_roster or get_clan_health for our own clan — local data is deeper.",
        }

    if aspect == "player":
        payload = cr_api.get_player(normalized_tag)
        return _filter_cr_player(payload) if payload else {"error": "not_found_or_unavailable"}
    if aspect == "player_battles":
        payload = cr_api.get_player_battle_log(normalized_tag)
        return _filter_cr_player_battles(payload, limit=limit, mode=mode) if payload is not None else {"error": "not_found_or_unavailable"}
    if aspect == "player_chests":
        payload = cr_api.get_player_chests(normalized_tag)
        return _filter_cr_player_chests(payload) if payload is not None else {"error": "not_found_or_unavailable"}
    if aspect == "clan":
        payload = cr_api.get_clan_by_tag(normalized_tag)
        return _filter_cr_clan(payload) if payload else {"error": "not_found_or_unavailable"}
    if aspect == "clan_members":
        payload = cr_api.get_clan_by_tag(normalized_tag)
        return _filter_cr_clan_members(payload, limit=limit) if payload else {"error": "not_found_or_unavailable"}
    if aspect == "clan_war":
        payload = cr_api.get_current_war(normalized_tag)
        return _filter_cr_clan_war(payload) if payload else {"error": "not_found_or_unavailable"}
    if aspect == "clan_war_log":
        payload = cr_api.get_river_race_log(normalized_tag)
        return _filter_cr_clan_war_log(payload, focal_tag=normalized_tag) if payload else {"error": "not_found_or_unavailable"}
    if aspect == "tournament":
        payload = cr_api.get_tournament(normalized_tag)
        return _filter_cr_tournament(payload, limit=limit) if payload else {"error": "not_found_or_unavailable"}

    return {"error": f"Unknown aspect: {aspect}"}


__all__ = ["_execute_cr_api"]
