from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import (
    _build_form_label,
    _build_form_summary,
    _canon_tag,
    _card_level,
    _rowdicts,
    _utcnow,
    chicago_today,
    managed_connection,
)
from storage._enrichment import _member_reference_fields
from storage.game_modes import (
    mode_group_label,
    special_event_badge_names,
    special_event_context_for_badge,
)

CARD_UPGRADE_SIGNAL_MIN_LEVEL = 16
MASTERY_BADGE_SIGNAL_MIN_LEVEL = 5
CARD_UNLOCK_SIGNAL_RARITIES = {"legendary", "champion"}
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
    if badge_name in {
        "EmoteCollection",
        "BannerCollection",
        "CollectionLevel",
        "ClanDonations",
    }:
        return "collection"
    if badge_name.startswith("SeasonalBadge_") or badge_name.startswith("MergeTacticsBadge_"):
        return "seasonal"
    if special_event_context_for_badge(badge_name):
        return "event"
    if badge_name.startswith("Crl") or badge_name in {"EasterEgg"}:
        return "event"
    if badge_name in {
        "YearsPlayed",
        "BattleWins",
        "ClanWarWins",
        "ClanWarsVeteran",
        "LadderTop1000",
    }:
        return "career"
    return "general"


def _badge_label(name: str | None) -> str | None:
    badge_name = str(name or "").strip()
    if not badge_name:
        return None
    if badge_name in BADGE_NAME_OVERRIDES:
        return BADGE_NAME_OVERRIDES[badge_name]
    if badge_name.startswith("Mastery") and len(badge_name) > len("Mastery"):
        return f"{_split_identifier_words(badge_name[len('Mastery') :])} Mastery"
    return _split_identifier_words(badge_name)


def _badge_card_name(name: str | None) -> str | None:
    badge_name = str(name or "").strip()
    if not badge_name.startswith("Mastery") or len(badge_name) <= len("Mastery"):
        return None
    return _split_identifier_words(badge_name[len("Mastery") :])


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
    event_context = special_event_context_for_badge(name)
    if event_context:
        fields.update(
            {
                "event_name": event_context["event_name"],
                "event_recognition_guidance": event_context["recognition_guidance"],
            }
        )
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
        "cr_account_age_years": age_years
        if isinstance(age_years, int) and age_years >= 0
        else None,
        "cr_account_age_updated_at": fetched_at,
    }


