import json
import re

import cr_api

from agent.core import log
from agent.cr_api_tool import _execute_cr_api
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
        except (TypeError, ValueError):
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
    for field in ("name", "level", "maxLevel", "rarity", "elixirCost", "levels_to_max"):
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


def _enrich_war_player_type(result, tag):
    """Add war_player_type classification to a result dict by player tag."""
    from storage.war_analytics import _war_player_type
    from db import get_connection

    canon = tag if tag.startswith("#") else f"#{tag}"
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM players WHERE player_tag = ?", (canon,),
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

def _memory_viewer_scope_for_workflow(workflow: str | None) -> str:
    if workflow in {
        "clanops",
        "channel_update_leadership",
        "arena_relay_observation",
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

    result = {}

    if "profile" in include:
        result["profile"] = _enrich_member_profile(db.get_member_profile(member_tag))

    if "form" in include:
        result["form"] = db.get_member_recent_form(member_tag, scope=scope)

    if "profile" in include or "form" in include:
        # Deterministic mode-mix identity (engine/profiles) — grounded
        # personality facts; the battery showed member answers never
        # mentioned Ranked identities without this (cases 26/30).
        try:
            from engine import profiles as _profiles

            conn = db.get_connection()
            try:
                result["playstyle"] = _profiles.player_mode_profile(conn, member_tag)
            finally:
                conn.close()
        except Exception:
            pass

    if "war" in include:
        result["war"] = db.get_member_war_status(member_tag, season_id=None)

    if "trend" in include:
        result["trend"] = db.build_member_trend_summary_context(
            member_tag, days=days, window_days=min(days // 4, 7) or 7,
        )

    if "deck" in include:
        current_deck = db.get_member_current_deck(member_tag)
        if isinstance(current_deck, dict):
            current_deck = dict(current_deck)
            current_deck["cards"] = _slim_card_list(current_deck.get("cards"))
            current_deck["support_cards"] = _slim_card_list(current_deck.get("support_cards"))
        result["current_deck"] = current_deck
        result["signature_cards"] = db.get_member_signature_cards(
            member_tag, mode_scope="overall",
        )

    if "cards" in include:
        result["card_collection"] = {
            "error": "deprecated_include",
            "hint": (
                "include=['cards'] was removed because the full collection routinely "
                "overflowed context. Use get_member_card_profile for a compact digest, "
                "or lookup_member_cards(filter=...) for a targeted slice."
            ),
        }

    if "losses" in include:
        result["losses"] = db.get_member_recent_losses(
            member_tag,
            scope=scope,
            limit=arguments.get("losses_limit", 30),
        )

    if "battles" in include:
        result["battles"] = db.get_member_recent_battles(
            member_tag,
            scope=arguments.get("battles_scope", "overall_10"),
            limit=arguments.get("battles_limit", 10),
        )

    if "history" in include:
        result["history"] = db.get_member_history(member_tag, days=days)

    if "ranked" in include:
        result["ranked"] = db.get_member_ranked_status(member_tag, days=days)

    if "mode_activity" in include:
        result["mode_activity"] = db.get_member_mode_activity(member_tag, days=days)

    if "memories" in include:
        from memory_store import list_memories

        memories = list_memories(
            viewer_scope=_memory_viewer_scope_for_workflow(workflow),
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

    if "awards" in include:
        result["awards"] = db.get_member_trophy_case(member_tag)

    return result


def _execute_get_awards(arguments):
    """Execute the get_awards tool — filtered list, per-member leaderboard,
    or current-season standings across the awards table."""
    mode = (arguments.get("mode") or "list").strip().lower()
    award_type = arguments.get("award_type")
    season_id = arguments.get("season_id")
    rank = arguments.get("rank")
    limit = arguments.get("limit")

    if mode == "current_standings":
        target_season = int(season_id) if season_id is not None else None
        standings = db.get_season_awards_standings(season_id=target_season)
        if isinstance(standings, dict):
            standings = dict(standings)
            standings["freshness"] = _war_standings_freshness(target_season)
            # QA M26: these are LIVE in-progress standings. The durable awards
            # ledger (mode='list') is written only at season close, so list()
            # can show 0 for an active season — not a contradiction.
            standings["provisional"] = True
            standings["source_note"] = (
                "live in-progress standings; awards are committed to the ledger "
                "(mode='list') only when the season closes."
            )
        return standings

    if mode == "leaderboard":
        if not award_type:
            raise ValueError("get_awards(mode='leaderboard') requires award_type")
        results = db.award_leaderboard(
            award_type=award_type,
            rank=int(rank) if rank is not None else None,
            limit=int(limit) if limit is not None else 20,
        )
        return {
            "mode": "leaderboard",
            "filters": {
                "award_type": award_type,
                "rank": int(rank) if rank is not None else None,
            },
            "count": len(results),
            "results": results,
        }

    member_tag = arguments.get("member_tag")
    resolved_tag = _resolve_member_tag(member_tag) if member_tag else None
    results = db.list_awards(
        award_type=award_type,
        season_id=int(season_id) if season_id is not None else None,
        rank=int(rank) if rank is not None else None,
        member_tag=resolved_tag,
        limit=int(limit) if limit is not None else 100,
    )
    cap = int(limit) if limit is not None else 100
    return {
        "mode": "list",
        "filters": {
            "member_tag": resolved_tag,
            "award_type": award_type,
            "season_id": int(season_id) if season_id is not None else None,
            "rank": int(rank) if rank is not None else None,
        },
        "count": len(results),
        # QA L25: a capped result reads as complete without this.
        "truncated": len(results) >= cap,
        # QA M27: season_id is overloaded — war seasons are small integers
        # (e.g. 129-134); Path-of-Legends champ awards use a YYYYMM month
        # (e.g. 202606). Filtering by a war integer silently excludes pol awards
        # for the same period.
        "season_id_note": "season_id mixes war-season integers (129-134) and Path-of-Legends YYYYMM months (e.g. 202606); a war-integer filter excludes pol awards.",
        "results": results,
    }


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

def _compact_deck_row(member: dict) -> dict:
    """Slim a war-day participant to identity + deck/points counts for the
    get_river_race engagement tool. The full member-reference dict (donation
    ranks, elder flags, war_points_rank, etc.) is irrelevant to 'who has decks
    left today' and, times ~50 members, overflowed the tool-result char cap."""
    return {
        "member_ref": member.get("member_ref") or member.get("name"),
        "tag": member.get("tag"),
        "decks_used_today": member.get("decks_used_today"),
        "decks_used_total": member.get("decks_used_total"),
        "points_today": member.get("points_today"),
    }


def _remaining_deck_participant_summary(day_state: dict) -> dict:
    total_participants = int(day_state.get("total_participants") or 0)
    finished_count = int(day_state.get("finished_count") or 0)
    untouched_count = int(day_state.get("untouched_count") or 0)

    used_some = day_state.get("used_some")
    if isinstance(used_some, list):
        partial_count = len(used_some)
    else:
        partial_count = max(0, total_participants - finished_count - untouched_count)

    participants_with_decks_left = max(0, total_participants - finished_count)
    if partial_count + untouched_count != participants_with_decks_left:
        partial_count = max(0, participants_with_decks_left - untouched_count)

    return {
        "total": participants_with_decks_left,
        "partial": partial_count,
        "untouched": untouched_count,
        "finished": finished_count,
        "total_participants": total_participants,
        "count_source": "used_some + used_none",
        "summary": (
            f"{participants_with_decks_left} participants have decks left today: "
            f"{untouched_count} untouched, {partial_count} partial."
        ),
    }


def _execute_get_river_race(arguments):
    """Execute the consolidated get_river_race tool."""
    aspect = arguments.get("aspect", "standings")

    if aspect == "standings":
        war_status = db.get_current_war_status()
        if not isinstance(war_status, dict):
            return {"error": "No active war data available."}
        # Two separate races, never coalesced (see storage/war_status.py):
        #  - race_standings (by fame) = the WEEKLY boat race that decides the week
        #  - day_standings (by period_points) = TODAY's race, resets each day
        # Compare within one race only; never a clan's period points vs another's
        # fame. primary_metric names which race decides this week.
        return {
            "primary_metric": war_status.get("primary_metric"),
            "race_standings": war_status.get("race_standings", []),
            "race_rank": war_status.get("race_rank"),
            "finish_line": war_status.get("finish_line"),
            "boat_scored": war_status.get("boat_scored"),
            "day_standings": war_status.get("day_standings", []),
            "day_rank": war_status.get("day_rank"),
            "day_scored": war_status.get("day_scored"),
            "period_points": war_status.get("period_points"),
            "projected_day_fame": war_status.get("projected_day_fame"),
            # Boat defenses add fame too (from the API's periodLogs) — fold them
            # into "will we finish today". clinches_finish_today = we cross the
            # finish line at today's close (winning the week early) if this rank +
            # defenses hold.
            "projected_defense_fame": war_status.get("projected_defense_fame"),
            "projected_fame_at_close": war_status.get("projected_fame_at_close"),
            "defenses_remaining": war_status.get("defenses_remaining"),
            "clinches_finish_today": war_status.get("clinches_finish_today"),
            "season_week_label": war_status.get("season_week_label"),
            "is_colosseum_week": db.is_colosseum_week_confirmed(
                war_status.get("period_type"),
                war_status.get("trophy_change"),
                trophy_stakes_known=bool(war_status.get("trophy_stakes_known")),
            ),
            "is_final_battle_day": bool(war_status.get("final_battle_day_active")),
            "is_final_practice_day": bool(war_status.get("final_practice_day_active")),
            "trophy_stakes_text": war_status.get("trophy_stakes_text"),
            # QA M7: freshness — the race baseline is polled, not live-to-the-second.
            "observed_at": war_status.get("observed_at"),
        }

    if aspect == "engagement":
        from agent.war_render import render_war_now

        data = db.build_war_now_context()
        if not data:
            return {"error": "No active war data available."}
        # Render the war-now prose at the agent boundary (storage returns data
        # only). Surfaced to the brain as the ``now_text`` field, as before.
        data["now_text"] = render_war_now(data)
        day_state = db.get_current_war_day_state() or {}
        remaining_deck_participants = _remaining_deck_participant_summary(day_state)
        data.update({
            "war_day_key": day_state.get("war_day_key"),
            "observed_at": day_state.get("observed_at"),  # QA M7: freshness anchor
            "clan_fame": day_state.get("clan_fame"),          # WEEKLY boat fame (cumulative)
            "clan_period_points": day_state.get("period_points"),  # TODAY's points (resets daily)
            "day_rank": day_state.get("day_rank"),
            "total_participants": day_state.get("total_participants"),
            "engaged_count": day_state.get("engaged_count"),
            "finished_count": day_state.get("finished_count"),
            "untouched_count": day_state.get("untouched_count"),
            "partial_deck_participant_count": remaining_deck_participants["partial"],
            "participants_with_decks_left_count": remaining_deck_participants["total"],
            "remaining_deck_participants": remaining_deck_participants,
            # Members contribute POINTS, not fame (fame is clan-only); top_points_total
            # is each member's cumulative season points. The season points leader is
            # the War Champ. decks_used_today is the daily signal.
            "top_points_total": day_state.get("top_points_total"),
            # Deck-usage rosters are slimmed to identity + deck/points counts: the
            # full member-reference dict (donation ranks, elder flags, etc.) blew
            # the 20K tool-result cap and dropped used_none (the no-show list the
            # brain needs). The brain drills into a member via get_member if needed.
            "used_all_4": [_compact_deck_row(m) for m in (day_state.get("used_all_4") or [])],
            "used_some": [_compact_deck_row(m) for m in (day_state.get("used_some") or [])],
            "used_none": [_compact_deck_row(m) for m in (day_state.get("used_none") or [])],
        })
        return data

    return {"error": f"Unknown aspect: {aspect}"}


def _war_standings_freshness(season_id=None):
    """Snapshot timestamp and section-finalization summary for live standings.

    Returns a dict the agent can quote to players (`as_of`,
    `current_week_included`, `current_week_war_day_key`, `finalized_races`).
    Falls back gracefully when no live war is active.
    """
    state = db.get_current_war_day_state() or {}
    state_season = state.get("season_id")
    target_season = season_id if season_id is not None else state_season
    war_day_key = state.get("war_day_key")
    section_index = state.get("section_index")

    finalized_races = 0
    current_section_finalized = False
    if target_season is not None:
        finalized_races = db.count_war_races_for_season(int(target_season)) or 0
        if section_index is not None:
            current_section_finalized = db.is_war_section_finalized(
                int(target_season), int(section_index)
            )

    current_week_included = bool(
        war_day_key
        and section_index is not None
        and state_season == target_season
        and not current_section_finalized
    )
    as_of = None
    if current_week_included and war_day_key:
        as_of = db.get_latest_war_participant_snapshot_observed_at(war_day_key)
    if not as_of:
        as_of = db.get_latest_war_race_finish_time(int(target_season)) if target_season is not None else None

    return {
        "as_of": as_of,
        "current_week_included": current_week_included,
        "current_week_war_day_key": war_day_key if current_week_included else None,
        "current_week_section_index": section_index if current_week_included else None,
        "finalized_races": finalized_races,
        "narration_hint": (
            "Quote `as_of` when answering 'right now' questions so players see how fresh the read is. "
            "If `current_week_included` is true, today's battles are included; if false, the response covers only finalized weeks."
        ),
    }


def _current_week_war_top(limit: int = 10) -> list[dict]:
    """Per-member points for the CURRENT war section (this week), active members.
    Fail-open: any error returns [] (the summary is still useful without it)."""
    try:
        conn = _facade_db().get_connection()
    except Exception:
        return []
    try:
        row = conn.execute(
            "SELECT season_id, MAX(section_index) AS section FROM war_participation "
            "WHERE season_id = (SELECT MAX(season_id) FROM war_participation) "
            "GROUP BY season_id"
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            """SELECT COALESCE(p.display_name, p.current_name) AS name, wp.fame AS points, wp.decks_used
               FROM war_participation wp
               JOIN players p ON p.player_tag = wp.player_tag
               JOIN clan_memberships cm ON cm.player_tag = wp.player_tag
                    AND cm.left_at IS NULL
               WHERE wp.season_id = ? AND wp.section_index = ?
               ORDER BY wp.fame DESC LIMIT ?""",
            (row["season_id"], row["section"], limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _execute_get_war_season(arguments):
    """Execute the consolidated get_war_season tool."""
    aspect = arguments.get("aspect", "summary")
    season_id = arguments.get("season_id")
    limit = arguments.get("limit", 10)

    if aspect == "summary":
        result = db.get_war_season_summary(season_id=season_id, top_n=limit)
        # "top contributors THIS WEEK" needs the current section's per-member
        # points, not season cumulative (rehearsal 2026-07-04: the model answered
        # season totals to a this-week question because only those existed).
        if isinstance(result, dict) and not season_id:
            result["current_week_top"] = _current_week_war_top(limit)
        return result
    elif aspect == "standings":
        # "fame" is accepted as a back-compat alias for the per-member points metric.
        metric = arguments.get("metric", "points")
        if metric in ("points", "fame"):
            members = db.get_war_champ_standings(season_id=season_id)
            _enrich_war_player_types(members)
            rookie_mvps = db.get_rookie_mvp_candidates(season_id=season_id, limit=3)
            return {
                "season_id": season_id,
                "metric": "points",
                "freshness": _war_standings_freshness(season_id),
                "members": members,
                "rookie_mvps": rookie_mvps,
            }
        elif metric == "win_rate":
            raw = db.get_war_battle_win_rates(
                season_id=season_id, limit=limit, min_battles=4,
            )
        elif metric == "attendance":
            raw = db.get_members_without_war_participation(season_id=season_id)
        else:
            return {"error": f"Unknown metric: {metric}"}
        if isinstance(raw, dict):
            members = (
                raw.get("members") or raw.get("standings") or raw.get("results") or []
            )
            _enrich_war_player_types(members)
        return raw
    elif aspect == "win_rates":
        return db.get_war_battle_win_rates(
            season_id=season_id, limit=limit, min_battles=4,
        )
    elif aspect == "boat_battles":
        return db.get_clan_boat_battle_record(weeks=3)
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
    "member_ref", "player_tag", "role", "roster_status", "trophies", "clan_rank",
    "donations_week", "donations_received_week", "war_points_rank_season",
    "joined_date", "elder_eligible", "in_discord", "cr_games_per_day",
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
            "note": ("donations_week / donations_received_week are THIS WEEK's "
                     "counts (CR resets them Monday); they are fully answerable "
                     "for any asker — not leadership-restricted."),
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
            days=days, window_days=window_days,
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
        return db.get_members_at_risk(
            inactivity_days=arguments.get("inactivity_days", 7),
            min_donations_week=arguments.get("min_donations_week", 20),
            require_war_participation=False,
            min_war_races=1,
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


def _ranked_current_standings(limit: int = 10) -> list[dict]:
    """Current Ranked standings for active members: league desc, rating desc.
    Reads player_current_state (rating source fixed 2026-07-04 — it was
    trophy-road trophies)."""
    from engine.normalize import ranked_league_name

    try:
        conn = _facade_db().get_connection()
    except Exception:
        return []
    try:
        rows = conn.execute(
            """SELECT COALESCE(p.display_name, p.current_name) AS name, pcs.ranked_league AS league,
                      pcs.ranked_trophies AS rating
               FROM player_current_state pcs
               JOIN players p ON p.player_tag = pcs.player_tag
               JOIN clan_memberships cm ON cm.player_tag = pcs.player_tag
                    AND cm.left_at IS NULL
               WHERE pcs.ranked_league IS NOT NULL
               ORDER BY pcs.ranked_league DESC,
                        COALESCE(pcs.ranked_trophies, 0) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {"name": r["name"], "league": r["league"],
             "league_name": ranked_league_name(r["league"]),
             "rating": r["rating"]}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()


def _execute_get_clan_game_modes(arguments):
    aspect = arguments.get("aspect", "summary")
    days = arguments.get("days", 30)
    limit = arguments.get("limit", 10)
    mode_group = arguments.get("mode_group")
    if aspect == "ranked":
        mode_group = "ranked"
    elif aspect == "events":
        mode_group = "special_event"
    summary = db.get_clan_game_mode_summary(days=days, mode_group=mode_group, limit=limit)
    if aspect == "ranked":
        return {
            "aspect": aspect,
            "window_days": summary["window_days"],
            "mode_mix": summary["by_group"],
            "ranked_activity": summary["ranked_activity"],
            "ranked_profiles": summary["ranked_profiles"],
            # "Top Ranked player" = standings (league, then rating) — NOT
            # battle volume; rehearsal 2026-07-04: the volume framing crowned
            # the wrong player. Era-correct names from the normalizer.
            "current_standings": _ranked_current_standings(),
        }
    if aspect == "duos":
        # 2v2 teammate pairs from battle_events.teammate_tag (battery case 31:
        # the data existed but no tool exposed pair aggregation).
        conn = db.get_connection()
        try:
            rows = conn.execute(
                """SELECT COALESCE(p1.display_name, p1.current_name) AS player, COALESCE(p2.display_name, p2.current_name) AS teammate,
                          COUNT(*) AS battles, SUM(b.outcome = 'W') AS wins
                   FROM battle_events b
                   JOIN players p1 ON p1.player_tag = b.player_tag
                   JOIN players p2 ON p2.player_tag = b.teammate_tag
                   WHERE b.teammate_tag IS NOT NULL
                     AND b.battle_time >= strftime('%Y%m%dT%H%M%S', 'now', ?)
                   GROUP BY b.player_tag, b.teammate_tag
                   HAVING battles >= 2
                   ORDER BY battles DESC LIMIT ?""",
                (f"-{days} days", limit),
            ).fetchall()
        finally:
            conn.close()
        return {
            "aspect": aspect,
            "window_days": days,
            # QA L14: the JOIN is against `players` (not the active roster), so a
            # teammate who has LEFT the clan but is still known WILL appear;
            # only never-seen external teammates are absent.
            "note": "pairs are directional (player's own logged battles); a teammate "
                    "who has since left the clan can still appear, but a teammate never "
                    "seen in our data (a random / non-roster player) does not",
            "duos": [dict(r) for r in rows],
        }
    if aspect == "side_modes":
        # QA M13: side_mode_progress + leaderboards are not populated yet (no
        # progress_json / leaderboard-context source). Empty means NOT TRACKED,
        # not "nothing happening" — say so rather than imply a quiet clan.
        side_progress = summary["side_mode_progress"]
        leaderboards = summary["leaderboards"]
        return {
            "aspect": aspect,
            "window_days": summary["window_days"],
            "side_mode_progress": side_progress,
            "leaderboards": leaderboards,
            "mode_mix": summary["by_group"],
            "side_mode_progress_tracked": bool(side_progress),
            "leaderboards_tracked": bool(leaderboards),
            "note": "side_mode_progress / leaderboards are not tracked yet; empty here means no data source, not clan inactivity — use mode_mix for side-mode activity.",
        }
    if aspect == "events":
        return {
            "aspect": aspect,
            "window_days": summary["window_days"],
            "event_activity": summary["by_game_mode"],
            "event_participation": summary["event_participation"],
            "event_badge_completions": summary["event_badge_completions"],
            "active_events": summary["active_events"],
            "mode_mix": summary["by_group"],
        }
    return {"aspect": aspect, **summary}


_ELIXIR_STATE_WINDOWS = (7, 28, 56, 90)


def _state_limit(arguments, *, default: int = 25, maximum: int = 100) -> int:
    try:
        value = int(arguments.get("limit", default))
    except (TypeError, ValueError):
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
        except (TypeError, ValueError):
            continue
        if 1 <= days <= 90:
            windows.append(days)
    return tuple(dict.fromkeys(windows)) or _ELIXIR_STATE_WINDOWS


def _state_days(arguments) -> int:
    try:
        days = int(arguments.get("days", 7))
    except (TypeError, ValueError):
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
    return None, {"error": "invalid_scope", "detail": f"Unknown state scope: {requested}"}


def _require_leadership_state(workflow: str | None) -> dict | None:
    if _workflow_can_read_leadership_state(workflow):
        return None
    return {
        "error": "leadership_state_unavailable",
        "detail": "This Elixir state view is available only in leadership workflows.",
    }


def _status_filter(arguments, *, default: tuple[str, ...] | None = None) -> tuple[str, ...] | None:
    status = (arguments.get("status") or "").strip()
    if not status:
        return default
    if status == "all":
        return None
    if status == "due":
        return ("due",)
    return (status,)


def _state_event_class(arguments):
    """Resolve the event_class filter: 'signal' (default), 'battle', or 'all' (None)."""
    event_class = (arguments.get("event_class") or "signal").strip().lower()
    return None if event_class == "all" else event_class


def _compact_intent(row: dict) -> dict:
    """Project a communication_intents row to what a reader needs, dropping the
    heavy payload_json / recognition_key / lifecycle-timestamp plumbing."""
    return {
        "intent_type": row.get("intent_type"),
        "lane": row.get("lane"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "posted": bool(row.get("discord_message_id")),
        "last_error": row.get("last_error") or None,
    }


def _execute_get_elixir_state(arguments, workflow=None):
    """Read Elixir's internal event/project/case/intent state with scope gates."""
    aspect = arguments.get("aspect", "operational_summary")
    limit = _state_limit(arguments)

    if aspect == "event_summary":
        scope, error = _state_scope(arguments, workflow)
        if error:
            return error
        result = event_facades.summarize_event_windows(
            windows=_state_windows(arguments),
            scope=scope,
            subject_type=arguments.get("subject_type"),
            subject_key=arguments.get("subject_key"),
            event_class=_state_event_class(arguments),
        )
        if isinstance(result, dict):
            # QA M15: signal-event counts reflect the ~7-day event stream, while
            # battles_mirrored spans the full battle history — the coverages
            # differ. event_class is NOT applied here (streams are signal events).
            result["coverage_note"] = (
                "signal-event counts cover only the recent event stream (~7d); "
                "battles_mirrored spans the full battle history — different coverage. "
                "event_class does not filter this view; use get_clan_game_modes for battles."
            )
        return result

    if aspect == "recent_events":
        scope, error = _state_scope(arguments, workflow)
        if error:
            return error
        event_class = _state_event_class(arguments)
        out = {
            "scope": scope or "all",
            "days": _state_days(arguments),
            "events": event_facades.list_recent_events(
                days=_state_days(arguments),
                scope=scope,
                event_type=arguments.get("event_type"),
                subject_type=arguments.get("subject_type"),
                subject_key=arguments.get("subject_key"),
                event_class=event_class,
                limit=limit,
            ),
        }
        if event_class == "battle":
            # QA M16: event_class='battle' is a no-op here (this is the signal
            # stream, not battle_events) — don't let it read as "no battles".
            out["note"] = ("event_class='battle' is not supported by this signal-event "
                           "view; use get_clan_game_modes for battle activity.")
        return out

    if aspect == "game_modes":
        return event_facades.summarize_battle_modes(
            windows=_state_windows(arguments),
            top_members=int(arguments.get("top_members") or 5),
        )

    if aspect == "season_window":
        return db.get_season_window()

    leadership_error = _require_leadership_state(workflow)
    if leadership_error:
        return leadership_error

    if aspect == "war_season":
        return {"war_season": db.get_war_season_snapshot()}

    if aspect == "decision_cases":
        status = (arguments.get("status") or "").strip()
        case_type = arguments.get("case_type")
        if status == "due":
            return {
                "due": db.list_due_decision_cases(case_type=case_type, limit=limit),
            }
        if status and status != "all":
            return {
                "cases": db.list_decision_cases(
                    statuses=(status,),
                    case_type=case_type,
                    limit=limit,
                ),
            }
        return db.decision_case_snapshot(open_limit=limit, due_limit=limit)

    if aspect == "communication_intents":
        status = (arguments.get("status") or "").strip()
        return {
            "intents": db.list_recent_communication_intents(
                status=status if status and status != "all" else None,
                workflow=arguments.get("workflow"),
                target_channel_key=arguments.get("target_channel_key"),
                limit=limit,
            )
        }

    if aspect == "communication_trace":
        message_id = (arguments.get("message_id") or "").strip()
        if not message_id:
            return {"error": "message_id_required"}
        return db.get_communication_trace_for_message(message_id) or {
            "error": "message_not_found",
            "message_id": message_id,
        }

    if aspect == "operational_summary":
        # A dashboard, not a data dump. Every block is structurally bounded so
        # the result stays under the tool envelope's char cap as data grows —
        # otherwise the envelope blindly drops whole arrays mid-deliberation.
        # Cases are capped tighter than intents because the awareness read
        # already carries the full deduped case set; this is a drill-down.
        case_limit = min(limit, 10)
        intent_limit = min(limit, 15)
        return {
            "event_windows": event_facades.summarize_event_windows(windows=_ELIXIR_STATE_WINDOWS, scope=None),
            "recent_events": event_facades.list_recent_events(days=7, limit=10),
            "game_modes": event_facades.summarize_battle_modes(windows=(7,)),
            "war_season": db.get_war_season_snapshot(),
            "decision_cases": db.decision_case_snapshot(
                open_limit=case_limit, due_limit=case_limit, dedupe=True
            ),
            "recent_intents": [
                _compact_intent(i) for i in db.list_recent_communication_intents(limit=intent_limit)
            ],
            "failed_intents": [
                _compact_intent(i)
                for i in db.list_recent_communication_intents(status="failed", limit=intent_limit)
            ],
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
            raise ValueError("birthday value must be {\"month\": M, \"day\": D}")
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
    case_type = (arguments.get("case_type") or "").strip()

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
    case = None
    if case_type:
        try:
            case = db.upsert_member_review_case(
                case_type=case_type,
                member={"tag": resolved_tag},
                title=f"Watch: {resolved_tag}",
                recommendation=reason,
                rationale=reason,
                due_at=expires_at,
            )
        except Exception as exc:
            log.warning("flag_member_watch case upsert failed: %s", exc)
    result = {
        "success": True,
        "memory_id": memory["memory_id"],
        "member_tag": resolved_tag,
        "type": "watch",
    }
    if case:
        result["case_id"] = case.get("case_id")
        result["case_key"] = case.get("case_key")
    return result


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


def _followup_topic_slug(topic: str, *, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (topic or "").strip().lower()).strip("-")
    return slug[:max_len].rstrip("-") or "general"


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
    case_type = (arguments.get("case_type") or "").strip()

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
    # A leadership followup is action-oriented by definition, so it always becomes
    # a durable decision case — the single home for the concern — with the memory
    # above as its narrative annotation. A specific case_type (e.g.
    # promotion_review) routes to the member-review card path; otherwise it is a
    # generic followup case keyed by topic so distinct concerns about the same
    # member do not collapse into one.
    case = None
    effective_type = case_type or "leadership_followup"
    topic_slug = _followup_topic_slug(topic)
    try:
        if case_type and resolved_tag:
            case = db.upsert_member_review_case(
                case_type=case_type,
                member={"tag": resolved_tag},
                title=f"Followup: {topic}",
                recommendation=recommendation,
                rationale=recommendation,
            )
        elif resolved_tag:
            case = db.upsert_decision_case(
                case_type=effective_type,
                title=f"Followup: {topic}",
                recommendation=recommendation,
                rationale=recommendation,
                target_player_tag=resolved_tag,
                case_key=f"leadership_followup:member:{resolved_tag}:{topic_slug}",
                state={"topic": topic},
            )
        else:
            case = db.upsert_decision_case(
                case_type=effective_type,
                title=f"Followup: {topic}",
                recommendation=recommendation,
                rationale=recommendation,
                subject_type="operation",
                subject_key=f"operation:{topic_slug}",
                case_key=f"leadership_followup:{topic_slug}",
                state={"topic": topic},
            )
    except Exception as exc:
        log.warning("record_leadership_followup case upsert failed: %s", exc)
    result = {
        "success": True,
        "memory_id": memory["memory_id"],
        "member_tag": resolved_tag,
        "type": "followup",
    }
    if case:
        result["case_id"] = case.get("case_id")
        result["case_key"] = case.get("case_key")
    return result


# ── Main dispatch ─────────────────────────────────────────────────────────

def _execute_tool(name, arguments, workflow=None):
    """Execute a tool call and return the result as a string."""
    try:
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
        elif name == "get_member_card_profile":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            _refresh_member_cache(member_tag, include_battles=False)
            result = db.get_member_card_profile(member_tag)
            if result is None:
                result = {
                    "error": "no_collection_snapshot",
                    "member_tag": member_tag,
                    "hint": "No card collection snapshot exists yet for this member.",
                }
            else:
                _annotate_roster_status(result, member_tag)
                # QA M21: upgrade costs / cards-required come from a static offline
                # cost table (card levels are live; the per-level card counts are
                # not) — flag the precision so "N to next level" isn't overstated.
                if isinstance(result, dict):
                    result["upgrade_cost_note"] = (
                        "cards_required / ready-to-upgrade counts come from a static "
                        "card-cost table, not live game state; treat them as close "
                        "estimates, and gold to actually upgrade is unknown."
                    )
        elif name == "lookup_member_cards":
            member_tag = _resolve_member_tag(arguments["member_tag"])
            include_battles = bool(((arguments.get("filter") or {}).get("mode")) == "war"
                                   or (arguments.get("filter") or {}).get("deck"))
            _refresh_member_cache(member_tag, include_battles=include_battles)
            result = db.lookup_member_cards(
                member_tag,
                filter=arguments.get("filter"),
                limit=arguments.get("limit", 20),
            )
            _annotate_roster_status(result, member_tag)
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
