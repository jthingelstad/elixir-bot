import json
import re
import sqlite3

import cr_api
from agent.core import log
from agent.cr_api_tool import _execute_cr_api
from capabilities import awards as awards_capability
from capabilities import battle_intel as battle_intel_capability
from capabilities import deck_intel as deck_reco_capability
from capabilities import decks as deck_capability
from capabilities import game_modes as game_mode_capability
from capabilities import management as management_capability
from capabilities import members as member_capability
from capabilities import war as war_capability
from storage import events_read as event_facades


class _ModuleProxy:
    def __init__(self, getter):
        self._getter = getter

    def __getattr__(self, name):
        return getattr(self._getter(), name)


def _facade_db():
    # Late-bind through the elixir_agent facade so a test that patches
    # elixir_agent.db intercepts every tool's data access. Function-level
    # import: the facade imports this module, never the other way around.
    import elixir_agent

    return elixir_agent.db


db = _ModuleProxy(_facade_db)


def _resource_constraints_note() -> dict:
    return {
        "gold_known": False,
        "gold_note": "Current gold is not available in Elixir's stored Clash Royale player data.",
    }


def _badge_profile_metrics_summary(profile: dict) -> str | None:
    metric_specs = (
        ("cr_collection_level", "collection level"),
        ("cr_clan_war_wins", "clan war wins"),
        ("cr_battle_wins", "battle wins"),
        ("cr_clan_donations", "clan donations"),
        ("cr_banner_count", "banners"),
        ("cr_emote_count", "emotes"),
    )
    parts = []
    for key, label in metric_specs:
        value = profile.get(key)
        if isinstance(value, int) and value >= 0:
            parts.append(f"{label} {value:,}")
    if not parts:
        return None
    return "Badge-backed profile metrics: " + "; ".join(parts)


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

    profile_badge_summary = _badge_profile_metrics_summary(enriched)
    if profile_badge_summary:
        enriched["profile_badge_metrics_summary"] = profile_badge_summary

    def _parse_json_object(field):
        try:
            value = json.loads(enriched.get(field) or "{}")
        except TypeError, ValueError:
            return None
        return value if isinstance(value, dict) and value else None

    ranked_current = _parse_json_object("current_path_of_legend_season_result_json")
    ranked_last = _parse_json_object("last_path_of_legend_season_result_json")
    ranked_best = _parse_json_object("best_path_of_legend_season_result_json")
    if ranked_current or ranked_last or ranked_best:
        from engine.normalize import ranked_league_name

        def _name_league(block, *, legacy_ok=True):
            if not block:
                return block
            league = block.get("leagueNumber")
            if league is None:
                return block
            block = dict(block)
            # best/last can predate the 2025 rework: values above the current
            # 7-league scheme use the old 10-league Path of Legends scale.
            if legacy_ok and league > 7:
                block["league_name"] = (
                    f"{ranked_league_name(league, legacy=True)} (Path of Legends era)"
                )
            else:
                block["league_name"] = ranked_league_name(league)
            return block

        enriched["ranked_status"] = {
            "current": _name_league(ranked_current, legacy_ok=False),
            "last": _name_league(ranked_last),
            "best": _name_league(ranked_best),
            "wording": "Say Ranked to players; API fields use Path of Legend/pathOfLegend.",
        }
        if ranked_current:
            league = ranked_current.get("leagueNumber")
            trophies = ranked_current.get("trophies")
            rank = ranked_current.get("rank")
            bits = []
            if league is not None:
                bits.append(f"league {league}")
            if trophies is not None:
                bits.append(f"{trophies} ranked trophies")
            if rank is not None:
                bits.append(f"rank #{rank}")
            if bits:
                enriched["ranked_summary"] = "Ranked current season: " + ", ".join(bits)

    progress = _parse_json_object("progress_json")
    if progress:
        enriched["side_mode_progress_keys"] = sorted(str(key) for key in progress.keys())

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


def _slim_card_for_llm(card):
    """Project a normalized card dict down to the fields the LLM actually uses.

    Drops CR-internal IDs, image URLs, duplicate api_* fields, redundant
    evolution numbers, and no-op false/null booleans. Keeps elixirCost inline
    so the LLM doesn't need a second lookup_cards round-trip for swap math.
    """
    if not isinstance(card, dict):
        return card
    slim = {}
    # maxLevel is omitted: every card maxes at 16 on the display scale, so it is a
    # constant, and printing it is how "Lv15/16" reached a member. levels_to_max
    # carries the part that varies.
    for field in ("name", "level", "rarity", "elixirCost", "levels_to_max"):
        value = card.get(field)
        if value is not None:
            slim[field] = value
    if card.get("is_max_level"):
        slim["is_max_level"] = True
    mode_label = card.get("mode_label")
    if mode_label:
        slim["mode_label"] = mode_label
    mode_status_label = card.get("mode_status_label")
    if mode_status_label:
        slim["mode_status_label"] = mode_status_label
    for flag in ("supports_evo", "supports_hero", "evo_unlocked", "hero_unlocked"):
        if card.get(flag):
            slim[flag] = True
    return slim


def _slim_card_list(cards):
    if not isinstance(cards, list):
        return cards
    return [_slim_card_for_llm(card) for card in cards]


# The API's rarity-relative scale. Internal only: a maxed epic is 11 of 11 there
# and 16 of 16 on the player's screen, and the player has never seen the former.
_API_SCALE_FIELDS = ("api_level", "api_max_level")


def _scrub_api_scale(value):
    """Remove the rarity-relative level fields from anything bound for the model.

    _slim_card_for_llm has always dropped these, but it was only ever wired to
    get_member's current deck — the card tool itself never called it, so every
    card it returned carried api_level/api_max_level beside the display values.
    The model read the pair as two meaningful scales and told a member his Wall
    Breakers were "display Lv15/16, normalized 10/11", then invented an authority
    for it ("the normalized level Supercell uses to compare across rarities").
    His Wall Breakers are level 15. There is one number and the player knows it.

    Recursive rather than field-by-field on purpose: the leak happened because a
    projection had to be remembered at each call site, and one was not.
    """
    if isinstance(value, dict):
        return {k: _scrub_api_scale(v) for k, v in value.items() if k not in _API_SCALE_FIELDS}
    if isinstance(value, list):
        return [_scrub_api_scale(v) for v in value]
    return value


def _enrich_war_player_type(result, tag):
    """Add war_player_type classification to a result dict by player tag."""
    from db import get_connection
    from storage.war_analytics import _war_player_type

    canon = tag if tag.startswith("#") else f"#{tag}"
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM players WHERE player_tag = ?",
            (canon,),
        ).fetchone()
        if row:
            result["war_player_type"] = _war_player_type(conn, canon)
    finally:
        conn.close()


def _annotate_roster_status(result, member_tag):
    """Flag a departed member on a member-facing result dict (QA H6/M20/L18) so
    their stats aren't read as a current-roster player / active war no-show."""
    if not isinstance(result, dict):
        return result
    status = db.member_roster_status(member_tag)
    result.update(status)
    if status.get("roster_status") == "departed":
        result["departed_note"] = (
            f"This member left the clan on {status.get('left_at')}; their stats are "
            "historical and any current-roster / war-attendance attribution is not live."
        )
    return result


def _enrich_war_player_types(members):
    """Add war_player_type to each member dict in a list."""
    from db import get_connection
    from storage.war_analytics import war_player_types_by_tag

    tags = [member.get("tag") or member.get("player_tag") or "" for member in members]
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
    if player is not None:
        db.snapshot_player_profile(player, expected_tag=member_tag)
    else:
        log.warning(
            "player_profile_refresh_skipped tag=%s reason=cr_api_returned_none",
            member_tag,
        )
    if include_battles:
        battles = cr_api.get_player_battle_log(member_tag)
        if battles is not None:
            db.snapshot_player_battlelog(member_tag, battles)
        else:
            log.warning(
                "player_battlelog_refresh_skipped tag=%s reason=cr_api_returned_none",
                member_tag,
            )


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
    choices = ", ".join(
        m.get("member_ref_with_handle") or m.get("current_name") or m["player_tag"]
        for m in matches[:3]
    )
    log.warning(
        "member_resolution_ambiguous query=%r top_score=%d second_score=%d choices=%s",
        query,
        top.get("match_score", 0),
        second.get("match_score", 0),
        choices,
    )
    raise ValueError(f"Ambiguous member reference '{query}'. Top matches: {choices}")