def _nonnegative_int(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _profile_badge_metadata_fields(player_data: dict, *, fetched_at: str) -> dict:
    badges = _indexed_items(player_data.get("badges") or [])
    collection_level = badges.get("CollectionLevel") or {}
    return {
        "cr_collection_level": _nonnegative_int(collection_level.get("progress")),
        "cr_collection_level_badge_tier": _nonnegative_int(collection_level.get("level")),
        "cr_collection_level_badge_max_tier": _nonnegative_int(collection_level.get("maxLevel")),
        "cr_collection_level_updated_at": fetched_at,
        "cr_clan_war_wins": _nonnegative_int((badges.get("ClanWarWins") or {}).get("progress")),
        "cr_battle_wins": _nonnegative_int((badges.get("BattleWins") or {}).get("progress")),
        "cr_clan_donations": _nonnegative_int((badges.get("ClanDonations") or {}).get("progress")),
        "cr_banner_count": _nonnegative_int((badges.get("BannerCollection") or {}).get("progress")),
        "cr_emote_count": _nonnegative_int((badges.get("EmoteCollection") or {}).get("progress")),
        "cr_profile_badges_updated_at": fetched_at,
    }


def _games_per_day_metadata_fields(member_id: int, *, computed_at: str, conn) -> dict:
    cutoff = (
        (
            datetime.fromisoformat(chicago_today())
            - timedelta(days=max(GAMES_PER_DAY_WINDOW_DAYS - 1, 0))
        )
        .date()
        .isoformat()
    )
    row = conn.execute(
        "SELECT COALESCE(SUM(battles), 0) AS total_battles "
        "FROM player_daily_battle_rollups WHERE member_id = ? AND battle_date >= ?",
        (member_id, cutoff),
    ).fetchone()
    total_battles = int((row["total_battles"] or 0) if row else 0)
    return {
        "cr_games_per_day": round(total_battles / GAMES_PER_DAY_WINDOW_DAYS, 2),
        "cr_games_per_day_window_days": GAMES_PER_DAY_WINDOW_DAYS,
        "cr_games_per_day_updated_at": computed_at,
    }


def _card_display_max_level(card: dict) -> int | None:
    """Delegates to the normalizer (engine/normalize.py)."""
    from engine.normalize import card_display_max_level

    return card_display_max_level(card.get("maxLevel"))


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
def snapshot_player_profile(
    player_data: dict,
    *,
    expected_tag: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Apply an interactive profile refresh through the engine's canonical
    observation path. Returns [] (the retired signal list stays retired)."""
    from engine import materialize, observations
    from storage.incidents import record_incident

    requested_tag = expected_tag or (
        player_data.get("tag") if isinstance(player_data, dict) else ""
    )
    now = _utcnow()
    admission, observation = observations.observe(
        "player",
        requested_tag,
        player_data,
        now,
        source="interactive_refresh",
    )
    if not admission.accepted:
        record_incident(
            "storage.snapshot_player_profile",
            "CR observation rejected by admission boundary",
            context={"entity_key": admission.entity_key, "errors": admission.errors},
            severity="error",
            conn=conn,
        )
        return []
    assert observation is not None
    materialize.apply_interactive_observation(
        conn,
        observation,
        track_poll_freshness=True,
    )
    return []


@managed_connection
def get_player_intel_refresh_targets(
    limit: int = 12,
    stale_after_hours: int = 6,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """v5.1: staleness reads poll_state (the adaptive scheduler's ledger,
    runtime.md §4) instead of profile-snapshot ages."""
    stale_cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=stale_after_hours)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, "
        "cs.role, cs.clan_rank, ps.last_profile_poll AS last_profile_at, "
        "ps.last_battle_seen AS last_battle_at "
        "FROM players m "
        "LEFT JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "LEFT JOIN poll_state ps ON ps.player_tag = m.player_tag "
        "WHERE EXISTS (SELECT 1 FROM clan_memberships cm "
        "  WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL) "
        "ORDER BY "
        "CASE cs.role WHEN 'leader' THEN 0 WHEN 'coLeader' THEN 1 WHEN 'elder' THEN 2 ELSE 3 END, "
        "COALESCE(ps.last_profile_poll, '') ASC, "
        "COALESCE(cs.clan_rank, 999) ASC, "
        "m.current_name COLLATE NOCASE"
    ).fetchall()
    targets = []
    for row in rows:
        item = dict(row)
        item["needs_profile_refresh"] = (
            item["last_profile_at"] is None or item["last_profile_at"] < stale_cutoff
        )
        item["needs_battle_refresh"] = (
            item["last_battle_at"] is None or item["last_battle_at"] < stale_cutoff
        )
        if not item["needs_profile_refresh"] and not item["needs_battle_refresh"]:
            continue
        targets.append(_member_reference_fields(conn, row["tag"], item))
    return targets[:limit]


# Modes we signal on separately. ``ladder`` is Trophy Road (is_ladder=1),
# ``ranked`` is Path of Legend / Ranked (is_ranked=1). Combining them into one streak
# signal (pre-v4.7) hid the mode from the awareness agent — "hot in ranked
# climbing toward UC" reads nothing like "ladder push on Trophy Road." See #22.
_BATTLE_MODE_PREDICATES = {
    "ladder": "is_ladder = 1",
    "ranked": "is_ranked = 1",
}

_HIGH_TROPHY_THRESHOLD = 7000
_NOTABLE_OPPONENT_LIMIT = 3
_OPPONENTS_SUMMARY_LIMIT = 10


@managed_connection
def snapshot_player_battlelog(
    player_tag: str, battle_log: list[dict], conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    """Apply an interactive battlelog refresh through the engine's canonical
    observation path. Returns [] (the retired signal list stays retired)."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from engine import materialize, observations
    from storage.incidents import record_incident

    tag = _canon_tag(player_tag)
    if not tag:
        return []
    now_dt = _dt.now(_tz.utc)
    now = _utcnow()
    admission, observation = observations.observe(
        "player_battlelog",
        tag,
        battle_log,
        now,
        source="interactive_refresh",
    )
    if not admission.accepted:
        record_incident(
            "storage.snapshot_player_battlelog",
            "CR observation rejected by admission boundary",
            context={"entity_key": admission.entity_key, "errors": admission.errors},
            severity="error",
            conn=conn,
        )
        return []
    assert observation is not None
    materialize.apply_interactive_observation(
        conn,
        observation,
        now=now_dt,
        track_poll_freshness=True,
    )
    return []


_LOSSES_SCOPE_PREDICATES = {
    "overall_10": "1=1",
    "competitive_10": "is_competitive = 1",
    "ladder_ranked_10": "(is_ladder = 1 OR is_ranked = 1)",
    "ladder_10": "is_ladder = 1",
    "ranked_10": "is_ranked = 1",
    "event_10": "is_special_event = 1",
    "tournament_10": "(battle_type = 'tournament' OR tournament_tag IS NOT NULL)",
    "two_v_two_10": "(battle_type = 'clanMate2v2' OR game_mode_id IN (72000014, 72000051) OR game_mode_name = 'TeamVsTeam')",
    "friendly_10": "(battle_type IN ('clanMate', 'friendly', 'unknown') OR is_hosted_match = 1)",
    "war_10": "is_war = 1",
}


def _deck_card_modes(deck_json) -> list[tuple[str, Optional[str]]]:
    """(name, played_as) for each card in a stored slim deck.

    `played_as` follows the same 'evo'/'hero'/None convention the card-usage
    aggregation uses, so an Evo Knight is never collapsed into a plain one.
    Tolerant of NULL and of rows written before decks carried an evolution mode.
    """
    try:
        cards = json.loads(deck_json or "[]")
    except json.JSONDecodeError, TypeError, ValueError:
        return []
    if not isinstance(cards, list):
        return []
    out = []
    for card in cards:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        mode = card.get("evolution_level")
        out.append((card["name"], "evo" if mode == 1 else "hero" if mode == 2 else None))
    return out


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
        "SELECT player_tag AS member_id, COALESCE(display_name, current_name) AS current_name FROM players WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return None
    member_id = member_row["member_id"]
    rows = conn.execute(
        f"SELECT outcome, crowns_for, crowns_against, opponent_deck_json, battle_time, battle_type, game_mode_name, "
        f"opponent_tag, opponent_name, opponent_clan_tag "
        f"FROM battle_events WHERE player_tag = ? AND {predicate} "
        f"ORDER BY battle_time DESC LIMIT ?",
        (member_id, limit),
    ).fetchall()
    sample_battles = len(rows)
    losses = [r for r in rows if r["outcome"] == "L"]
    losses_examined = len(losses)
    # The headline: which cards keep showing up on the other side of a loss.
    # Counted once per BATTLE, not once per appearance, so the number reads as
    # "beaten by this card N times" rather than a card-slot tally. The dedupe
    # matters for war duels, which store 2-3 sub-decks under one battle and
    # could otherwise count a shared card twice for a single loss.
    card_counts: Counter[tuple[str, Optional[str]]] = Counter()
    decks_seen = 0
    opponent_agg: dict[str, dict] = {}
    for row in losses:
        card_counts.update(set(_deck_card_modes(row["opponent_deck_json"])))
        if row["opponent_deck_json"]:
            decks_seen += 1
        opp_tag = row["opponent_tag"]
        if opp_tag:
            entry = opponent_agg.get(opp_tag)
            if entry is None:
                entry = {"tag": opp_tag, "losses_count": 0}
                opponent_agg[opp_tag] = entry
            entry["losses_count"] += 1
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
    opponent_tags = sorted(
        opponent_agg.values(),
        key=lambda o: (o["losses_count"], o.get("name") or ""),
        reverse=True,
    )
    # Battles polled before schema v16 have a NULL opponent deck and are not
    # backfillable once their raw payload ages out, so state the coverage rather
    # than implying every examined loss contributed cards.
    top_opponent_cards = [
        {"name": name, "played_as": played_as, "losses_faced": count}
        for (name, played_as), count in card_counts.most_common(top_cards)
    ]
    return {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "scope": scope,
        "lookback_battles": sample_battles,
        "losses_examined": losses_examined,
        "current_loss_streak": current_loss_streak,
        "avg_crown_deficit": avg_crown_deficit,
        "opponent_tags": opponent_tags,
        "top_opponent_cards": top_opponent_cards,
        "losses_with_deck_data": decks_seen,
        "opponent_decks_captured": bool(decks_seen),
        "note": (
            f"Opponent cards come from {decks_seen} of {losses_examined} examined "
            "losses; the rest predate deck capture. `losses_faced` counts battles "
            "lost with that card on the other side, not card copies. Cite only "
            "cards listed here."
            if decks_seen
            else (
                "No opponent decks captured in this window (all examined losses "
                "predate deck capture). Do not cite or invent opponent cards — "
                "ground comments in loss streak / crown deficit, and use "
                "opponent_tags to scout via cr_api (aspect='player'/'clan')."
            )
        ),
    }


@managed_connection
def get_member_recent_battles(
    tag: str,
    scope: str = "overall_10",
    limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    """Chronological list of this member's most recent individual battles.

    Returns per-battle rows from battle_events: outcome, crowns, trophy
    change, opponent name/tag/clan, and slim own/opponent deck previews.
    Powers the `battles` include on get_member.
    """
    member_tag = _canon_tag(tag)
    predicate = _LOSSES_SCOPE_PREDICATES.get(scope, _LOSSES_SCOPE_PREDICATES["overall_10"])
    member_row = conn.execute(
        "SELECT player_tag AS member_id, COALESCE(display_name, current_name) AS current_name FROM players WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return None
    requested_limit = max(1, int(limit or 10))
    cap = 100
    capped_limit = min(requested_limit, cap)
    rows = conn.execute(
        f"SELECT battle_time, battle_type, game_mode_name, outcome, crowns_for, crowns_against, "
        f"trophy_change, deck_json, opponent_deck_json, opponent_name, opponent_tag, opponent_clan_tag "
        f"FROM battle_events WHERE player_tag = ? AND {predicate} "
        f"ORDER BY battle_time DESC LIMIT ?",
        (member_row["member_id"], capped_limit),
    ).fetchall()
    battles = []
    for row in rows:
        entry = {
            "battle_time": row["battle_time"],
            "battle_type": row["battle_type"],
            "game_mode_name": row["game_mode_name"],
            "outcome": row["outcome"],
            "crowns_for": row["crowns_for"],
            "crowns_against": row["crowns_against"],
            "trophy_change": row["trophy_change"],
            "opponent_name": row["opponent_name"],
            "opponent_tag": row["opponent_tag"],
            "opponent_clan_tag": row["opponent_clan_tag"],
        }
        for field, raw in (
            ("own_deck", row["deck_json"]),
            ("opponent_deck", row["opponent_deck_json"]),
        ):
            try:
                cards = json.loads(raw or "[]")
            except TypeError, ValueError:
                cards = []
            if isinstance(cards, list) and cards:
                entry[field] = [
                    c.get("name") for c in cards if isinstance(c, dict) and c.get("name")
                ]
        battles.append(entry)
    result = {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "scope": scope,
        "count": len(battles),
        "battles": battles,
    }
    if requested_limit > cap:
        result["requested_limit"] = requested_limit
        result["capped_at"] = cap
        result["cap_note"] = (
            f"You asked for {requested_limit} battles but the per-call cap is {cap}. "
            f"Returned the most recent {cap}. Tell the user you're working from {cap} battles, "
            f"not the {requested_limit} they requested."
        )
    return result


def _json_object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except TypeError, ValueError, json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _profile_pol_payload(row, field: str) -> dict | None:
    payload = _json_object(row[field] if row and field in row.keys() else None)
    return payload or None


def _rollup_summary(rows) -> dict:
    battles = sum(int(row["battles"] or 0) for row in rows)
    wins = sum(int(row["wins"] or 0) for row in rows)
    losses = sum(int(row["losses"] or 0) for row in rows)
    draws = sum(int(row["draws"] or 0) for row in rows)
    trophy_delta = sum(int(row["trophy_change_total"] or 0) for row in rows)
    return {
        "battles": battles,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": round(wins / battles, 4) if battles else None,
        "trophy_delta": trophy_delta,
    }


@managed_connection
def get_member_ranked_status(
    tag: str, days: int = 30, conn: Optional[sqlite3.Connection] = None
) -> Optional[dict]:
    member_tag = _canon_tag(tag)
    member_row = conn.execute(
        "SELECT player_tag AS member_id, COALESCE(display_name, current_name) AS current_name FROM players WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return None

    # v5.1: PoL detail comes from player_current_state (ranked_* columns);
    # the season-result JSON blobs retired with the profile snapshots.
    cs = conn.execute(
        "SELECT observed_at AS fetched_at, trophies, best_trophies, ranked_league, ranked_trophies "
        "FROM player_current_state WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    profile = None
    if cs:
        profile = {
            "fetched_at": cs["fetched_at"],
            "trophies": cs["trophies"],
            "best_trophies": cs["best_trophies"],
            "current_path_of_legend_season_result_json": None,
            "last_path_of_legend_season_result_json": None,
            "best_path_of_legend_season_result_json": None,
            "ranked_league": cs["ranked_league"],
            "ranked_trophies": cs["ranked_trophies"],
        }

    rollups = list_player_daily_battle_rollups(
        member_tag, days=days, mode_group="ranked", conn=conn
    )
    recent = _rollup_summary(rollups)
    latest_battle = conn.execute(
        "SELECT battle_time, game_mode_id, game_mode_name, league_number, outcome, trophy_change "
        "FROM battle_events WHERE player_tag = ? AND is_ranked = 1 "
        "ORDER BY battle_time DESC LIMIT 1",
        (member_row["member_id"],),
    ).fetchone()

    result = {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "window_days": max(1, int(days or 30)),
        "current": (
            {
                "leagueNumber": profile.get("ranked_league"),
                "trophies": profile.get("ranked_trophies"),
            }
            if profile and profile.get("ranked_league") is not None
            else None
        ),
        "last": None,
        "best": None,
        "trophy_road": {
            "trophies": profile["trophies"] if profile else None,
            "best_trophies": profile["best_trophies"] if profile else None,
        },
        "recent_ranked": recent,
        "profile_fetched_at": profile["fetched_at"] if profile else None,
    }
    if latest_battle:
        result["latest_ranked_battle"] = dict(latest_battle)
    result["note"] = (
        "Use player-facing 'Ranked'; API fields are stored as Path of Legend/pathOfLegend."
    )
    return result


@managed_connection
def list_player_daily_battle_rollups(
    tag: str,
    days: int = 30,
    mode_group: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    member_tag = _canon_tag(tag)
    cutoff = (
        (datetime.fromisoformat(chicago_today()) - timedelta(days=max(days - 1, 0)))
        .date()
        .isoformat()
    )
    where = ["r.player_tag = ?", "r.battle_date >= ?"]
    params = [member_tag, cutoff]
    if mode_group:
        where.append("r.mode_group = ?")
        params.append(mode_group)
    rows = conn.execute(
        "SELECT r.battle_date, r.mode_group, r.game_mode_id, r.game_mode_name, r.battles, r.wins, r.losses, r.draws, r.crowns_for, r.crowns_against, r.trophy_change_total, r.first_battle_at, r.last_battle_at, r.captured_battles, r.expected_battle_delta, r.completeness_ratio, r.is_complete "
        "FROM player_daily_battle_rollups r "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY r.battle_date ASC, r.mode_group ASC, COALESCE(r.game_mode_id, 0) ASC",
        tuple(params),
    ).fetchall()
    return _rowdicts(rows)


@managed_connection
def get_member_mode_activity(
    tag: str,
    days: int = 30,
    mode_group: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    member_tag = _canon_tag(tag)
    member_row = conn.execute(
        "SELECT player_tag AS member_id, COALESCE(display_name, current_name) AS current_name FROM players WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return None
    rows = list_player_daily_battle_rollups(member_tag, days=days, mode_group=mode_group, conn=conn)
    by_group: dict[str, dict] = {}
    by_game_mode: dict[tuple[str, Optional[int]], dict] = {}
    for row in rows:
        group = row["mode_group"] or "other"
        group_bucket = by_group.setdefault(
            group,
            {
                "mode_group": group,
                "label": mode_group_label(group),
                "battles": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "trophy_delta": 0,
            },
        )
        game_key = (group, row["game_mode_id"])
        game_bucket = by_game_mode.setdefault(
            game_key,
            {
                "mode_group": group,
                "label": mode_group_label(group),
                "game_mode_id": row["game_mode_id"],
                "game_mode_name": row["game_mode_name"],
                "battles": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "trophy_delta": 0,
            },
        )
        for bucket in (group_bucket, game_bucket):
            bucket["battles"] += int(row["battles"] or 0)
            bucket["wins"] += int(row["wins"] or 0)
            bucket["losses"] += int(row["losses"] or 0)
            bucket["draws"] += int(row["draws"] or 0)
            bucket["trophy_delta"] += int(row["trophy_change_total"] or 0)

    for bucket in list(by_group.values()) + list(by_game_mode.values()):
        bucket["win_rate"] = (
            round(bucket["wins"] / bucket["battles"], 4) if bucket["battles"] else None
        )

    return {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "window_days": max(1, int(days or 30)),
        "mode_group": mode_group,
        "by_group": sorted(
            by_group.values(), key=lambda item: (-item["battles"], item["mode_group"])
        ),
        "by_game_mode": sorted(
            by_game_mode.values(),
            key=lambda item: (
                -item["battles"],
                item["mode_group"],
                item.get("game_mode_id") or 0,
            ),
        ),
    }


@managed_connection
def get_member_special_event_activity(
    tag: str,
    days: int = 14,
    *,
    game_mode_id=None,
    game_mode_name: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict]:
    member_tag = _canon_tag(tag)
    member_row = conn.execute(
        "SELECT player_tag AS member_id, COALESCE(display_name, current_name) AS current_name FROM players WHERE player_tag = ?",
        (member_tag,),
    ).fetchone()
    if not member_row:
        return None
    days = max(1, int(days or 14))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S.000Z")
    where = ["bf.player_tag = ?", "bf.is_special_event = 1", "bf.battle_time >= ?"]
    params: list = [member_row["member_id"], cutoff]
    if game_mode_id is not None:
        where.append("bf.game_mode_id = ?")
        params.append(game_mode_id)
    if game_mode_name:
        where.append("bf.game_mode_name = ?")
        params.append(game_mode_name)
    event_contexts = _special_event_context_index(conn)
    rows = conn.execute(
        "SELECT bf.game_mode_id, bf.game_mode_name, bf.event_tag, COUNT(*) AS battles, "
        "SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN bf.outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
        "MAX(bf.battle_time) AS latest_battle "
        "FROM battle_events bf "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY bf.game_mode_id, bf.game_mode_name, bf.event_tag "
        "ORDER BY battles DESC, latest_battle DESC",
        tuple(params),
    ).fetchall()
    modes = []
    total_battles = 0
    for row in rows:
        item = dict(row)
        item["battles"] = int(item.get("battles") or 0)
        item["wins"] = int(item.get("wins") or 0)
        item["losses"] = int(item.get("losses") or 0)
        total_battles += item["battles"]
        item["win_rate"] = round(item["wins"] / item["battles"], 4) if item["battles"] else None
        _enrich_special_event_item(item, event_contexts)
        modes.append(item)
    return {
        "member_tag": member_tag,
        "member_name": member_row["current_name"],
        "window_days": days,
        "total_battles": total_battles,
        "by_game_mode": modes,
    }


def _special_event_badge_completions(days: int, conn) -> dict[str, list[dict]]:
    badge_names = special_event_badge_names()
    if not badge_names:
        return {}
    cutoff = (
        (datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 30))))
        .replace(tzinfo=None)
        .strftime("%Y-%m-%dT%H:%M:%S")
    )
    placeholders = ",".join("?" for _ in badge_names)
    rows = conn.execute(
        "SELECT player_tag AS tag, json_extract(payload_json, '$.badge_name') AS badge_name, "
        "json_extract(payload_json, '$.badge_name') AS badge_label, COUNT(*) AS badge_events, "
        "MAX(observed_at) AS latest_badge_event "
        "FROM player_events "
        "WHERE event_type = 'badge_earned' "
        f"AND json_extract(payload_json, '$.badge_name') IN ({placeholders}) "
        "AND observed_at >= ? "
        "GROUP BY player_tag, badge_name "
        "ORDER BY latest_badge_event DESC",
        (*badge_names, cutoff),
    ).fetchall()
    completions_by_tag: dict[str, list[dict]] = {}
    for row in rows:
        context = special_event_context_for_badge(row["badge_name"]) or {}
        item = {
            "tag": row["tag"],
            "badge_name": row["badge_name"],
            "badge_label": row["badge_label"] or context.get("badge_label"),
            "badge_events": int(row["badge_events"] or 0),
            "latest_badge_event": row["latest_badge_event"],
        }
        if context:
            item.update(
                {
                    "event_name": context["event_name"],
                }
            )
        completions_by_tag.setdefault(row["tag"], []).append(item)
    return completions_by_tag


def _event_description_from_raw(raw_json) -> str | None:
    try:
        raw = json.loads(raw_json or "{}")
    except TypeError, ValueError, json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    description = raw.get("description")
    return description if isinstance(description, str) and description.strip() else None


def _special_event_context_index(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT source_key, display_name, game_mode_id, game_mode_name, event_tag, raw_json "
        "FROM game_mode_contexts WHERE context_type = 'event'"
    ).fetchall()
    by_tag: dict[str, dict] = {}
    by_mode_id: dict[int, dict | None] = {}
    for row in rows:
        context = {
            "event_name": row["display_name"],
            "event_description": _event_description_from_raw(row["raw_json"]),
            "event_tag": row["event_tag"],
            "event_source_key": row["source_key"],
        }
        if row["event_tag"]:
            by_tag[str(row["event_tag"])] = context
        if row["game_mode_id"] is not None:
            mode_id = int(row["game_mode_id"])
            by_mode_id[mode_id] = context if mode_id not in by_mode_id else None
    return {"by_tag": by_tag, "by_mode_id": by_mode_id}


def _enrich_special_event_item(item: dict, context_index: dict[str, dict]) -> dict:
    context = None
    event_tag = item.get("event_tag")
    if event_tag:
        context = context_index["by_tag"].get(str(event_tag))
    if context is None and item.get("game_mode_id") is not None:
        context = context_index["by_mode_id"].get(int(item["game_mode_id"]))
    if context:
        item["event_name"] = context.get("event_name")
        item["event_description"] = context.get("event_description")
        item["event_source_key"] = context.get("event_source_key")
        if not item.get("event_tag"):
            item["event_tag"] = context.get("event_tag")
    return item


def _special_event_activity(days: int, limit: int, conn) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 30)))).strftime(
        "%Y%m%dT%H%M%S.000Z"
    )
    event_contexts = _special_event_context_index(conn)
    rows = conn.execute(
        "SELECT bf.game_mode_id, bf.game_mode_name, bf.event_tag, "
        "COUNT(DISTINCT bf.player_tag) AS members_active, COUNT(*) AS battles, "
        "SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN bf.outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
        "SUM(CASE WHEN bf.outcome = 'D' THEN 1 ELSE 0 END) AS draws, "
        "SUM(COALESCE(bf.trophy_change, 0)) AS trophy_delta, "
        "MAX(bf.battle_time) AS latest_battle "
        "FROM battle_events bf "
        "WHERE bf.is_special_event = 1 AND bf.battle_time >= ? "
        "GROUP BY bf.game_mode_id, bf.game_mode_name, bf.event_tag "
        "ORDER BY battles DESC, latest_battle DESC, COALESCE(bf.game_mode_id, 0) ASC "
        "LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    activity = []
    for row in rows:
        item = {
            "mode_group": "special_event",
            "label": mode_group_label("special_event"),
            "game_mode_id": row["game_mode_id"],
            "game_mode_name": row["game_mode_name"],
            "event_tag": row["event_tag"],
            "members_active": int(row["members_active"] or 0),
            "battles": int(row["battles"] or 0),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "draws": int(row["draws"] or 0),
            "trophy_delta": int(row["trophy_delta"] or 0),
            "latest_battle": row["latest_battle"],
        }
        item["win_rate"] = round(item["wins"] / item["battles"], 4) if item["battles"] else None
        activity.append(_enrich_special_event_item(item, event_contexts))
    return activity


def _special_event_participation(
    days: int,
    limit: int,
    conn,
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 30)))).strftime(
        "%Y%m%dT%H%M%S.000Z"
    )
    event_contexts = _special_event_context_index(conn)
    rows = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, "
        "bf.game_mode_id, bf.game_mode_name, bf.event_tag, COUNT(*) AS event_battles, "
        "SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN bf.outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
        "MAX(bf.battle_time) AS latest_event_battle "
        "FROM battle_events bf "
        "JOIN players m ON m.player_tag = bf.player_tag "
        "WHERE bf.is_special_event = 1 AND bf.battle_time >= ? "
        "GROUP BY bf.player_tag, bf.game_mode_id, bf.game_mode_name, bf.event_tag "
        "ORDER BY event_battles DESC, latest_event_battle DESC, m.current_name COLLATE NOCASE "
        "LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    participation = []
    for row in rows:
        item = dict(row)
        battles = int(item.get("event_battles") or 0)
        wins = int(item.get("wins") or 0)
        item["event_battles"] = battles
        item["wins"] = wins
        item["losses"] = int(item.get("losses") or 0)
        item["win_rate"] = round(wins / battles, 4) if battles else None
        _enrich_special_event_item(item, event_contexts)
        participation.append(_member_reference_fields(conn, item.pop("member_id"), item))
    return participation


