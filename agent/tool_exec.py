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


def _refresh_member_cache(member_tag, include_battles=False):
    """Refresh stored player profile and optionally battle log for a member."""
    player = cr_api.get_player(member_tag)
    if player:
        db.snapshot_player_profile(player)
    if include_battles:
        battles = cr_api.get_player_battle_log(member_tag)
        if battles:
            db.snapshot_player_battlelog(member_tag, battles)


def _resolve_member_tag(value):
    """Accept a tag, name, alias, or Discord handle and return a canonical player tag."""
    query = (value or "").strip()
    if not query:
        raise ValueError("member reference is required")
    if query.startswith("#"):
        return query
    if re.fullmatch(r"[0289PYLQGRJCUV]{3,15}", query.upper()):
        return f"#{query.upper()}"

    matches = db.resolve_member(query, limit=5)
    if not matches:
        raise ValueError(f"Could not resolve member reference: {query}")
    exactish = [m for m in matches if m.get("match_score", 0) >= 850]
    if len(exactish) == 1:
        return exactish[0]["player_tag"]
    if len(matches) == 1:
        return matches[0]["player_tag"]
    top = matches[0]
    second = matches[1]
    if (top.get("match_score", 0) - second.get("match_score", 0)) >= 100:
        return top["player_tag"]
    choices = ", ".join(m.get("member_ref_with_handle") or m.get("current_name") or m["player_tag"] for m in matches[:3])
    raise ValueError(f"Ambiguous member reference '{query}'. Top matches: {choices}")