# ── Member domain execution ───────────────────────────────────────────────


def _memory_viewer_scope_for_workflow(workflow: str | None) -> str:
    if workflow in {
        "clanops",
        "channel_update_leadership",
        "screenshot_readout",
        "awareness",
        "memory_synthesis",
    }:
        return "leadership"
    return "public"


def _execute_get_member(arguments, workflow=None):
    """Execute the consolidated get_member tool."""
    member_tag = _resolve_member_tag(arguments["member_tag"])
    include = arguments.get("include") or ["profile", "form"]
    scope = arguments.get("scope", "competitive_10")
    days = arguments.get("days", 30)

    needs_battles = any(a in include for a in ("form", "deck", "war", "battles"))
    _refresh_member_cache(member_tag, include_battles=needs_battles)

    facets = {
        item
        for item in include
        if item
        in {
            "profile",
            "form",
            "war",
            "trend",
            "losses",
            "battles",
            "history",
            "ranked",
            "mode_activity",
            "awards",
        }
    }
    if "profile" in include or "form" in include:
        facets.add("playstyle")
    if "deck" in include:
        facets.add("loadout")
    member_read = member_capability.get_member_intelligence(
        member_tag,
        facets=sorted(facets),
        days=days,
        scope=scope,
        battles_scope=arguments.get("battles_scope", "overall_10"),
        battles_limit=arguments.get("battles_limit", 10),
        losses_limit=arguments.get("losses_limit", 30),
        source=db,
    )
    result = {}

    if "profile" in include:
        result["profile"] = _enrich_member_profile(member_read.get("profile"))

    if "form" in include:
        result["form"] = member_read.get("form")

    if "profile" in include or "form" in include:
        if member_read.get("playstyle") is not None:
            result["playstyle"] = member_read["playstyle"]

    if "war" in include:
        result["war"] = member_read.get("war")

    if "trend" in include:
        result["trend"] = member_read.get("trend")

    if "deck" in include:
        loadout = member_read.get("loadout") or {}
        current_deck = loadout.get("current_deck")
        if isinstance(current_deck, dict):
            current_deck = dict(current_deck)
            current_deck["cards"] = _slim_card_list(current_deck.get("cards"))
            current_deck["support_cards"] = _slim_card_list(current_deck.get("support_cards"))
        result["current_deck"] = current_deck
        result["signature_cards"] = loadout.get("signature_cards")

    if "cards" in include:
        result["card_collection"] = {
            "error": "deprecated_include",
            "hint": (
                "include=['cards'] was removed because the full collection routinely "
                "overflowed context. Use get_member_cards(view='profile') for a compact "
                "digest or get_member_cards(view='lookup', filter=...) for a targeted slice."
            ),
        }

    if "losses" in include:
        result["losses"] = member_read.get("losses")

    if "battles" in include:
        result["battles"] = member_read.get("battles")

    if "history" in include:
        result["history"] = member_read.get("history")

    if "ranked" in include:
        result["ranked"] = member_read.get("ranked")

    if "mode_activity" in include:
        result["mode_activity"] = member_read.get("mode_activity")

    if "memories" in include:
        from memory_store import list_memories

        memories = list_memories(
            viewer_scope=_memory_viewer_scope_for_workflow(workflow),
            filters={"member_tag": member_tag},
            limit=15,
        )
        if not memories:
            result["memories"] = {
                "member_tag": member_tag,
                "memories": [],
                "message": "No stored memories for this member.",
            }
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

    if "awards" in include:
        result["awards"] = member_read.get("awards")

    return _scrub_api_scale(result)


def _execute_get_deck_intelligence(arguments, workflow=None):
    view = arguments.get("view", "member")
    member_tag = arguments.get("member_tag")
    if view == "card_impact" and workflow not in {
        "clanops",
        "channel_update_leadership",
    }:
        return {"error": "The 'card_impact' analysis is only available in leadership channels."}
    if view == "member" or (view == "card_impact" and member_tag):
        if not member_tag:
            return {"error": "member_tag_required", "view": view}
        member_tag = _resolve_member_tag(member_tag)
        _refresh_member_cache(member_tag, include_battles=True)
    result = deck_capability.get_deck_intelligence(
        view=view,
        player_tag=member_tag,
        cards=arguments.get("cards"),
        changes=arguments.get("changes"),
        days=arguments.get("days", 30),
        scope=arguments.get("scope", "competitive"),
        source=db,
    )
    if view == "member" and isinstance(result, dict) and member_tag:
        _annotate_roster_status(result, member_tag)
    return result


def _execute_get_deck_recommendations(arguments):
    """Forward-looking deck recommendations, gated by ownership and card level."""
    view = arguments.get("view", "discover")
    member_tag = arguments.get("member_tag")
    if not member_tag:
        return {"error": "member_tag_required", "view": view}
    return deck_reco_capability.get_deck_recommendations(
        view=view,
        member_tag=_resolve_member_tag(member_tag),
        card=arguments.get("card"),
        anchors=arguments.get("anchors"),
        count=arguments.get("count"),
        require=arguments.get("require"),
        limit=arguments.get("limit", 6),
    )


def _execute_read_deck_link(arguments):
    """Resolve a deck-share link a member pasted into chat."""
    return deck_reco_capability.read_deck_link(
        link=arguments.get("link"),
        member_tag=(
            _resolve_member_tag(arguments["member_tag"]) if arguments.get("member_tag") else None
        ),
    )


def _execute_get_battle_intelligence(arguments):
    """Computed battle intelligence (Feature 1). Reads the enriched tables the
    Stage-A worker fills; member_tag is required only for the per-member views."""
    view = arguments.get("view", "battle")
    member_tag = arguments.get("member_tag")
    if member_tag:
        member_tag = _resolve_member_tag(member_tag)
    elif view in {"battle", "member_summary", "deck", "coaching", "newcomer"}:
        return {"error": "member_tag_required", "view": view}
    return battle_intel_capability.get_battle_intelligence(
        view=view,
        member_tag=member_tag,
        card=arguments.get("card"),
        scope=arguments.get("scope", "all"),
        limit=arguments.get("limit", 20),
        days=arguments.get("days"),
        source=db,
    )


def _execute_get_member_cards(arguments):
    """Execute the single card-collection tool.

    ``profile`` owns broad collection questions; ``lookup`` owns targeted
    slices. The former public names remain dispatcher aliases for compatibility
    but are no longer advertised to any workflow.
    """
    view = arguments.get("view") or "profile"
    if view not in {"profile", "lookup"}:
        return {"error": "unknown_member_cards_view", "view": view}

    member_tag = _resolve_member_tag(arguments["member_tag"])
    if view == "lookup":
        card_filter = arguments.get("filter")
        if not isinstance(card_filter, dict) or not card_filter:
            return {
                "error": "member_cards_filter_required",
                "hint": "view='lookup' requires a non-empty filter",
            }
        include_battles = bool(card_filter.get("mode") == "war" or card_filter.get("deck"))
        _refresh_member_cache(member_tag, include_battles=include_battles)
        result = db.lookup_member_cards(
            member_tag,
            filter=card_filter,
            limit=arguments.get("limit", 20),
        )
        _annotate_roster_status(result, member_tag)
        return _scrub_api_scale(result)

    _refresh_member_cache(member_tag, include_battles=False)
    result = db.get_member_card_profile(member_tag)
    if result is None:
        return {
            "error": "no_collection_snapshot",
            "member_tag": member_tag,
            "hint": "No card collection snapshot exists yet for this member.",
        }
    _annotate_roster_status(result, member_tag)
    if isinstance(result, dict):
        result["upgrade_cost_note"] = (
            "cards_required / ready-to-upgrade counts come from a static "
            "card-cost table, not live game state; treat them as close "
            "estimates, and gold to actually upgrade is unknown."
        )
    return _scrub_api_scale(result)


