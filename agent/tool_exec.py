import json
import re

from agent import app as _app


class _ModuleProxy:
    def __init__(self, getter):
        self._getter = getter

    def __getattr__(self, name):
        return getattr(self._getter(), name)


db = _ModuleProxy(lambda: _app.db)
cr_api = _app.cr_api
log = _app.log


def _resource_constraints_note() -> dict:
    return {
        "gold_known": False,
        "gold_note": "Current gold is not available in Elixir's stored Clash Royale player data.",
    }


def _enrich_member_profile(result):
    if not isinstance(result, dict):
        return result

    enriched = dict(result)
    role = enriched.get("role")
    member_name = enriched.get("member_name") or enriched.get("current_name") or "This member"
    if role:
        if role == "leader":
            enriched["current_role_summary"] = f"{member_name} is currently the clan leader."
        elif role == "coLeader":
            enriched["current_role_summary"] = f"{member_name} is currently a co-leader."
        elif role == "elder":
            enriched["current_role_summary"] = f"{member_name} is currently an Elder."
        else:
            enriched["current_role_summary"] = f"{member_name} is currently a member."

    age_years = enriched.get("cr_account_age_years")
    age_days = enriched.get("cr_account_age_days")
    if age_years is not None or age_days is not None:
        age_parts = []
        if isinstance(age_years, int) and age_years >= 0:
            age_parts.append(f"{age_years} year{'s' if age_years != 1 else ''}")
        if isinstance(age_days, int) and age_days >= 0:
            age_parts.append(f"{age_days:,} day{'s' if age_days != 1 else ''}")
        if age_parts:
            enriched["account_age_summary"] = (
                "Derived Clash Royale account age from Years Played badge data: "
                + " / ".join(age_parts)
            )

    games_per_day = enriched.get("cr_games_per_day")
    window_days = enriched.get("cr_games_per_day_window_days")
    if isinstance(games_per_day, (int, float)) and window_days:
        enriched["recent_activity_summary"] = (
            f"Recent activity: {games_per_day:.2f} games played per day over the last {window_days} days"
        )

    enriched.update(_resource_constraints_note())
    return enriched


def _enrich_member_card_collection(result):
    if not isinstance(result, dict):
        return result
    enriched = dict(result)
    enriched.update(_resource_constraints_note())
    enriched["upgrade_guidance_note"] = (
        "Use this collection to suggest upgrade priorities or cards closest to max. "
        "Do not claim a member can afford an upgrade right now unless current gold is explicitly available."
    )
    return enriched


def _enrich_war_player_type(result, tag):
    """Add war_player_type classification to a result dict by player tag."""
    from storage.war_analytics import _war_player_type
    from db import get_connection

    canon = tag if tag.startswith("#") else f"#{tag}"
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT member_id FROM members WHERE player_tag = ?", (canon,),
        ).fetchone()
        if row:
            result["war_player_type"] = _war_player_type(conn, row["member_id"])
    finally:
        conn.close()


def _enrich_war_player_types(members):
    """Add war_player_type to each member dict in a list."""
    from storage.war_analytics import war_player_types_by_tag
    from db import get_connection

    tags = [
        member.get("tag") or member.get("player_tag") or ""
        for member in members
    ]
    tags = [t for t in tags if t]
    if not tags:
        return

    conn = get_connection()
    try:
        types_by_tag = war_player_types_by_tag(conn, tags)
    finally:
        conn.close()

    for member in members:
        tag = member.get("tag") or member.get("player_tag") or ""
        if not tag:
            continue
        canon = tag if tag.startswith("#") else f"#{tag}"
        if canon in types_by_tag:
            member["war_player_type"] = types_by_tag[canon]


def _refresh_member_cache(member_tag, include_battles=False):
    """Refresh stored player profile and optionally battle log for a member."""
    player = cr_api.get_player(member_tag)
    if player:
        db.snapshot_player_profile(player)
    else:
        log.warning("player_profile_refresh_skipped tag=%s reason=cr_api_returned_none", member_tag)
    if include_battles:
        battles = cr_api.get_player_battle_log(member_tag)
        if battles:
            db.snapshot_player_battlelog(member_tag, battles)
        else:
            log.warning("player_battlelog_refresh_skipped tag=%s reason=cr_api_returned_none", member_tag)


