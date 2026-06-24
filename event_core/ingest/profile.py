"""Player profile ingest: raw /players payload -> Player.observe_profile."""
from __future__ import annotations

import hashlib
import json

from event_core.domain.player import PROFILE_SCALAR_FIELDS


def _indexed_items(items: list[dict] | None) -> dict[str, dict]:
    indexed = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            indexed[name] = item
    return indexed


def _nonnegative_int(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _badge_profile_fields(payload: dict) -> dict:
    if "badges" not in payload:
        return {}
    badges = _indexed_items(payload.get("badges") or [])
    years_played = badges.get("YearsPlayed") or {}
    collection_level = badges.get("CollectionLevel") or {}
    clan_wars_veteran = badges.get("ClanWarsVeteran") or {}
    return {
        "cr_account_age_days": _nonnegative_int(years_played.get("progress")),
        "cr_account_age_years": _nonnegative_int(years_played.get("level")),
        "cr_collection_level": _nonnegative_int(collection_level.get("progress")),
        "cr_collection_level_badge_tier": _nonnegative_int(collection_level.get("level")),
        "cr_collection_level_badge_max_tier": _nonnegative_int(collection_level.get("maxLevel")),
        "cr_clan_war_wins": _nonnegative_int((badges.get("ClanWarWins") or {}).get("progress")),
        "cr_battle_wins": _nonnegative_int((badges.get("BattleWins") or {}).get("progress")),
        "cr_clan_wars_veteran": _nonnegative_int(clan_wars_veteran.get("progress")),
        "cr_clan_wars_veteran_badge_tier": _nonnegative_int(clan_wars_veteran.get("level")),
        "cr_clan_wars_veteran_badge_max_tier": _nonnegative_int(clan_wars_veteran.get("maxLevel")),
        "cr_clan_donations": _nonnegative_int((badges.get("ClanDonations") or {}).get("progress")),
        "cr_banner_count": _nonnegative_int((badges.get("BannerCollection") or {}).get("progress")),
        "cr_emote_count": _nonnegative_int((badges.get("EmoteCollection") or {}).get("progress")),
    }


def build_profile_observation(payload: dict) -> dict:
    """Extract the tracked profile fields from a raw /players payload."""
    obs: dict[str, object] = {}
    for cr_key, attr in PROFILE_SCALAR_FIELDS.items():
        if cr_key in payload:
            obs[attr] = payload[cr_key]
    obs.update(_badge_profile_fields(payload))
    # Path-of-Legend (ranked ladder): flatten the live season result's three
    # subfields so they ride the same observation/diff/hash path as the scalars.
    # Only set when the payload carries the result, so a player with no PoL data
    # doesn't churn the content hash.
    if "currentPathOfLegendSeasonResult" in payload:
        pol = payload.get("currentPathOfLegendSeasonResult") or {}
        obs["pol_league_number"] = pol.get("leagueNumber")
        obs["pol_trophies"] = pol.get("trophies")
        obs["pol_rank"] = pol.get("rank")
    return obs


def profile_content_hash(observation: dict) -> str:
    """Deterministic content hash for dedup (mirrors the legacy snapshot slide)."""
    blob = json.dumps(observation, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ingest_player_payload(app, payload: dict, observed_at: str) -> bool:
    """Ingest one raw player payload. Returns True if it changed aggregate state."""
    tag = payload.get("tag")
    if not tag:
        return False
    observation = build_profile_observation(payload)
    content_hash = profile_content_hash(observation)
    return app.observe_player_profile(tag, observation, observed_at, content_hash)
