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
    from storage.war_analytics import _war_player_type
    from db import get_connection

    conn = get_connection()
    try:
        for member in members:
            tag = member.get("tag") or member.get("player_tag") or ""
            if not tag:
                continue
            canon = tag if tag.startswith("#") else f"#{tag}"
            row = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?", (canon,),
            ).fetchone()
            if row:
                member["war_player_type"] = _war_player_type(conn, row["member_id"])
    finally:
        conn.close()


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
        elif name == "update_member":
            result = _execute_update_member(arguments)
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