def _execute_get_game_mode_performance(arguments):
    """One named game mode: the member's record plus the clan leaderboard.

    Exists because the grouped rollups could not answer "how am I doing in
    C.H.A.O.S Draft League" — every special event collapsed into one bucket, so
    Elixir truthfully told a member it could not tell him, while the data held
    134 of his battles at 57%.
    """
    from capabilities.game_modes import get_game_mode_performance

    mode = (arguments.get("mode") or "").strip()
    days = arguments.get("days")
    try:
        days = int(days) if days is not None else 90
    except TypeError, ValueError:
        days = 90
    return get_game_mode_performance(
        mode,
        player_tag=arguments.get("member_tag"),
        days=max(1, min(days, 365)),
    )


def _execute_get_awards(arguments):
    """Execute the get_awards tool — filtered list, per-member leaderboard,
    or current-season standings across the awards table."""
    mode = (arguments.get("mode") or "list").strip().lower()
    award_type = arguments.get("award_type")
    season_id = arguments.get("season_id")
    rank = arguments.get("rank")
    limit = arguments.get("limit")

    if mode == "current_standings":
        return awards_capability.get_awards_recognition(
            view="current_standings", season_id=season_id, source=db
        )["data"]

    if mode == "leaderboard":
        if not award_type:
            raise ValueError("get_awards(mode='leaderboard') requires award_type")
        return awards_capability.get_awards_recognition(
            view="leaderboard",
            award_type=award_type,
            rank=rank,
            limit=limit,
            source=db,
        )["data"]

    member_tag = arguments.get("member_tag")
    resolved_tag = _resolve_member_tag(member_tag) if member_tag else None
    return awards_capability.get_awards_recognition(
        view="list",
        award_type=award_type,
        season_id=season_id,
        rank=rank,
        member_tag=resolved_tag,
        limit=limit,
        source=db,
    )["data"]


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
        _annotate_roster_status(result, member_tag)

    return result


# ── River Race domain execution ────────────────────────────────────────────


def _execute_get_river_race(arguments):
    """Execute the consolidated get_river_race tool."""
    aspect = arguments.get("aspect", "standings")
    snapshot = war_capability.get_war_intelligence(source=db)

    if aspect == "standings":
        if not snapshot.get("available"):
            return {"error": "No active war data available."}
        weekly = snapshot["weekly_race"]
        daily = snapshot["daily_race"]
        projection = snapshot["projection"]
        period = snapshot["period"]
        return {
            "game_truth": snapshot.get("game_truth"),
            "primary_metric": projection.get("primary_metric"),
            "race_standings": weekly.get("standings") or [],
            "race_rank": weekly.get("rank"),
            "finish_line": weekly.get("finish_line"),
            "boat_scored": weekly.get("scored"),
            "day_standings": daily.get("standings") or [],
            "day_rank": daily.get("rank"),
            "day_scored": daily.get("scored"),
            "period_points": daily.get("period_points"),
            "projected_day_fame": projection.get("projected_day_fame"),
            "projected_defense_fame": projection.get("projected_defense_fame"),
            "projected_fame_at_close": projection.get("projected_fame_at_close"),
            "defenses_remaining": projection.get("defenses_remaining"),
            "clinches_finish_today": projection.get("clinches_finish_today"),
            "season_week_label": period.get("season_week_label"),
            "is_colosseum_week": period.get("is_colosseum_week"),
            "is_final_battle_day": period.get("is_final_battle_day"),
            "is_final_practice_day": period.get("is_final_practice_day"),
            "trophy_stakes_text": period.get("trophy_stakes_text"),
            "observed_at": snapshot.get("observed_at"),
        }

    if aspect == "engagement":
        from agent.war_render import render_war_now

        if not snapshot.get("available"):
            return {"error": "No active war data available."}
        data = dict(snapshot.get("clock") or {})
        data["game_truth"] = snapshot.get("game_truth")
        data["now_text"] = render_war_now(data)
        engagement = snapshot.get("engagement") or {}
        remaining = engagement.get("remaining_decks") or {}

        def _legacy_member(member):
            return {
                "member_ref": member.get("member_ref"),
                "tag": member.get("player_tag"),
                "decks_used_today": member.get("decks_used_today"),
                "decks_used_total": member.get("decks_used_total"),
                "points_today": member.get("points_today"),
            }

        data.update(
            {
                "war_day_key": engagement.get("war_day_key"),
                "observed_at": engagement.get("observed_at"),
                "clan_fame": engagement.get("clan_fame"),
                "clan_period_points": engagement.get("clan_period_points"),
                "day_rank": engagement.get("day_rank"),
                "total_participants": engagement.get("total_participants"),
                "engaged_count": engagement.get("engaged_count"),
                "finished_count": engagement.get("finished_count"),
                "untouched_count": engagement.get("untouched_count"),
                "partial_deck_participant_count": remaining.get("partial"),
                "participants_with_decks_left_count": remaining.get("total"),
                "remaining_deck_participants": remaining,
                "top_points_total": engagement.get("top_points_total"),
                "used_all_4": [_legacy_member(m) for m in engagement.get("used_all_4") or []],
                "used_some": [_legacy_member(m) for m in engagement.get("used_some") or []],
                "used_none": [_legacy_member(m) for m in engagement.get("used_none") or []],
            }
        )
        return data

    return {"error": f"Unknown aspect: {aspect}"}


def _execute_get_war_season(arguments):
    """Execute the consolidated get_war_season tool."""
    aspect = arguments.get("aspect", "summary")
    season_id = arguments.get("season_id")
    limit = arguments.get("limit", 10)

    result = war_capability.get_war_season_view(
        view=aspect,
        season_id=season_id,
        metric=arguments.get("metric", "points"),
        limit=limit,
        source=db,
    )["data"]
    if aspect == "standings" and isinstance(result, dict):
        members = result.get("members") or result.get("standings") or result.get("results") or []
        _enrich_war_player_types(members)
    return result


# ── Clan domain execution ─────────────────────────────────────────────────