def _resolve_member_tag(value):
    """Accept a tag, name, alias, or Discord handle and return a canonical player tag."""
    from storage.roster import pick_best_match

    query = (value or "").strip()
    if not query:
        raise ValueError("member reference is required")
    if query.startswith("#"):
        return query
    if re.fullmatch(r"[0289PYLQGRJCUV]{3,15}", query.upper()):
        return f"#{query.upper()}"

    matches = db.resolve_member(query, limit=5)
    if not matches:
        log.warning("member_resolution_failed query=%r reason=no_matches", query)
        raise ValueError(f"Could not resolve member reference: {query}")
    best = pick_best_match(matches)
    if best is not None:
        return best["player_tag"]
    top, second = matches[0], matches[1]
    choices = ", ".join(m.get("member_ref_with_handle") or m.get("current_name") or m["player_tag"] for m in matches[:3])
    log.warning(
        "member_resolution_ambiguous query=%r top_score=%d second_score=%d choices=%s",
        query, top.get("match_score", 0), second.get("match_score", 0), choices,
    )
    raise ValueError(f"Ambiguous member reference '{query}'. Top matches: {choices}")


# ── Member domain execution ───────────────────────────────────────────────

def _execute_get_member(arguments):
    """Execute the consolidated get_member tool."""
    member_tag = _resolve_member_tag(arguments["member_tag"])
    include = arguments.get("include") or ["profile", "form"]
    scope = arguments.get("scope", "competitive_10")
    days = arguments.get("days", 30)

    needs_battles = any(a in include for a in ("form", "deck", "war"))
    _refresh_member_cache(member_tag, include_battles=needs_battles)

    result = {}

    if "profile" in include:
        result["profile"] = _enrich_member_profile(db.get_member_profile(member_tag))

    if "form" in include:
        result["form"] = db.get_member_recent_form(member_tag, scope=scope)

    if "war" in include:
        result["war"] = db.get_member_war_status(member_tag, season_id=None)

    if "trend" in include:
        result["trend"] = db.build_member_trend_summary_context(
            member_tag, days=days, window_days=min(days // 4, 7) or 7,
        )

    if "deck" in include:
        result["current_deck"] = db.get_member_current_deck(member_tag)
        result["signature_cards"] = db.get_member_signature_cards(
            member_tag, mode_scope="overall",
        )

    if "cards" in include:
        result["card_collection"] = _enrich_member_card_collection(
            db.get_member_card_collection(
                member_tag,
                limit=arguments.get("limit", 60),
                min_level=arguments.get("min_level"),
                include_support=True,
                rarity=arguments.get("rarity"),
            )
        )

    if "losses" in include:
        result["losses"] = db.get_member_recent_losses(
            member_tag,
            scope=scope,
            limit=arguments.get("losses_limit", 30),
        )

    if "history" in include:
        result["history"] = db.get_member_history(member_tag, days=days)

    if "memories" in include:
        from memory_store import list_memories

        memories = list_memories(
            viewer_scope="public",
            filters={"member_tag": member_tag},
            limit=15,
        )
        if not memories:
            result["memories"] = {"member_tag": member_tag, "memories": [], "message": "No stored memories for this member."}
        else:
            result["memories"] = {
                "member_tag": member_tag,
                "count": len(memories),
                "memories": [
                    {
                        "title": m.get("title"),
                        "summary": m.get("summary") or m.get("body", "")[:220],
                        "source_type": m.get("source_type"),
                        "scope": m.get("scope"),
                        "created_at": m.get("created_at"),
                        "tags": m.get("tags", []),
                    }
                    for m in memories
                ],
            }

    if "chests" in include:
        result["chests"] = cr_api.get_player_chests(member_tag)

    return result


def _execute_get_member_war_detail(arguments):
    """Execute the consolidated get_member_war_detail tool."""
    member_tag = _resolve_member_tag(arguments["member_tag"])
    aspect = arguments.get("aspect", "summary")

    if aspect == "battles":
        _refresh_member_cache(member_tag, include_battles=True)

    if aspect == "summary":
        result = db.get_member_war_stats(member_tag)
    elif aspect == "attendance":
        result = db.get_member_war_attendance(member_tag, season_id=None)
    elif aspect == "battles":
        result = db.get_member_war_battle_record(member_tag, season_id=None)
    elif aspect == "missed_days":
        result = db.get_member_missed_war_days(member_tag, season_id=None)
    elif aspect == "vs_clan_avg":
        result = db.compare_member_war_to_clan_average(member_tag, season_id=None)
    elif aspect == "war_decks":
        _refresh_member_cache(member_tag, include_battles=True)
        result = db.reconstruct_member_war_decks(member_tag)
    else:
        return {"error": f"Unknown aspect: {aspect}"}

    if isinstance(result, dict):
        _enrich_war_player_type(result, member_tag)

    return result


# ── River Race domain execution ────────────────────────────────────────────

def _execute_get_river_race(arguments):
    """Execute the consolidated get_river_race tool."""
    aspect = arguments.get("aspect", "standings")

    if aspect == "standings":
        war_status = db.get_current_war_status()
        if not isinstance(war_status, dict):
            return {"error": "No active war data available."}
        return {
            "race_standings": war_status.get("race_standings", []),
            "race_rank": war_status.get("race_rank"),
            "season_week_label": war_status.get("season_week_label"),
            "colosseum_week": war_status.get("colosseum_week"),
            "final_battle_day_active": war_status.get("final_battle_day_active"),
            "final_practice_day_active": war_status.get("final_practice_day_active"),
            "trophy_stakes_text": war_status.get("trophy_stakes_text"),
        }

    if aspect == "engagement":
        day_state = db.get_current_war_day_state()
        if not isinstance(day_state, dict):
            return {"error": "No active war data available."}
        return {
            "war_day_key": day_state.get("war_day_key"),
            "phase": day_state.get("phase"),
            "phase_display": day_state.get("phase_display"),
            "day_number": day_state.get("day_number"),
            "day_total": day_state.get("day_total"),
            "clan_fame": day_state.get("clan_fame"),
            "period_started_at": day_state.get("period_started_at"),
            "period_ends_at": day_state.get("period_ends_at"),
            "time_left_text": day_state.get("time_left_text"),
            "total_participants": day_state.get("total_participants"),
            "engaged_count": day_state.get("engaged_count"),
            "finished_count": day_state.get("finished_count"),
            "untouched_count": day_state.get("untouched_count"),
            "top_fame_today": day_state.get("top_fame_today"),
            "top_fame_total": day_state.get("top_fame_total"),
            "used_all_4": day_state.get("used_all_4"),
            "used_some": day_state.get("used_some"),
            "used_none": day_state.get("used_none"),
        }

    return {"error": f"Unknown aspect: {aspect}"}


def _execute_get_war_season(arguments):
    """Execute the consolidated get_war_season tool."""
    aspect = arguments.get("aspect", "summary")
    season_id = arguments.get("season_id")
    limit = arguments.get("limit", 10)

    if aspect == "summary":
        return db.get_war_season_summary(season_id=season_id, top_n=limit)
    elif aspect == "standings":
        return db.get_war_champ_standings(season_id=season_id)
    elif aspect == "win_rates":
        return db.get_war_battle_win_rates(
            season_id=season_id, limit=limit, min_battles=1,
        )
    elif aspect == "boat_battles":
        return db.get_clan_boat_battle_record(wars=3)
    elif aspect == "score_trend":
        return db.get_war_score_trend(days=30)
    elif aspect == "season_comparison":
        return db.compare_fame_per_member_to_previous_season(season_id=season_id)
    elif aspect == "trending":
        return db.get_trending_war_contributors(
            season_id=season_id, recent_races=2, limit=limit,
        )
    elif aspect == "perfect_attendance":
        return db.get_perfect_war_participants(season_id=season_id)
    elif aspect == "no_participation":
        return db.get_members_without_war_participation(season_id=season_id)
    else:
        return {"error": f"Unknown aspect: {aspect}"}


def _execute_get_war_member_standings(arguments):
    """Execute the new get_war_member_standings tool."""
    metric = arguments.get("metric", "fame")
    season_id = arguments.get("season_id")
    limit = arguments.get("limit", 30)

    if metric == "fame":
        raw = db.get_war_champ_standings(season_id=season_id)
    elif metric == "win_rate":
        raw = db.get_war_battle_win_rates(
            season_id=season_id, limit=limit, min_battles=1,
        )
    elif metric == "attendance":
        raw = db.get_members_without_war_participation(season_id=season_id)
    else:
        return {"error": f"Unknown metric: {metric}"}

    if isinstance(raw, dict):
        members = raw.get("members") or raw.get("standings") or raw.get("results") or []
        _enrich_war_player_types(members)

    return raw


# ── Clan domain execution ─────────────────────────────────────────────────

def _execute_get_clan_roster(arguments):
    """Execute the consolidated get_clan_roster tool."""
    aspect = arguments.get("aspect", "list")
    days = arguments.get("days", 30)
    limit = arguments.get("limit", 10)

    if aspect == "list":
        return db.list_members()
    elif aspect == "summary":
        return db.get_clan_roster_summary()
    elif aspect == "recent_joins":
        return db.list_recent_joins(days=days)
    elif aspect == "longest_tenure":
        return db.list_longest_tenure_members(limit=limit)
    elif aspect == "role_changes":
        return db.get_recent_role_changes(days=days)
    elif aspect == "max_cards":
        return db.get_members_with_most_level_16_cards(limit=limit)
    else:
        return {"error": f"Unknown aspect: {aspect}"}


def _execute_get_clan_health(arguments, workflow=None):
    """Execute the consolidated get_clan_health tool."""
    aspect = arguments.get("aspect", "at_risk")

    # Sensitive aspect gating
    sensitive_aspects = {"at_risk", "promotion_candidates"}
    allowed_workflows = {"clanops", "channel_update_leadership"}
    if aspect in sensitive_aspects and workflow not in allowed_workflows:
        return {"error": f"The '{aspect}' analysis is only available in leadership channels."}

    if aspect == "at_risk":
        return db.get_members_at_risk(
            inactivity_days=arguments.get("inactivity_days", 7),
            min_donations_week=arguments.get("min_donations_week", 20),
            require_war_participation=False,
            min_war_races=1,
            tenure_grace_days=14,
            season_id=arguments.get("season_id"),
        )
    elif aspect == "hot_streaks":
        return db.get_members_on_hot_streak(
            min_streak=arguments.get("min_streak", 4),
            scope="ladder_ranked_10",
        )
    elif aspect == "losing_streaks":
        return db.get_members_on_losing_streak(
            min_streak=arguments.get("min_streak", 3),
            scope="competitive_10",
        )
    elif aspect == "trophy_drops":
        return db.get_trophy_drops(
            days=arguments.get("days", 7),
            min_drop=arguments.get("min_drop", 100),
        )
    elif aspect == "promotion_candidates":
        return db.get_promotion_candidates()
    else:
        return {"error": f"Unknown aspect: {aspect}"}


def _execute_get_clan_trends(arguments):
    """Execute the consolidated get_clan_trends tool."""
    window_days = arguments.get("window_days", 7)
    days = arguments.get("days", 30)
    # Return both the comparison and the summary for completeness
    comparison = db.compare_clan_trend_windows(window_days=window_days)
    summary = db.build_clan_trend_summary_context(days=days, window_days=window_days)
    if isinstance(comparison, dict):
        comparison["trend_summary"] = summary
        return comparison
    return {"comparison": comparison, "trend_summary": summary}


# ── Write tools execution ─────────────────────────────────────────────────

def _execute_update_member(arguments):
    """Execute the consolidated update_member tool."""
    member_tag = _resolve_member_tag(arguments["member_tag"])
    field = arguments["field"]
    value = arguments["value"]

    if field == "birthday":
        if isinstance(value, dict):
            month = value.get("month")
            day = value.get("day")
        else:
            raise ValueError("birthday value must be {\"month\": M, \"day\": D}")
        db.set_member_birthday(member_tag, name=None, month=month, day=day)
    elif field == "join_date":
        db.set_member_join_date(member_tag, name=None, joined_date=str(value))
    elif field == "profile_url":
        db.set_member_profile_url(member_tag, name=None, url=str(value))
    elif field == "note":
        db.set_member_note(member_tag, name=None, note=str(value))
    else:
        return {"error": f"Unknown field: {field}"}

    return {"success": True, "field": field}


# ── CR API bridge ─────────────────────────────────────────────────────────

_CR_BATTLES_DEFAULT_LIMIT = 15
_CR_BATTLES_MAX_LIMIT = 25
_CR_MEMBERS_DEFAULT_LIMIT = 15
_CR_MEMBERS_MAX_LIMIT = 30
_CR_TOURNAMENT_DEFAULT_LIMIT = 15
_CR_TOURNAMENT_MAX_LIMIT = 30
_CR_CHESTS_LIMIT = 10
_CR_TOP_BATTLE_OPPONENT_FIELDS = ("tag", "name", "crowns")
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
        game_mode = (battle.get("gameMode") or {}).get("name")
        battle_type = battle.get("type") or ""
        if mode and not _battle_matches_mode(mode, battle_type, game_mode):
            continue
        trimmed.append({
            "battleTime": battle.get("battleTime"),
            "type": battle_type,
            "gameMode": game_mode,
            "arena": (battle.get("arena") or {}).get("name"),
            "team": [_filter_cr_battle_participant(p) for p in (battle.get("team") or [])],
            "opponent": [_filter_cr_battle_participant(p) for p in (battle.get("opponent") or [])],
        })
        if len(trimmed) >= limit:
            break
    return {"battles": trimmed, "count": len(trimmed)}


def _battle_matches_mode(mode, battle_type, game_mode_name):
    """Client-side filter for player_battles `mode` argument."""
    battle_type_l = (battle_type or "").lower()
    game_mode_l = (game_mode_name or "").lower()
    if mode == "ladder":
        return "ladder" in battle_type_l or "pvp" in battle_type_l
    if mode == "path_of_legends":
        return "pathoflegend" in battle_type_l or "path_of_legend" in battle_type_l or "path of legend" in game_mode_l
    if mode == "war":
        return "riverracepvp" in battle_type_l or "boat" in battle_type_l or "clanwar" in battle_type_l or "war" in game_mode_l
    if mode == "tournament":
        return "tournament" in battle_type_l or "tournament" in game_mode_l
    if mode == "challenge":
        return "challenge" in battle_type_l or "challenge" in game_mode_l
    return True


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
    for m in member_list:
        role = m.get("role") or "unknown"
        role_counts[role] = role_counts.get(role, 0) + 1
        if isinstance(m.get("trophies"), (int, float)):
            trophies.append(m["trophies"])
        if isinstance(m.get("donations"), (int, float)):
            total_donations += m["donations"]
    trophies.sort()
    n = len(trophies)
    avg = round(sum(trophies) / n) if n else 0
    median = trophies[n // 2] if n else 0
    return {
        "total_members": len(member_list),
        "role_counts": role_counts,
        "avg_trophies": avg,
        "median_trophies": median,
        "total_donations_week": total_donations,
    }


def _filter_cr_clan(payload):
    location = payload.get("location") or {}
    description = payload.get("description") or ""
    if len(description) > _CR_DESCRIPTION_MAX:
        description = description[:_CR_DESCRIPTION_MAX] + "..."
    member_list = payload.get("memberList") or []
    return {
        "name": payload.get("name"),
        "tag": payload.get("tag"),
        "description": description,
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
    member_list.sort(key=lambda m: m.get("trophies") or 0, reverse=True)
    trimmed = [
        {
            "tag": m.get("tag"),
            "name": m.get("name"),
            "role": m.get("role"),
            "trophies": m.get("trophies"),
            "expLevel": m.get("expLevel"),
            "donations": m.get("donations"),
            "donationsReceived": m.get("donationsReceived"),
            "lastSeen": m.get("lastSeen"),
            "clanRank": m.get("clanRank"),
        }
        for m in member_list[:limit]
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
    for c in clans:
        participants = c.get("participants") or []
        clan_summaries.append({
            "tag": c.get("tag"),
            "name": c.get("name"),
            "fame": c.get("fame"),
            "repairPoints": c.get("repairPoints"),
            "participants_count": len(participants),
        })
        for p in participants:
            top_participants.append({
                "tag": p.get("tag"),
                "name": p.get("name"),
                "clan_tag": c.get("tag"),
                "fame": p.get("fame"),
                "decksUsed": p.get("decksUsed"),
            })
    top_participants.sort(key=lambda p: p.get("fame") or 0, reverse=True)
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
        for s in standings:
            clan = s.get("clan") or {}
            if clan.get("tag") == focal_tag_hash:
                focal_rank = s.get("rank")
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
    description = payload.get("description") or ""
    if len(description) > _CR_DESCRIPTION_MAX:
        description = description[:_CR_DESCRIPTION_MAX] + "..."
    members = list(payload.get("membersList") or [])
    members.sort(key=lambda m: m.get("score") or 0, reverse=True)
    trimmed = [
        {
            "tag": m.get("tag"),
            "name": m.get("name"),
            "score": m.get("score"),
            "rank": m.get("rank"),
            "previousRank": m.get("previousRank"),
            "clan": {"tag": (m.get("clan") or {}).get("tag"), "name": (m.get("clan") or {}).get("name")} if m.get("clan") else None,
        }
        for m in members[:limit]
    ]
    return {
        "tag": payload.get("tag"),
        "name": payload.get("name"),
        "description": description,
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


def _execute_get_clan_intel_report(arguments):
    """Build a threat analysis for a competitor in our current river race.

    Wraps storage.opponent_intel.build_clan_intel_entry so the scheduled Intel
    Report (and conversational scouting) runs through normal tool plumbing.
    """
    from storage.opponent_intel import build_clan_intel_entry

    raw_tag = arguments.get("clan_tag")
    try:
        clan_tag = cr_api._normalize_cr_tag(raw_tag)
    except cr_api.InvalidTagError as exc:
        return {"error": "invalid_tag", "detail": str(exc)}

    war = cr_api.get_current_war()
    if not war:
        return {"error": "no_active_war", "hint": "Our clan is not currently in a river race."}

    war_clans = list(war.get("clans") or [])
    our_war_entry = war.get("clan")
    our_tag_hash = f"#{cr_api.CLAN_TAG}"
    if our_war_entry:
        war_clans = [our_war_entry] + [c for c in war_clans if (c.get("tag") or "").upper() != our_tag_hash.upper()]

    target_tag_hash = f"#{clan_tag}"
    target_entry = next(
        (c for c in war_clans if (c.get("tag") or "").upper() == target_tag_hash.upper()),
        None,
    )
    if target_entry is None:
        return {
            "error": "clan_not_in_current_war",
            "clan_tag": target_tag_hash,
            "hint": "This clan is not in our current river race. Use cr_api(aspect='clan') for general scouting.",
        }

    is_us = clan_tag == cr_api.CLAN_TAG
    clan_profile = cr_api.get_clan_by_tag(clan_tag)
    entry = build_clan_intel_entry(target_entry, clan_profile, is_us=is_us)
    return entry


def _execute_cr_api(arguments):
    """Unified CR API bridge. One aspect per call; returns a filtered payload."""
    aspect = arguments.get("aspect")
    if not aspect:
        return {"error": "aspect is required"}
    raw_tag = arguments.get("tag")
    try:
        normalized_tag = cr_api._normalize_cr_tag(raw_tag)
    except cr_api.InvalidTagError as exc:
        return {"error": "invalid_tag", "detail": str(exc)}

    limit = arguments.get("limit")
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


def _execute_flag_member_watch(arguments):
    """Awareness-loop observation: flag a member for leadership attention.

    Persists as a leadership-scoped, inference-typed memory with the
    ``watch-list`` tag so downstream readers (next tick's memory context,
    leadership digests) can filter.
    """
    from memory_store import attach_tags, create_memory

    member_tag_input = arguments.get("member_tag")
    reason = (arguments.get("reason") or "").strip()
    expires_at = arguments.get("expires_at")

    if not member_tag_input or not reason:
        return {"error": "flag_member_watch requires member_tag and reason"}

    resolved_tag = _resolve_member_tag(member_tag_input)
    title = f"Watch: {resolved_tag}"
    body = f"{reason}"
    try:
        memory = create_memory(
            title=title,
            body=body,
            summary=body[:220],
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.7,
            created_by="elixir:awareness-tool",
            scope="leadership",
            member_tag=resolved_tag,
            expires_at=expires_at,
        )
    except Exception as exc:
        log.warning("flag_member_watch failed: %s", exc)
        return {"error": "flag_member_watch_failed", "detail": str(exc)}

    attach_tags(memory["memory_id"], ["watch-list"], actor="elixir:awareness-tool")
    return {
        "success": True,
        "memory_id": memory["memory_id"],
        "member_tag": resolved_tag,
        "type": "watch",
    }


def _execute_schedule_revisit(arguments):
    """Awareness-loop self-scheduling: queue a reminder to look at a signal
    again at a later tick. Persists to the ``revisits`` table; surfaces in
    ``Situation.due_revisits`` when ``at`` has passed.
    """
    from storage.revisits import schedule_revisit

    signal_key = (arguments.get("signal_key") or "").strip()
    at = (arguments.get("at") or "").strip()
    rationale = (arguments.get("rationale") or "").strip()

    if not signal_key or not at or not rationale:
        return {"error": "schedule_revisit requires signal_key, at, and rationale"}

    try:
        row = schedule_revisit(
            signal_key=signal_key,
            due_at=at,
            rationale=rationale,
            created_by_workflow="awareness",
        )
    except ValueError as exc:
        return {"error": "invalid_revisit", "detail": str(exc)}
    except Exception as exc:
        log.warning("schedule_revisit failed: %s", exc)
        return {"error": "schedule_revisit_failed", "detail": str(exc)}

    return {
        "success": True,
        "revisit_id": row.get("revisit_id"),
        "signal_key": row.get("signal_key"),
        "due_at": row.get("due_at"),
    }


def _execute_record_leadership_followup(arguments):
    """Awareness-loop observation: queue an operational suggestion.

    Persists as a leadership-scoped, inference-typed memory with the
    ``followup`` tag. If ``member_tag`` is provided, the memory is scoped to
    that member so the member-context view surfaces it.
    """
    from memory_store import attach_tags, create_memory

    topic = (arguments.get("topic") or "").strip()
    recommendation = (arguments.get("recommendation") or "").strip()
    member_tag_input = arguments.get("member_tag")

    if not topic or not recommendation:
        return {"error": "record_leadership_followup requires topic and recommendation"}

    resolved_tag = _resolve_member_tag(member_tag_input) if member_tag_input else None
    title = f"Followup: {topic}"
    body = recommendation
    try:
        memory = create_memory(
            title=title,
            body=body,
            summary=body[:220],
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.7,
            created_by="elixir:awareness-tool",
            scope="leadership",
            member_tag=resolved_tag,
        )
    except Exception as exc:
        log.warning("record_leadership_followup failed: %s", exc)
        return {"error": "record_leadership_followup_failed", "detail": str(exc)}

    attach_tags(memory["memory_id"], ["followup"], actor="elixir:awareness-tool")
    return {
        "success": True,
        "memory_id": memory["memory_id"],
        "member_tag": resolved_tag,
        "type": "followup",
    }


# ── Main dispatch ─────────────────────────────────────────────────────────

def _execute_tool(name, arguments, workflow=None):
    """Execute a tool call and return the result as a string."""
    try:
        if name == "resolve_member":
            result = db.resolve_member(
                arguments["query"],
                limit=arguments.get("limit", 5),
            )
        elif name == "get_member":
            result = _execute_get_member(arguments)
        elif name == "get_member_war_detail":
            result = _execute_get_member_war_detail(arguments)
        elif name == "get_river_race":
            result = _execute_get_river_race(arguments)
        elif name == "get_war_season":
            result = _execute_get_war_season(arguments)
        elif name == "get_war_member_standings":
            result = _execute_get_war_member_standings(arguments)
        elif name == "get_clan_roster":
            result = _execute_get_clan_roster(arguments)
        elif name == "get_clan_health":
            result = _execute_get_clan_health(arguments, workflow=workflow)
        elif name == "get_clan_trends":
            result = _execute_get_clan_trends(arguments)
        elif name == "lookup_cards":
            result = db.lookup_cards(
                name=arguments.get("name"),
                rarity=arguments.get("rarity"),
                min_cost=arguments.get("min_cost"),
                max_cost=arguments.get("max_cost"),
                card_type=arguments.get("card_type"),
                has_evolution=arguments.get("has_evolution"),
                limit=arguments.get("limit", 25),
            )
        elif name == "get_player_details":
            player_tag = _resolve_member_tag(arguments["player_tag"])
            result = cr_api.get_player(player_tag)
        elif name == "cr_api":
            result = _execute_cr_api(arguments)
        elif name == "get_clan_intel_report":
            result = _execute_get_clan_intel_report(arguments)
        elif name == "update_member":
            result = _execute_update_member(arguments)
        elif name == "save_clan_memory":
            from memory_store import attach_tags, create_memory
            from storage.contextual_memory import upsert_member_note_memory

            title = arguments["title"]
            body = arguments["body"]
            tags = arguments.get("tags") or []
            member_tag_input = arguments.get("member_tag")

            # Awareness-loop writes are observations, not leadership decisions.
            # Tag them as elixir_inference with <1.0 confidence so memory
            # readers can tell them apart from human leader notes.
            from_awareness = workflow == "awareness"
            actor = "elixir:awareness-tool" if from_awareness else "leader:elixir-tool"
            source_type = "elixir_inference" if from_awareness else "leader_note"
            is_inference = from_awareness
            confidence = 0.7 if from_awareness else 1.0

            if member_tag_input:
                resolved_tag = _resolve_member_tag(member_tag_input)
                if from_awareness:
                    memory = create_memory(
                        title=title,
                        body=body,
                        summary=body[:220],
                        source_type=source_type,
                        is_inference=is_inference,
                        confidence=confidence,
                        created_by=actor,
                        scope="leadership",
                        member_tag=resolved_tag,
                    )
                else:
                    memory = upsert_member_note_memory(
                        member_tag=resolved_tag,
                        member_label=member_tag_input,
                        note=body,
                        created_by=actor,
                        metadata={"title": title, "tool": "save_clan_memory"},
                    )
                if memory and tags:
                    attach_tags(memory["memory_id"], tags, actor=actor)
                result = {
                    "success": True,
                    "memory_id": memory["memory_id"] if memory else None,
                    "type": "elixir_observation" if from_awareness else "member_note",
                }
            else:
                memory = create_memory(
                    title=title,
                    body=body,
                    summary=body[:220],
                    source_type=source_type,
                    is_inference=is_inference,
                    confidence=confidence,
                    created_by=actor,
                    scope="leadership",
                )
                if tags:
                    attach_tags(memory["memory_id"], tags, actor=actor)
                result = {
                    "success": True,
                    "memory_id": memory["memory_id"],
                    "type": source_type,
                }
        elif name == "flag_member_watch":
            result = _execute_flag_member_watch(arguments)
        elif name == "record_leadership_followup":
            result = _execute_record_leadership_followup(arguments)
        elif name == "schedule_revisit":
            result = _execute_schedule_revisit(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, default=str)
    except Exception as e:
        log.error("Tool execution error (%s): %s", name, e)
        return json.dumps({"error": str(e)})



__all__ = ["_refresh_member_cache", "_resolve_member_tag", "_execute_tool"]