@managed_connection
def get_clan_mode_top_members(
    days: int = 7, per_mode: int = 3, conn: Optional[sqlite3.Connection] = None
) -> dict:
    """Top most-active members per mode group over the window — the NAMED activity
    behind the aggregate mode mix (who's grinding ranked, pushing an event, running
    2v2 or ladder). Sourced from the authoritative battle_events store, keyed by
    mode label; ranked rows also carry the member's current Path-of-Legends league.
    Compact by design (top ``per_mode`` each) so it fits the awareness read."""
    days = max(1, int(days or 7))
    per_mode = max(1, min(int(per_mode or 3), 10))
    rows = conn.execute(
        """
        WITH m AS (
          SELECT b.mode_group,
                 COALESCE(p.display_name, p.current_name) AS name,
                 pcs.ranked_league AS league,
                 COUNT(*) AS battles,
                 SUM(b.outcome = 'W') AS wins,
                 SUM(b.outcome = 'L') AS losses,
                 SUM(COALESCE(b.trophy_change, 0)) AS trophy_delta,
                 ROW_NUMBER() OVER (PARTITION BY b.mode_group ORDER BY COUNT(*) DESC) AS rn
          FROM battle_events b
          LEFT JOIN players p ON p.player_tag = b.player_tag
          LEFT JOIN player_current_state pcs ON pcs.player_tag = b.player_tag
          WHERE b.battle_time >= strftime('%Y%m%dT%H%M%S.000Z', 'now', ?)
            AND b.mode_group IS NOT NULL
          GROUP BY b.mode_group, b.player_tag
        )
        SELECT mode_group, name, league, battles, wins, losses, trophy_delta
        FROM m WHERE rn <= ? ORDER BY mode_group, battles DESC
        """,
        (f"-{days} day", per_mode),
    ).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        battles = int(r["battles"] or 0)
        entry = {
            "member_ref": r["name"],
            "battles": battles,
            "wins": int(r["wins"] or 0),
            "losses": int(r["losses"] or 0),
            "win_rate": round((r["wins"] or 0) / battles, 3) if battles else 0,
            "trophy_delta": int(r["trophy_delta"] or 0),
        }
        if r["mode_group"] == "ranked" and r["league"] is not None:
            entry["league"] = r["league"]
        out.setdefault(mode_group_label(r["mode_group"]), []).append(entry)
    return out