def _donations_this_week(limit: int = 10) -> dict:
    """Top donors this week (CR resets the counter Monday). Fail-open."""
    try:
        conn = _facade_db().get_connection()
    except Exception:
        return {"note": "donation data unavailable", "top_donors_this_week": []}
    try:
        rows = conn.execute(
            """SELECT COALESCE(p.display_name, p.current_name) AS name,
                      COALESCE(pcs.donations_week, 0) AS donated,
                      COALESCE(pcs.donations_received_week, 0) AS received
               FROM player_current_state pcs
               JOIN players p ON p.player_tag = pcs.player_tag
               JOIN clan_memberships cm ON cm.player_tag = pcs.player_tag
                    AND cm.left_at IS NULL
               ORDER BY donated DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {
            "note": "donations reset Monday; these are THIS WEEK's counts",
            "top_donors_this_week": [dict(r) for r in rows],
        }
    except Exception:
        return {"note": "donation data unavailable", "top_donors_this_week": []}
    finally:
        conn.close()


_ROSTER_LIST_KEYS = (
    "member_ref",
    "player_tag",
    "role",
    "roster_status",
    "trophies",
    "clan_rank",
    "donations_week",
    "donations_received_week",
    "war_points_rank_season",
    "joined_date",
    "elder_eligible",
    "in_discord",
    "cr_games_per_day",
)


def _compact_roster_member(member: dict) -> dict:
    """Roster 'list' view: keep the roster-relevant fields and drop the ~28 noisy
    cr_* internals / *_updated_at timestamps. All 50 members with the full dict
    was ~63K chars and overflowed the tool-result cap, dropping the whole members
    list. The brain drills into a member's deep CR stats via get_member."""
    return {k: member[k] for k in _ROSTER_LIST_KEYS if k in member}


def _execute_get_clan_roster(arguments):
    """Execute the consolidated get_clan_roster tool."""
    aspect = arguments.get("aspect", "list")
    days = arguments.get("days", 30)
    limit = arguments.get("limit", 10)

    if aspect == "list":
        # Rehearsal 2026-07-04: the model read donations_week as "lifetime"
        # and refused the question — spell the semantics out in-band.
        return {
            "note": (
                "donations_week / donations_received_week are THIS WEEK's "
                "counts (CR resets them Monday); they are fully answerable "
                "for any asker — not leadership-restricted."
            ),
            "members": [_compact_roster_member(m) for m in db.list_members()],
        }
    elif aspect == "card_owners":
        from storage import cards as _cards_storage

        return _cards_storage.list_card_owners(
            arguments.get("card_name") or "",
            maxed_only=bool(arguments.get("maxed_only", True)),
        )
    elif aspect == "donations":
        # Compact this-week leaderboard — the full list truncates past the
        # tool char limit and the members block gets dropped (rehearsal
        # 2026-07-04: the model could never see weekly donation counts).
        return _donations_this_week(limit)
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
    elif aspect == "trends":
        window_days = arguments.get("window_days", 7)
        comparison = db.compare_clan_trend_windows(window_days=window_days)
        summary = db.build_clan_trend_summary_context(
            days=days,
            window_days=window_days,
        )
        if isinstance(comparison, dict):
            comparison["trend_summary"] = summary
            return comparison
        return {"comparison": comparison, "trend_summary": summary}
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
        return management_capability.get_management_decisions(
            view="at_risk", arguments=arguments, source=db
        )["data"]
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
        return management_capability.get_management_decisions(
            view="promotion_candidates", source=db
        )["data"]
    else:
        return {"error": f"Unknown aspect: {aspect}"}


def _execute_get_clan_game_modes(arguments):
    aspect = arguments.get("aspect", "summary")
    days = arguments.get("days", 30)
    limit = arguments.get("limit", 10)
    mode_group = arguments.get("mode_group")
    if aspect == "ranked":
        mode_group = "ranked"
    elif aspect == "events":
        mode_group = "special_event"
    snapshot = game_mode_capability.get_clan_game_modes(
        days=days,
        mode_group=mode_group,
        limit=limit,
    )
    modes = list((snapshot.get("modes") or {}).values())
    if aspect == "ranked":
        return {
            "aspect": aspect,
            "window_days": snapshot["window_days"],
            "mode_mix": modes,
            "ranked_activity": snapshot["ranked"]["activity"],
            "ranked_profiles": snapshot["ranked"]["profiles"],
            "current_standings": snapshot["ranked"]["standings"],
        }
    if aspect == "duos":
        return {
            "aspect": aspect,
            "window_days": snapshot["window_days"],
            # QA L14: the JOIN is against `players` (not the active roster), so a
            # teammate who has LEFT the clan but is still known WILL appear;
            # only never-seen external teammates are absent.
            "note": "pairs are directional (player's own logged battles); a teammate "
            "who has since left the clan can still appear, but a teammate never "
            "seen in our data (a random / non-roster player) does not",
            "duos": snapshot["duos"],
        }
    if aspect == "side_modes":
        # Progress is a bounded current-state projection. Keys remain opaque
        # labels, so an empty list means no active profile reported a key, not
        # that an unlisted game surface was necessarily inactive.
        side_progress = snapshot["side_modes"]["progress"]
        leaderboards = snapshot["side_modes"]["leaderboards"]
        return {
            "aspect": aspect,
            "window_days": snapshot["window_days"],
            "side_mode_progress": side_progress,
            "leaderboards": leaderboards,
            "mode_mix": modes,
            "side_mode_progress_tracked": snapshot["side_modes"]["progress_tracked"],
            "leaderboards_tracked": snapshot["side_modes"]["leaderboards_tracked"],
            "note": "Progress keys are tracked as opaque labels; an empty list means no active profile reported a key. Leaderboards remain untracked, so use mode_mix for battle activity.",
        }
    if aspect == "events":
        return {
            "aspect": aspect,
            "window_days": snapshot["window_days"],
            "event_activity": snapshot["events"]["activity"],
            "event_participation": snapshot["events"]["participation"],
            "event_badge_completions": snapshot["events"]["badge_completions"],
            "active_events": snapshot["events"]["active"],
            "mode_mix": modes,
        }
    return {
        "aspect": aspect,
        "window_days": snapshot["window_days"],
        "mode_group": snapshot["mode_group"],
        "by_group": modes,
        "by_game_mode": snapshot["game_modes"],
        "ranked_activity": snapshot["ranked"]["activity"],
        "ranked_profiles": snapshot["ranked"]["profiles"],
        "side_mode_progress": snapshot["side_modes"]["progress"],
        "event_participation": snapshot["events"]["participation"],
        "event_badge_completions": snapshot["events"]["badge_completions"],
        "active_events": snapshot["events"]["active"],
        "leaderboards": snapshot["side_modes"]["leaderboards"],
        "capability": snapshot["capability"],
        "contract_version": snapshot["contract_version"],
    }


_ELIXIR_STATE_WINDOWS = (7, 28, 56, 90)


def _state_limit(arguments, *, default: int = 25, maximum: int = 100) -> int:
    try:
        value = int(arguments.get("limit", default))
    except TypeError, ValueError:
        return default
    if value < 1:
        return default
    return min(value, maximum)


def _state_windows(arguments) -> tuple[int, ...]:
    raw = arguments.get("windows")
    if not raw:
        return _ELIXIR_STATE_WINDOWS
    windows = []
    for item in raw:
        try:
            days = int(item)
        except TypeError, ValueError:
            continue
        if 1 <= days <= 90:
            windows.append(days)
    return tuple(dict.fromkeys(windows)) or _ELIXIR_STATE_WINDOWS


def _state_days(arguments) -> int:
    try:
        days = int(arguments.get("days", 7))
    except TypeError, ValueError:
        return 7
    return min(max(days, 1), 90)


def _workflow_can_read_leadership_state(workflow: str | None) -> bool:
    return _memory_viewer_scope_for_workflow(workflow) == "leadership"


def _state_scope(arguments, workflow: str | None) -> tuple[str | None, dict | None]:
    requested = (arguments.get("scope") or "").strip()
    if not _workflow_can_read_leadership_state(workflow):
        if requested and requested != "public":
            return None, {
                "error": "leadership_state_unavailable",
                "detail": "Only leadership workflows can read leadership or all-scope Elixir state.",
            }
        return "public", None
    if requested in {"", "all"}:
        return None, None
    if requested in {"public", "leadership", "system_internal"}:
        return requested, None
    return None, {
        "error": "invalid_scope",
        "detail": f"Unknown state scope: {requested}",
    }


def _require_leadership_state(workflow: str | None) -> dict | None:
    if _workflow_can_read_leadership_state(workflow):
        return None
    return {
        "error": "leadership_state_unavailable",
        "detail": "This Elixir state view is available only in leadership workflows.",
    }


def _execute_get_elixir_state(arguments, workflow=None):
    """Read current event, awareness, and decision state with scope gates."""
    aspect = arguments.get("aspect", "operational_summary")
    limit = _state_limit(arguments)

    if aspect == "event_summary":
        scope, error = _state_scope(arguments, workflow)
        if error:
            return error
        result = event_facades.summarize_event_windows(
            windows=_state_windows(arguments),
            scope=scope,
            subject_key=arguments.get("subject_key"),
        )
        if isinstance(result, dict):
            # QA M15: signal-event counts reflect the ~7-day event stream, while
            # battles_mirrored spans the full battle history — the coverages
            # differ. event_class is NOT applied here (streams are signal events).
            result["coverage_note"] = (
                "signal-event counts cover only the recent event stream (~7d); "
                "battles_mirrored spans the full battle history — different coverage. "
                "event_class does not filter this view; use aspect='game_modes' for battles."
            )
        return result

    if aspect == "recent_events":
        scope, error = _state_scope(arguments, workflow)
        if error:
            return error
        out = {
            "scope": scope or "all",
            "days": _state_days(arguments),
            "events": event_facades.list_recent_events(
                days=_state_days(arguments),
                scope=scope,
                event_type=arguments.get("event_type"),
                subject_key=arguments.get("subject_key"),
                limit=limit,
            ),
        }
        return out

    if aspect == "game_modes":
        return game_mode_capability.get_clan_game_mode_windows(
            windows=_state_windows(arguments),
            top_members=int(arguments.get("top_members") or 5),
        )

    if aspect == "season_window":
        return db.get_season_window()

    if aspect == "war_season":
        view = arguments.get("war_view") or "snapshot"
        kwargs = {"view": view, "source": db}
        if view in {"summary", "history"}:
            kwargs["limit"] = limit
        return {"war_season": war_capability.get_war_season_view(**kwargs)["data"]}

    leadership_error = _require_leadership_state(workflow)
    if leadership_error:
        return leadership_error

    if aspect == "awareness_activity":
        return db.get_awareness_activity(limit=limit)

    if aspect == "leader_actions":
        status = (arguments.get("status") or "").strip() or "proposed"
        return {
            "actions": db.list_leader_actions(
                status=None if status == "all" else status,
                limit=limit,
            )
        }

    if aspect == "operational_summary":
        # A dashboard, not a data dump. Every block is structurally bounded so
        # the result stays under the tool envelope's char cap as data grows —
        # otherwise the envelope blindly drops whole arrays mid-deliberation.
        action_limit = min(limit, 10)
        return {
            "event_windows": event_facades.summarize_event_windows(
                windows=_ELIXIR_STATE_WINDOWS, scope=None
            ),
            "recent_events": event_facades.list_recent_events(days=7, limit=10),
            "game_modes": game_mode_capability.get_clan_game_mode_windows(windows=(7,)),
            "war_season": war_capability.get_war_season_view(view="snapshot", source=db)["data"],
            "leader_actions": db.list_leader_actions(status="proposed", limit=action_limit),
            "awareness": db.get_awareness_activity(limit=min(limit, 15)),
        }

    return {"error": f"Unknown aspect: {aspect}"}


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
            raise ValueError('birthday value must be {"month": M, "day": D}')
        db.set_member_birthday(member_tag, name=None, month=month, day=day)
    elif field == "join_date":
        db.set_member_join_date(member_tag, name=None, joined_date=str(value))
    elif field == "profile_url":
        db.set_member_profile_url(member_tag, name=None, url=str(value))
    elif field == "note":
        db.set_member_note(member_tag, name=None, note=str(value))
    elif field == "nickname":
        # Leader override — pins a readable name Elixir prefers over the game
        # name everywhere. Empty value clears it (back to the auto-cleaned name).
        nickname = str(value).strip() or None
        db.set_member_nickname(member_tag, nickname, source="leader")
    else:
        return {"error": f"Unknown field: {field}"}

    return {"success": True, "field": field}


def _execute_get_clan_intel_report(arguments):
    """Build a threat analysis for a competitor in our current river race.

    Wraps storage.opponent_intel.build_clan_intel_entry so the scheduled Intel
    Report (and conversational scouting) runs through normal tool plumbing.
    """
    from datetime import datetime, timezone

    from storage.opponent_intel import build_clan_intel_entry, war_day_context

    raw_tag = arguments.get("clan_tag")
    try:
        clan_tag = cr_api._normalize_cr_tag(raw_tag)
    except cr_api.InvalidTagError as exc:
        return {"error": "invalid_tag", "detail": str(exc)}

    war = cr_api.get_current_war()
    if not war:
        return {
            "error": "no_active_war",
            "hint": "Our clan is not currently in a river race.",
        }

    war_clans = list(war.get("clans") or [])
    our_war_entry = war.get("clan")
    our_tag_hash = f"#{cr_api.CLAN_TAG}"
    if our_war_entry:
        war_clans = [our_war_entry] + [
            c for c in war_clans if (c.get("tag") or "").upper() != our_tag_hash.upper()
        ]

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

    # QA M23: anchor the cumulative war numbers to which battle day they cover,
    # and note that war participant_count (this week's war roster) is not the
    # same population as roster member_count (the clan's full member list).
    entry["war_context"] = war_day_context(war)
    # QA L20: roster activity counts (recently_active_count) are relative to now;
    # stamp when this snapshot was read so the brain can age it.
    entry["observed_at"] = datetime.now(timezone.utc).isoformat()
    war_block = entry.get("war") or {}
    roster_block = entry.get("roster") or {}
    p_count = war_block.get("participant_count")
    m_count = roster_block.get("member_count") if roster_block else None
    if p_count is not None and m_count is not None and p_count != m_count:
        entry["participation_note"] = (
            f"{p_count} members entered this week's war vs {m_count} in the clan roster — "
            "war participant_count counts only those who joined the river race, "
            "not full clan membership."
        )
    return entry


def _normalize_away_until(value) -> str | None:
    """Normalize an LLM-supplied leave date to a comparable UTC timestamp.

    A bare date means the member is away THROUGH that day, so it resolves to that
    day's end — "back on the 3rd" must not expire at 00:00 on the 3rd. Returns
    None when the value cannot be read as a date, so the caller can refuse
    instead of writing a hold that protects nobody.
    """
    from datetime import datetime, timezone

    text = str(value or "").strip().replace("Z", "").replace(" ", "T")
    if not text:
        return None
    if len(text) == 10:  # bare date → end of that day
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
        return f"{text}T23:59:59Z"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    away_until = (arguments.get("away_until") or "").strip()

    if not member_tag_input or not reason:
        return {"error": "flag_member_watch requires member_tag and reason"}
    if arguments.get("case_type"):
        return {
            "error": "unsupported_case_type",
            "detail": (
                "flag_member_watch records private watch/leave state only; use "
                "record_leadership_followup with member_tag + action_type for a review card."
            ),
        }

    resolved_tag = _resolve_member_tag(member_tag_input)
    # A leave HOLD (member told leaders they're away) is a `Hold:` memory that
    # pauses the inactivity/kick clock until `away_until` — distinct from a plain
    # `Watch:` note, which only observes idleness and never grants grace. The
    # kick engine's LOA guard (_has_leadership_hold) matches the `Hold:` prefix,
    # NOT `Watch:`. Grace expires with the hold, so the clock resumes on return.
    is_hold = bool(away_until)
    title = f"{'Hold' if is_hold else 'Watch'}: {resolved_tag}"
    body = f"{reason}"
    if is_hold:
        normalized = _normalize_away_until(away_until)
        if normalized is None:
            # Refuse rather than write a hold that silently protects nobody. The
            # guard compares this value as a time; an unparseable shape used to
            # be stored anyway, the card told the leader the leave was recorded,
            # and the member was left fully exposed to the kick clock.
            return {
                "error": "invalid_away_until",
                "detail": (
                    f"Could not read {away_until!r} as a date. Use an ISO date or "
                    "datetime, e.g. '2026-08-03' or '2026-08-03T12:00:00Z'."
                ),
            }
        expires_at = normalized
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

    attach_tags(
        memory["memory_id"],
        ["leave-hold"] if is_hold else ["watch-list"],
        actor="elixir:awareness-tool",
    )
    result = {
        "success": True,
        "memory_id": memory["memory_id"],
        "member_tag": resolved_tag,
        "type": "hold" if is_hold else "watch",
    }
    if is_hold:
        result["away_until"] = away_until
    return result


def _execute_raise_clan_chat_relay(arguments):
    """Raise an in-game clan-chat relay card to #actions so a leader can paste
    a short message into the game's clan chat — the surface that reaches EVERY
    member, not just the Discord subset. Creates a ``proposed`` ``in_game_relay``
    leader-action recommendation; the pending-card poster surfaces it to #actions.

    The copy runs through the SAME deterministic clan-chat guardrails the
    awareness brain's voicings do (<=200 chars, no markdown/links/@mentions, no
    Supercell-filtered terms). If it misses them, nothing is raised and the
    caller is asked to reword — never post unsigned/invalid copy.
    """
    import hashlib

    from runtime.clan_chat_copy import signed_valid_messages
    from runtime.leader_action_ui import CLASH_COPY_MAX_LENGTH, LEADER_ACTION_UI_VERSION

    copy_in = (arguments.get("copy") or arguments.get("message") or "").strip()
    reason = (arguments.get("reason") or "").strip()
    member_tag_input = (arguments.get("member_tag") or "").strip()

    if not copy_in:
        return {"error": "raise_clan_chat_relay requires `copy` (the clan-chat message)"}

    copies = signed_valid_messages(copy_in, max_chars=CLASH_COPY_MAX_LENGTH)
    if not copies:
        return {
            "error": "clan_chat_copy_rejected",
            "detail": (
                f"Copy missed the clan-chat guardrails (<={CLASH_COPY_MAX_LENGTH} chars, "
                "plain text — no markdown/links/@mentions — and no filtered terms like '&' "
                "or '+<digits>'). Reword (e.g. 'and' for '&') and retry."
            ),
        }
    copy_text = "\n".join(c.strip() for c in copies if c and c.strip())
    resolved_tag = _resolve_member_tag(member_tag_input) if member_tag_input else None

    key_hash = hashlib.sha1(copy_text.encode("utf-8")).hexdigest()[:12]
    objective = f"interactive_relay:{key_hash}"
    action_key = f"interactive-relay:{key_hash}"

    try:
        baseline = db.build_leader_action_baseline(
            action_type="in_game_relay", target_player_tag=resolved_tag
        )
        action = db.create_leader_action_recommendation(
            action_type="in_game_relay",
            objective=objective,
            prompt_text=f"Paste this clan-chat note: {copy_text}",
            rationale=reason or "Leader-requested clan-chat relay",
            target_channel_key="actions",
            target_player_tag=resolved_tag,
            source_signal_key=action_key,
            source_signal_type="interactive_relay",
            copy_original_text=copy_text,
            copy_current_text=copy_text,
            baseline=baseline,
            action_key=action_key,
            ui_version=LEADER_ACTION_UI_VERSION,
        )
    except Exception as exc:
        log.warning("raise_clan_chat_relay failed: %s", exc)
        return {"error": "raise_clan_chat_relay_failed", "detail": str(exc)}

    return {
        "success": True,
        "action_id": (action or {}).get("action_id"),
        "clan_chat_copy": copy_text,
        "posts_to": "#actions",
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


def _execute_schedule_followup(arguments):
    """Carry an intention forward (Agentic Loop v2, Phase 5).

    Writes a row and nothing else. When it comes due the engine tick emits a
    `followup_due` event and the ordinary wake path takes over — this tool
    schedules Elixir, it does not schedule a message.
    """
    from storage.dossiers import schedule_followup

    due_at = str(arguments.get("due_at") or "").strip()
    why = str(arguments.get("why") or "").strip()
    if not due_at or not why:
        return {"error": "schedule_followup requires due_at and why"}
    member_tag = (arguments.get("member_tag") or "").strip() or None
    if member_tag:
        resolved = _resolve_member_tag(member_tag)
        if resolved:
            member_tag = resolved
    try:
        followup_id = schedule_followup(
            due_at=due_at, why=why, player_tag=member_tag, created_by="agent"
        )
    except Exception as exc:
        log.warning("schedule_followup failed: %s", exc)
        return {"error": "could not schedule the follow-up"}
    if not followup_id:
        return {"error": "schedule_followup requires due_at and why"}
    return {
        "scheduled": True,
        "followup_id": followup_id,
        "due_at": due_at,
        "member_tag": member_tag,
        "note": "You will be woken with this note at that time and can decide then.",
    }


def _execute_record_leadership_followup(arguments):
    """Awareness-loop observation: record an operational note.

    Persists as a leadership-scoped, inference-typed memory with the
    ``followup`` tag. If ``member_tag`` is provided, the memory is scoped to
    that member so the member-context view surfaces it.

    A NOTE IS NOT AN ESCALATION. This raises a #actions card only when
    ``action_type`` AND ``member_tag`` are both supplied; otherwise nothing
    reaches a human and the result says so (``escalated``).
    """
    from memory_store import attach_tags, create_memory

    topic = (arguments.get("topic") or "").strip()
    recommendation = (arguments.get("recommendation") or "").strip()
    member_tag_input = arguments.get("member_tag")
    action_type = (arguments.get("action_type") or "").strip()
    revisit_at = (arguments.get("revisit_at") or "").strip()
    signal_key = (arguments.get("signal_key") or "").strip()
    away_until = (arguments.get("away_until") or "").strip()

    if not topic or not recommendation:
        return {"error": "record_leadership_followup requires topic and recommendation"}
    if away_until and not member_tag_input:
        return {
            "error": "away_until_requires_member",
            "detail": "A leave of absence pauses one member's kick clock — name the member.",
        }
    if bool(revisit_at) != bool(signal_key):
        return {
            "error": "revisit_requires_time_and_signal",
            "detail": "revisit_at and signal_key must be supplied together",
        }
    if revisit_at:
        from storage.revisits import _normalize_due_at

        try:
            _normalize_due_at(revisit_at)
        except ValueError as exc:
            return {"error": "invalid_revisit", "detail": str(exc)}
    if action_type and action_type not in {
        "kick_recommendation",
        "promotion_recommendation",
        "demotion_recommendation",
    }:
        return {"error": "unsupported_action_type", "action_type": action_type}

    resolved_tag = _resolve_member_tag(member_tag_input) if member_tag_input else None

    # A leave of absence is a `Hold: <tag>` memory with an expiry. That exact
    # title prefix is what engine.management._has_leadership_hold matches to
    # pause a member's kick clock — `Followup:` is not, and neither is `Watch:`.
    # This capability used to live on flag_member_watch, which the trim to 14
    # tools moved out of the advertised set, leaving CLAN.md promising a hold
    # that nothing could record. Folded in here rather than re-adding a
    # near-duplicate write tool: the model reached for this one every time
    # anyway (12 uses to flag_member_watch's 0 in 464 offers).
    expires_at = None
    if away_until:
        expires_at = _normalize_away_until(away_until)
        if expires_at is None:
            # Refuse rather than write a hold that silently protects nobody.
            return {
                "error": "invalid_away_until",
                "detail": (
                    f"Could not read {away_until!r} as a date. Use an ISO date or "
                    "datetime, e.g. '2026-08-03' or '2026-08-03T12:00:00Z'."
                ),
            }

    title = f"Hold: {resolved_tag}" if away_until else f"Followup: {topic}"
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
            expires_at=expires_at,
        )
    except Exception as exc:
        log.warning("record_leadership_followup failed: %s", exc)
        return {"error": "record_leadership_followup_failed", "detail": str(exc)}

    tags = ["followup", "leave-hold"] if away_until else ["followup"]
    attach_tags(memory["memory_id"], tags, actor="elixir:awareness-tool")
    # #216: the action board is the only active decision store. The previous
    # path wrote a decision case and merely described it as a card; no runtime
    # consumer actually turned that case into an R card. Create the human-visible
    # action directly, with a stable per-member/type key so an automated write
    # cannot reopen a leader-closed decision.
    action = None
    if action_type and resolved_tag:
        try:
            action = db.create_leader_action_recommendation(
                action_type=action_type,
                objective=f"Followup: {topic}",
                prompt_text=recommendation,
                rationale=recommendation,
                target_player_tag=resolved_tag,
                source_signal_key=f"awareness:followup:{action_type}:{resolved_tag}",
                source_signal_type="awareness_leadership_followup",
                action_key=f"awareness:followup:{action_type}:{resolved_tag}",
            )
        except Exception as exc:
            log.warning("record_leadership_followup action upsert failed: %s", exc)
    escalated = bool(action and action.get("status") == "proposed")
    result = {
        "success": True,
        "memory_id": memory["memory_id"],
        "member_tag": resolved_tag,
        "type": "leave_hold" if away_until else "followup",
        # Say plainly whether this reached leadership. Without it the tool reads
        # as an escalation in every case, which is how observations went
        # unanswered for weeks while the brain believed it had reported them.
        "escalated": escalated,
    }
    if away_until:
        # Report the parsed value, not the input — the model should confirm the
        # date the kick clock will actually resume on, not the one it guessed.
        result["hold_until"] = expires_at
        result["note"] = (
            f"Kick clock paused for {resolved_tag} until {expires_at}. "
            "The hold expires on its own; no card was raised."
        )
    if action:
        result["action_id"] = action.get("action_id")
        result["action_key"] = action.get("action_key")
        result["action_status"] = action.get("status")
        if not escalated:
            result["note"] = "Leadership already decided this member review; no card was reopened."
    else:
        result["note"] = (
            "Recorded as a leadership memory only — no leader has been asked anything. "
            "If this needs a human to act, post it to the leader-lounge lane (#leaders), "
            "or call again with action_type + member_tag to raise a #actions card."
        )
    if revisit_at:
        from storage.revisits import schedule_revisit

        try:
            revisit = schedule_revisit(
                signal_key=signal_key,
                due_at=revisit_at,
                rationale=recommendation,
                created_by_workflow="awareness",
            )
            result["revisit"] = {
                "revisit_id": revisit.get("revisit_id"),
                "signal_key": revisit.get("signal_key"),
                "due_at": revisit.get("due_at"),
            }
        except sqlite3.Error as exc:
            log.warning("record_leadership_followup revisit failed: %s", exc)
            result["revisit_error"] = str(exc)
    return result


_REFERENCE_RE = re.compile(r"^\s*([rlm])?\s*#?(\d+)\s*$", re.IGNORECASE)
_REFERENCE_KIND_BY_LETTER = {
    "r": "leader_action",
    "l": "loop",
    "m": "memory",
}


def _execute_lookup_reference(arguments, workflow=None):
    """Resolve one of Elixir's own shorthand codes to its record: 'R<n>' (leader
    action), 'L<n>' (awareness loop), or 'M<n>' (memory).
    Elixir authors these codes, so a leader who cites one in chat is pointing at a
    real row — resolve it rather than guess."""
    raw = str(arguments.get("reference") or "").strip()
    match = _REFERENCE_RE.match(raw)
    if not match:
        return {
            "error": "unparseable_reference",
            "reference": raw,
            "hint": "Expected a code like 'R137' (leader action), 'L60' (loop), "
            "or 'M340' (memory).",
        }
    letter, number = match.group(1), int(match.group(2))
    kind = (
        _REFERENCE_KIND_BY_LETTER.get(letter.lower())
        if letter
        else (arguments.get("kind") or "").strip().lower() or None
    )
    if kind is None:
        return {
            "error": "ambiguous_reference",
            "reference": raw,
            "hint": "Bare number with no R/L/M prefix — pass kind="
            "'leader_action' | 'loop' | 'memory'.",
        }

    if kind == "case":
        return {
            "error": "retired_reference_kind",
            "reference": f"C{number}",
            "hint": "Decision cases were discarded; current leadership decisions use R<n>.",
        }

    if kind == "memory":
        from memory_store import get_memory

        mem = get_memory(
            number,
            viewer_scope=_memory_viewer_scope_for_workflow(workflow),
            include_archived=True,
        )
        if not mem:
            return {
                "error": "not_found",
                "reference": f"M{number}",
                "hint": f"No memory M{number} exists (or it's out of scope here).",
            }
        return {
            "reference": f"M{number}",
            "kind": "memory",
            "memory_id": mem.get("memory_id"),
            "memory_kind": mem.get("kind"),
            "title": mem.get("title"),
            "body": mem.get("body"),
            "summary": mem.get("summary"),
            "scope": mem.get("scope"),
            "member_tag": mem.get("member_tag"),
            "status": mem.get("status"),
            "created_by": mem.get("created_by"),
            "created_at": mem.get("created_at"),
            "updated_at": mem.get("updated_at"),
            "tags": mem.get("tags"),
        }

    if kind == "leader_action":
        action = db.get_leader_action_by_id(number)
        if not action:
            return {
                "error": "not_found",
                "reference": f"R{number}",
                "hint": f"No leader-action recommendation R{number} exists.",
            }
        return {
            "reference": f"R{number}",
            "kind": "leader_action",
            "action_id": action.get("action_id"),
            "action_type": action.get("action_type"),
            "status": action.get("status"),
            "target_member": action.get("target_player_name"),
            "target_tag": action.get("target_player_tag"),
            "objective": action.get("objective"),
            "rationale": action.get("rationale"),
            "clan_chat_copy": action.get("copy_current_text") or action.get("copy_original_text"),
            "proposed_at": action.get("proposed_at"),
            "decided_at": action.get("decided_at"),
            "decided_by_discord_user_id": action.get("decided_by_discord_user_id"),
            "decision_note": action.get("decision_note"),
            "outcome": action.get("outcome"),
        }

    if kind == "loop":
        loop = db.get_awareness_loop_by_number(number)
        if not loop:
            return {
                "error": "not_found",
                "reference": f"L{number}",
                "hint": f"No awareness loop L{number} exists yet.",
            }
        return {"reference": f"L{number}", "kind": "loop", **loop}

    return {"error": "unknown_kind", "reference": raw, "kind": kind}


# ── Main dispatch ─────────────────────────────────────────────────────────

ADVERTISED_TOOL_EXECUTOR_NAMES = frozenset(
    {
        "resolve_member",
        "get_member",
        "get_member_war_detail",
        "get_river_race",
        "get_clan_roster",
        "get_elixir_state",
        "get_deck_intelligence",
        "get_deck_recommendations",
        "read_deck_link",
        "get_battle_intelligence",
        "lookup_cards",
        "get_member_cards",
        "cr_api",
        "save_clan_memory",
        "record_leadership_followup",
        "schedule_followup",
        "get_awards",
        "get_game_mode_performance",
        "lookup_reference",
    }
)

# Chassis surface tools (Agentic Loop v2): executable and dispatched, but
# deliberately NOT advertised. "Advertised" means reachable through ALL_TOOLS by
# any workflow that shares the standard toolset; a posting tool is handed out
# per-turn by agent.chassis.surface_tools, gated on the attention's surfaces.
# Keeping them out of ADVERTISED is what lets the entrypoint smoke test keep
# asserting that every advertised tool is one a shared workflow may call.
SURFACE_TOOL_EXECUTOR_NAMES = frozenset({"post_to_discord", "post_to_clan_chat"})

# Old names remain executable for persisted traces and direct compatibility
# tests, but no workflow advertises them to the model.
LEGACY_TOOL_EXECUTOR_NAMES = frozenset(
    {
        "get_war_season",
        "get_clan_health",
        "get_clan_game_modes",
        "get_member_card_profile",
        "lookup_member_cards",
        "get_clan_intel_report",
        "update_member",
        "flag_member_watch",
        "raise_clan_chat_relay",
        "schedule_revisit",
    }
)
TOOL_EXECUTOR_NAMES = (
    ADVERTISED_TOOL_EXECUTOR_NAMES | LEGACY_TOOL_EXECUTOR_NAMES | SURFACE_TOOL_EXECUTOR_NAMES
)


def _execute_post_to_discord(arguments):
    """Validate a composed post and stage it for delivery.

    Staging, not sending: the post reaches members through
    ``runtime.awareness.deliver.deliver_posts`` after the turn, which is the one
    path that owns the hard-post floor, the durable outbox, idempotency and the
    clan-chat sibling. Delivering here would be a second delivery path — the
    thing the v4 architecture died of.

    What DOES happen here is the check, because immediate feedback is the whole
    reason posting is a tool call: a rejection returns the reason to the model,
    which fixes it and calls again. The rules are the failure modes measured in
    the 2026-08-04 scoped-composer experiment, not guesses.
    """
    from agent import chassis
    from agent.post_validation import PostRejected, validate_discord_post

    staging = chassis.active_staging()
    if staging is None:
        return json.dumps({"error": "post_to_discord is only available inside a chassis turn"})
    lane = str(arguments.get("lane") or "").strip()
    if lane not in ("announcements", "elixir"):
        return json.dumps({"error": f"unknown lane {lane!r}; use announcements or elixir"})
    # The turn may only post where its job declared it speaks. The tool schema
    # offers both lanes to every turn, so without this a role-change job — which
    # is announcements-only by a month of measured editorial judgment — could put
    # a roster fact in #elixir and break the strict Discord split. Allowed
    # surfaces are registry data on the Attention; this is where they bind.
    allowed = {
        chassis._DISCORD_SURFACES[surface]
        for surface in chassis._DISCORD_SURFACES
        if surface in staging.attention.surfaces
    }
    if lane not in allowed:
        return json.dumps(
            {
                "error": "lane_not_available",
                "reason": f"this job posts to {sorted(allowed) or 'no Discord lane'}, not {lane!r}",
            }
        )
    try:
        from runtime.emoji import available_emoji_names

        known = {name.lower() for name in available_emoji_names()}
        content = validate_discord_post(
            str(arguments.get("content") or ""),
            lane=lane,
            known_emoji=known,
            repairs=staging.repairs,
        )
    except PostRejected as exc:
        staging.rejections.append(str(exc))
        return json.dumps({"error": "post_rejected", "reason": str(exc)})
    post = staging.stage_discord(
        lane=lane,
        content=content,
        covers=arguments.get("covers_signal_keys") or [],
    )
    return json.dumps(
        {
            "accepted": True,
            "lane": lane,
            "characters": len(content),
            "covers": post["covers_signal_keys"],
            "note": "Staged for delivery. Post the in-game clan-chat sibling if this "
            "moment matters to the whole clan.",
        }
    )


def _execute_post_to_clan_chat(arguments):
    """Validate and attach the in-game line to its Discord sibling."""
    from agent import chassis
    from agent.post_validation import PostRejected, validate_clan_chat_post

    staging = chassis.active_staging()
    if staging is None:
        return json.dumps({"error": "post_to_clan_chat is only available inside a chassis turn"})
    try:
        content = validate_clan_chat_post(
            str(arguments.get("content") or ""), repairs=staging.repairs
        )
    except PostRejected as exc:
        staging.rejections.append(str(exc))
        return json.dumps({"error": "post_rejected", "reason": str(exc)})

    from runtime.clan_chat_copy import signed_valid_messages

    signed = signed_valid_messages([content])
    if not signed:
        reason = (
            "The clan-chat line did not survive in-game validation (links, mentions, "
            "markdown, engine internals, or empty after signing). Write one plain "
            "sentence a player reads mid-game, then call the tool again."
        )
        staging.rejections.append(reason)
        return json.dumps({"error": "post_rejected", "reason": reason})

    post = staging.attach_clan_chat(content)
    if post is None:
        return json.dumps(
            {
                "error": "no_discord_post_yet",
                "reason": "The in-game line rides alongside its Discord sibling. "
                "Call post_to_discord first, then call this.",
            }
        )
    return json.dumps(
        {
            "accepted": True,
            "characters": len(signed[0]),
            "signed_preview": signed[0],
            "note": "Staged. A '- E' signature is appended for you.",
        }
    )


def _execute_tool(name, arguments, workflow=None):
    """Execute a tool call and return the result as a string."""
    if name not in TOOL_EXECUTOR_NAMES:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        if name == "post_to_discord":
            return _execute_post_to_discord(arguments)
        if name == "post_to_clan_chat":
            return _execute_post_to_clan_chat(arguments)
        if name == "resolve_member":
            # QA M1: don't hard-filter to active — a recently-departed or observed
            # player must resolve (each match carries its own status field) rather
            # than returning [] as if they never existed.
            result = db.resolve_member(
                arguments["query"],
                status=None,
                limit=arguments.get("limit", 5),
            )
        elif name == "get_member":
            result = _execute_get_member(arguments, workflow=workflow)
        elif name == "get_member_war_detail":
            result = _execute_get_member_war_detail(arguments)
        elif name == "get_awards":
            result = _execute_get_awards(arguments)
        elif name == "get_game_mode_performance":
            result = _execute_get_game_mode_performance(arguments)
        elif name == "lookup_reference":
            result = _execute_lookup_reference(arguments, workflow=workflow)
        elif name == "get_river_race":
            result = _execute_get_river_race(arguments)
        elif name == "get_war_season":
            result = _execute_get_war_season(arguments)
        elif name == "get_clan_roster":
            result = _execute_get_clan_roster(arguments)
        elif name == "get_clan_health":
            result = _execute_get_clan_health(arguments, workflow=workflow)
        elif name == "get_clan_game_modes":
            result = _execute_get_clan_game_modes(arguments)
        elif name == "get_elixir_state":
            result = _execute_get_elixir_state(arguments, workflow=workflow)
        elif name == "get_deck_intelligence":
            result = _execute_get_deck_intelligence(arguments, workflow=workflow)
        elif name == "get_deck_recommendations":
            result = _execute_get_deck_recommendations(arguments)
        elif name == "read_deck_link":
            result = _execute_read_deck_link(arguments)
        elif name == "get_battle_intelligence":
            result = _execute_get_battle_intelligence(arguments)
        elif name == "lookup_cards":
            result = db.lookup_cards(
                name=arguments.get("name"),
                rarity=arguments.get("rarity"),
                min_cost=arguments.get("min_cost"),
                max_cost=arguments.get("max_cost"),
                card_type=arguments.get("card_type"),
                role=arguments.get("role"),
                has_evolution=arguments.get("has_evolution"),
                limit=arguments.get("limit", 25),
            )
        elif name == "get_member_cards":
            result = _execute_get_member_cards(arguments)
        elif name == "get_member_card_profile":
            result = _execute_get_member_cards({**arguments, "view": "profile"})
        elif name == "lookup_member_cards":
            result = _execute_get_member_cards({**arguments, "view": "lookup"})
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

            # QA M28: the awareness create_memory path was an unconditional INSERT
            # (the leader path upserts) — dedup a repeated identical observation
            # via a content-hash event key so ticks don't pile up duplicates.
            import hashlib as _hashlib

            _dedup_id = _hashlib.sha1(
                f"{title}|{member_tag_input or ''}|{body}".encode("utf-8")
            ).hexdigest()[:16]

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
                        event_type="awareness_obs",
                        event_id=_dedup_id,
                        idempotent=True,
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
                    event_type="awareness_obs" if from_awareness else None,
                    event_id=_dedup_id if from_awareness else None,
                    idempotent=from_awareness,
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
        elif name == "raise_clan_chat_relay":
            result = _execute_raise_clan_chat_relay(arguments)
        elif name == "record_leadership_followup":
            result = _execute_record_leadership_followup(arguments)
        elif name == "schedule_followup":
            result = _execute_schedule_followup(arguments)
        elif name == "schedule_revisit":
            result = _execute_schedule_revisit(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result, default=str)
    except Exception as e:
        log.exception("Tool execution error (%s): %s", name, e)
        return json.dumps({"error": str(e)})


execute_tool = _execute_tool

__all__ = [
    "ADVERTISED_TOOL_EXECUTOR_NAMES",
    "LEGACY_TOOL_EXECUTOR_NAMES",
    "TOOL_EXECUTOR_NAMES",
    "execute_tool",
]