def _execute_tool(name, arguments):
    """Execute a tool call and return the result as a string."""
    try:
        if name == "resolve_member":
            result = db.resolve_member(
                arguments["query"],
                limit=arguments.get("limit", 5),
            )
        elif name == "get_clan_roster_summary":
            result = db.get_clan_roster_summary()
        elif name == "list_clan_members":
            result = db.list_members()
        elif name == "list_longest_tenure_members":
            result = db.list_longest_tenure_members(
                limit=arguments.get("limit", 10),
            )
        elif name == "list_recent_joins":
            result = db.list_recent_joins(
                days=arguments.get("days", 30),
            )
        elif name == "get_member_profile":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = _enrich_member_profile(db.get_member_profile(member_tag))
        elif name == "get_member_overview":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = _enrich_member_profile(db.get_member_overview(member_tag))
        elif name == "get_member_recent_form":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_recent_form(
                member_tag,
                scope=arguments.get("scope", "competitive_10"),
            )
        elif name == "get_member_current_deck":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=False)
            result = db.get_member_current_deck(member_tag)
        elif name == "get_member_card_collection":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=False)
            result = _enrich_member_card_collection(
                db.get_member_card_collection(
                    member_tag,
                    limit=arguments.get("limit", 60),
                    min_level=arguments.get("min_level"),
                    include_support=arguments.get("include_support", True),
                    rarity=arguments.get("rarity"),
                )
            )
        elif name == "get_member_next_chests":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = cr_api.get_player_chests(member_tag)
        elif name == "get_member_signature_cards":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_signature_cards(
                member_tag,
                mode_scope=arguments.get("mode_scope", "overall"),
            )
        elif name == "get_members_with_most_level_16_cards":
            result = db.get_members_with_most_level_16_cards(
                limit=arguments.get("limit", 10),
            )
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
        elif name == "get_member_history":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_history(
                member_tag,
                days=arguments.get("days", 30),
            )
        elif name == "compare_member_trend_windows":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.compare_member_trend_windows(
                member_tag,
                window_days=arguments.get("window_days", 7),
            )
        elif name == "get_member_trend_summary":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.build_member_trend_summary_context(
                member_tag,
                days=arguments.get("days", 30),
                window_days=arguments.get("window_days", 7),
            )
        elif name == "compare_clan_trend_windows":
            result = db.compare_clan_trend_windows(
                window_days=arguments.get("window_days", 7),
            )
        elif name == "get_clan_trend_summary":
            result = db.build_clan_trend_summary_context(
                days=arguments.get("days", 30),
                window_days=arguments.get("window_days", 7),
            )
        elif name == "get_member_war_stats":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_war_stats(member_tag)
        elif name == "get_member_war_status":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_war_status(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_member_war_attendance":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_war_attendance(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_member_war_battle_record":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=True)
            result = db.get_member_war_battle_record(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_current_war_status":
            result = db.get_current_war_status()
        elif name == "get_current_war_day_state":
            result = db.get_current_war_day_state()
        elif name == "get_war_season_summary":
            result = db.get_war_season_summary(
                season_id=arguments.get("season_id"),
                top_n=arguments.get("top_n", 5),
            )
        elif name == "get_war_deck_status_today":
            result = db.get_war_deck_status_today()
        elif name == "get_members_without_war_participation":
            result = db.get_members_without_war_participation(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_trending_war_contributors":
            result = db.get_trending_war_contributors(
                season_id=arguments.get("season_id"),
                recent_races=arguments.get("recent_races", 2),
                limit=arguments.get("limit", 5),
            )
        elif name == "get_promotion_candidates":
            result = db.get_promotion_candidates()
        elif name == "compare_member_war_to_clan_average":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.compare_member_war_to_clan_average(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_members_at_risk":
            result = db.get_members_at_risk(
                inactivity_days=arguments.get("inactivity_days", 7),
                min_donations_week=arguments.get("min_donations_week", 20),
                require_war_participation=arguments.get("require_war_participation", False),
                min_war_races=arguments.get("min_war_races", 1),
                tenure_grace_days=arguments.get("tenure_grace_days", 14),
                season_id=arguments.get("season_id"),
            )
        elif name == "get_members_on_hot_streak":
            result = db.get_members_on_hot_streak(
                min_streak=arguments.get("min_streak", 4),
                scope=arguments.get("scope", "ladder_ranked_10"),
            )
        elif name == "get_members_on_losing_streak":
            result = db.get_members_on_losing_streak(
                min_streak=arguments.get("min_streak", 3),
                scope=arguments.get("scope", "competitive_10"),
            )
        elif name == "get_trophy_drops":
            result = db.get_trophy_drops(
                days=arguments.get("days", 7),
                min_drop=arguments.get("min_drop", 100),
            )
        elif name == "get_player_details":
            player_tag = _resolve_member_tag(arguments["player_tag"])
            result = cr_api.get_player(player_tag)
        elif name == "get_war_champ_standings":
            result = db.get_war_champ_standings(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_war_battle_win_rates":
            result = db.get_war_battle_win_rates(
                season_id=arguments.get("season_id"),
                limit=arguments.get("limit", 10),
                min_battles=arguments.get("min_battles", 1),
            )
        elif name == "get_clan_boat_battle_record":
            result = db.get_clan_boat_battle_record(
                wars=arguments.get("wars", 3),
            )
        elif name == "get_war_score_trend":
            result = db.get_war_score_trend(
                days=arguments.get("days", 30),
            )
        elif name == "compare_fame_per_member_to_previous_season":
            result = db.compare_fame_per_member_to_previous_season(
                season_id=arguments.get("season_id"),
            )
        elif name == "get_recent_role_changes":
            result = db.get_recent_role_changes(
                days=arguments.get("days", 30),
            )
        elif name == "get_member_missed_war_days":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            result = db.get_member_missed_war_days(
                member_tag,
                season_id=arguments.get("season_id"),
            )
        elif name == "get_perfect_war_participants":
            result = db.get_perfect_war_participants(
                season_id=arguments.get("season_id"),
            )
        elif name == "set_member_birthday":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_birthday(
                member_tag, name=None,
                month=arguments["month"], day=arguments["day"],
            )
            result = {"success": True}
        elif name == "set_member_join_date":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_join_date(
                member_tag, name=None,
                joined_date=arguments["date"],
            )
            result = {"success": True}
        elif name == "set_member_profile_url":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_profile_url(
                member_tag, name=None,
                url=arguments["url"],
            )
            result = {"success": True}
        elif name == "set_member_note":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            db.set_member_note(
                member_tag, name=None,
                note=arguments["note"],
            )
            result = {"success": True}
        elif name == "save_clan_memory":
            from memory_store import attach_tags, create_memory
            from storage.contextual_memory import upsert_member_note_memory

            title = arguments["title"]
            body = arguments["body"]
            tags = arguments.get("tags") or []
            member_tag_input = arguments.get("member_tag")

            if member_tag_input:
                resolved_tag = _resolve_member_tag(member_tag_input)
                memory = upsert_member_note_memory(
                    member_tag=resolved_tag,
                    member_label=member_tag_input,
                    note=body,
                    created_by="leader:elixir-tool",
                    metadata={"title": title, "tool": "save_clan_memory"},
                )
                if memory and tags:
                    attach_tags(memory["memory_id"], tags, actor="leader:elixir-tool")
                result = {"success": True, "memory_id": memory["memory_id"] if memory else None, "type": "member_note"}
            else:
                memory = create_memory(
                    title=title,
                    body=body,
                    summary=body[:220],
                    source_type="leader_note",
                    is_inference=False,
                    confidence=1.0,
                    created_by="leader:elixir-tool",
                    scope="leadership",
                )
                if tags:
                    attach_tags(memory["memory_id"], tags, actor="leader:elixir-tool")
                result = {"success": True, "memory_id": memory["memory_id"], "type": "leader_note"}
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, default=str)
    except Exception as e:
        log.error("Tool execution error (%s): %s", name, e)
        return json.dumps({"error": str(e)})



__all__ = ["_refresh_member_cache", "_resolve_member_tag", "_execute_tool"]