@managed_connection
def get_clan_game_mode_summary(
    days: int = 30,
    mode_group: Optional[str] = None,
    limit: int = 10,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    days = max(1, int(days or 30))
    limit = max(1, min(int(limit or 10), 50))
    # QA H12/M12: read the mode mix from the authoritative battle_events store,
    # not player_daily_battle_rollups — the rollups are lossy/stale, so mode_mix
    # (by_group) contradicted the battle_events-sourced ranked_activity in the
    # SAME payload (e.g. ranked 1152 vs 451). Same rolling window as
    # ranked_activity below so the two counts reconcile.
    where = ["b.battle_time >= strftime('%Y%m%dT%H%M%S.000Z', 'now', ?)"]
    params: list = [f"-{days} day"]
    if mode_group:
        where.append("b.mode_group = ?")
        params.append(mode_group)
    rows = conn.execute(
        "SELECT b.mode_group, b.game_mode_id, b.game_mode_name, COUNT(DISTINCT b.player_tag) AS members_active, "
        "COUNT(*) AS battles, SUM(b.outcome = 'W') AS wins, SUM(b.outcome = 'L') AS losses, SUM(b.outcome = 'D') AS draws, "
        "SUM(COALESCE(b.trophy_change, 0)) AS trophy_delta "
        "FROM battle_events b "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY b.mode_group, b.game_mode_id, b.game_mode_name "
        "ORDER BY battles DESC, b.mode_group ASC, COALESCE(b.game_mode_id, 0) ASC",
        tuple(params),
    ).fetchall()

    by_group: dict[str, dict] = {}
    by_game_mode = []
    for row in rows:
        group = row["mode_group"] or "other"
        group_bucket = by_group.setdefault(
            group,
            {
                "mode_group": group,
                "label": mode_group_label(group),
                "members_active": 0,
                "battles": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "trophy_delta": 0,
            },
        )
        group_bucket["members_active"] = max(
            group_bucket["members_active"], int(row["members_active"] or 0)
        )
        for key in ("battles", "wins", "losses", "draws"):
            group_bucket[key] += int(row[key] or 0)
        group_bucket["trophy_delta"] += int(row["trophy_delta"] or 0)
        by_game_mode.append(
            {
                "mode_group": group,
                "label": mode_group_label(group),
                "game_mode_id": row["game_mode_id"],
                "game_mode_name": row["game_mode_name"],
                "members_active": row["members_active"],
                "battles": row["battles"] or 0,
                "wins": row["wins"] or 0,
                "losses": row["losses"] or 0,
                "draws": row["draws"] or 0,
                "trophy_delta": row["trophy_delta"] or 0,
            }
        )
    for bucket in list(by_group.values()) + by_game_mode:
        bucket["win_rate"] = (
            round(bucket["wins"] / bucket["battles"], 4) if bucket["battles"] else None
        )

    member_count_rows = conn.execute(
        "SELECT b.mode_group, COUNT(DISTINCT b.player_tag) AS members_active "
        "FROM battle_events b "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY b.mode_group",
        tuple(params),
    ).fetchall()
    for row in member_count_rows:
        group = row["mode_group"] or "other"
        if group in by_group:
            by_group[group]["members_active"] = int(row["members_active"] or 0)

    ranked_members = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, COUNT(*) AS ranked_battles, "
        "SUM(CASE WHEN bf.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN bf.outcome = 'L' THEN 1 ELSE 0 END) AS losses, "
        "SUM(COALESCE(bf.trophy_change, 0)) AS trophy_delta, MAX(bf.league_number) AS max_league_seen, "
        "MAX(bf.battle_time) AS latest_ranked_battle "
        "FROM battle_events bf "
        "JOIN players m ON m.player_tag = bf.player_tag "
        "WHERE bf.is_ranked = 1 AND bf.battle_time >= strftime('%Y%m%dT%H%M%S.000Z', 'now', ?) "
        "GROUP BY bf.player_tag "
        "ORDER BY ranked_battles DESC, trophy_delta DESC, m.current_name COLLATE NOCASE "
        "LIMIT ?",
        (f"-{days} day", limit),
    ).fetchall()
    ranked_activity = []
    for row in ranked_members:
        item = dict(row)
        battles = int(item.get("ranked_battles") or 0)
        wins = int(item.get("wins") or 0)
        item["win_rate"] = round(wins / battles, 4) if battles else None
        ranked_activity.append(_member_reference_fields(conn, item.pop("member_id"), item))

    profile_rows = conn.execute(
        "SELECT m.player_tag AS member_id, m.player_tag AS tag, COALESCE(m.display_name, m.current_name) AS name, "
        "NULL AS current_path_of_legend_season_result_json, NULL AS progress_json, "
        "cs.trophies, cs.best_trophies, cs.ranked_league, cs.ranked_trophies "
        "FROM players m "
        "JOIN player_current_state cs ON cs.player_tag = m.player_tag "
        "WHERE EXISTS (SELECT 1 FROM clan_memberships cm "
        "  WHERE cm.player_tag = m.player_tag AND cm.left_at IS NULL)"
    ).fetchall()
    ranked_profiles = []
    progress_keys: dict[str, dict] = {}
    for row in profile_rows:
        pol = {
            "leagueNumber": row["ranked_league"],
            "trophies": row["ranked_trophies"],
            "rank": None,
        }
        if pol.get("leagueNumber") is not None:
            ranked_profiles.append(
                _member_reference_fields(
                    conn,
                    row["member_id"],
                    {
                        "tag": row["tag"],
                        "name": row["name"],
                        "league_number": pol.get("leagueNumber"),
                        "ranked_trophies": pol.get("trophies"),
                        "rank": pol.get("rank"),
                        "trophy_road_trophies": row["trophies"],
                        "best_trophies": row["best_trophies"],
                    },
                )
            )
        progress = _json_object(row["progress_json"])
        for key, value in progress.items():
            entry = progress_keys.setdefault(
                key,
                {
                    "progress_key": key,
                    "members": 0,
                    "max_trophies": None,
                    "max_best_trophies": None,
                    "top_member": None,
                },
            )
            entry["members"] += 1
            if isinstance(value, dict):
                trophies = value.get("trophies")
                best = value.get("bestTrophies")
                if isinstance(trophies, int) and (
                    entry["max_trophies"] is None or trophies > entry["max_trophies"]
                ):
                    entry["max_trophies"] = trophies
                    entry["top_member"] = row["name"]
                if isinstance(best, int) and (
                    entry["max_best_trophies"] is None or best > entry["max_best_trophies"]
                ):
                    entry["max_best_trophies"] = best
    ranked_profiles.sort(
        key=lambda item: (
            -(item.get("league_number") or 0),
            -(item.get("ranked_trophies") or 0),
            item.get("rank") is None,
            item.get("rank") or 999999,
            (item.get("name") or "").lower(),
        )
    )

    from storage.game_mode_contexts import list_game_mode_contexts

    event_badge_completions = _special_event_badge_completions(days, conn)
    event_participation = _special_event_participation(
        days,
        limit,
        conn,
    )

    return {
        "window_days": days,
        "mode_group": mode_group,
        "by_group": sorted(
            by_group.values(), key=lambda item: (-item["battles"], item["mode_group"])
        ),
        "by_game_mode": _special_event_activity(days, limit, conn)
        if mode_group == "special_event"
        else by_game_mode[:limit],
        "ranked_activity": ranked_activity,
        "ranked_profiles": ranked_profiles[:limit],
        "side_mode_progress": sorted(
            progress_keys.values(),
            key=lambda item: (-item["members"], item["progress_key"]),
        )[:limit],
        "event_participation": event_participation,
        "event_badge_completions": [
            completion
            for completions in event_badge_completions.values()
            for completion in completions
        ][:limit],
        "active_events": list_game_mode_contexts("event", limit=limit, conn=conn),
        "leaderboards": list_game_mode_contexts("leaderboard", limit=limit, conn=conn),
    }


