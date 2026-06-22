"""Player profile ingest: raw /players payload -> Player.observe_profile."""
from __future__ import annotations

import hashlib
import json

from event_core.domain.player import PROFILE_SCALAR_FIELDS


def build_profile_observation(payload: dict) -> dict:
    """Extract the tracked scalar profile fields from a raw /players payload."""
    obs: dict[str, object] = {}
    for cr_key, attr in PROFILE_SCALAR_FIELDS.items():
        if cr_key in payload:
            obs[attr] = payload[cr_key]
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
