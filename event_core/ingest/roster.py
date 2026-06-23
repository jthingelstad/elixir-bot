"""Roster ingest: raw /clans memberList -> Player.observe_roster_state.

Field extraction and defaults mirror storage.roster.snapshot_members exactly so
the rebuilt member_current_state projection matches the legacy table.
"""
from __future__ import annotations

import hashlib
import json



def build_roster_observation(member: dict) -> dict:
    """Extract member_current_state fields from one memberList entry."""
    arena = member.get("arena") or {}
    is_dict = isinstance(arena, dict)
    return {
        "role": member.get("role", "member"),
        "exp_level": member.get("expLevel", member.get("exp_level")),
        "trophies": member.get("trophies", 0),
        "best_trophies": member.get("bestTrophies", member.get("best_trophies")),
        "clan_rank": member.get("clanRank", member.get("clan_rank")),
        "donations_week": member.get("donations", 0),
        "donations_received_week": member.get(
            "donationsReceived", member.get("donations_received", 0)
        ),
        "arena_id": arena.get("id") if is_dict else None,
        "arena_name": arena.get("name") if is_dict else str(arena or ""),
        "arena_raw_name": arena.get("rawName") if is_dict else None,
        "last_seen_api": member.get("lastSeen", member.get("last_seen")),
    }


def roster_content_hash(observation: dict) -> str:
    blob = json.dumps(observation, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ingest_clan_payload(app, payload: dict, observed_at: str) -> int:
    """Ingest one /clans payload's roster. Returns count of members that changed."""
    members = payload.get("memberList") or []
    changed = 0
    for member in members:
        tag = member.get("tag")
        if not tag:
            continue
        obs = build_roster_observation(member)
        if app.observe_member_roster(tag, obs, observed_at, roster_content_hash(obs)):
            changed += 1
    return changed