@managed_connection
def _recompute_member_recent_form(member_id: int, conn=None):
    for scope, predicate in _LOSSES_SCOPE_PREDICATES.items():
        rows = conn.execute(
            f"SELECT outcome, crowns_for, crowns_against, trophy_change FROM battle_events WHERE player_tag = ? AND {predicate} ORDER BY battle_time DESC LIMIT 10",
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
        diffs = [
            (r["crowns_for"] or 0) - (r["crowns_against"] or 0)
            for r in rows
            if r["crowns_for"] is not None and r["crowns_against"] is not None
        ]
        trophy_changes = [r["trophy_change"] for r in rows if r["trophy_change"] is not None]
        avg_crown_diff = round(sum(diffs) / len(diffs), 2) if diffs else None
        avg_trophy_change = (
            round(sum(trophy_changes) / len(trophy_changes), 2) if trophy_changes else None
        )
        label = _build_form_label(wins, losses, sample_size)
        summary = _build_form_summary(wins, losses, draws, sample_size, label)
        conn.execute(
            "INSERT INTO player_recent_form (player_tag, computed_at, scope, sample_size, wins, losses, draws, current_streak, current_streak_type, win_rate, avg_crown_diff, avg_trophy_change, form_label, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(player_tag, scope) DO UPDATE SET computed_at = excluded.computed_at, sample_size = excluded.sample_size, wins = excluded.wins, losses = excluded.losses, draws = excluded.draws, current_streak = excluded.current_streak, current_streak_type = excluded.current_streak_type, win_rate = excluded.win_rate, avg_crown_diff = excluded.avg_crown_diff, avg_trophy_change = excluded.avg_trophy_change, form_label = excluded.form_label, summary = excluded.summary",
            (
                member_id,
                _utcnow(),
                scope,
                sample_size,
                wins,
                losses,
                draws,
                current_streak,
                streak_type,
                round(wins / sample_size, 4) if sample_size else 0,
                avg_crown_diff,
                avg_trophy_change,
                label,
                summary,
            ),
        )
    conn.commit()
