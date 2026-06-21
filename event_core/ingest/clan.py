"""Clan-level ingest: raw /clans payload -> Clan.observe_state.

Field extraction mirrors legacy storage.roster.snapshot_clan_daily_metrics
exactly (top-level clan fields + memberList aggregates) so the rebuilt
clan_daily_metrics projection matches the legacy table on the observable fields.

The ingest function takes an Application instance and does get-or-create + save
itself (same contract as ingest.roster.ingest_clan_payload).
"""
from __future__ import annotations

import hashlib
import json

# Default clan identity matches legacy snapshot fallbacks (the one tracked clan).
DEFAULT_CLAN_TAG = "#J2RGCRVG"
DEFAULT_CLAN_NAME = "POAP KINGS"
MAX_CLAN_SIZE = 50  # legacy open_slots = max(0, 50 - member_count)


def build_clan_observation(clan_data: dict) -> dict:
    """Extract clan_daily_metrics observable fields from one /clans payload.

    Mirrors snapshot_clan_daily_metrics line-for-line:
      - member_count: payload `members` if int, else len(memberList)
      - open_slots:   max(0, 50 - member_count)
      - aggregates summed/avg/max across memberList trophies & donations
    """
    clan_data = clan_data or {}
    member_list = clan_data.get("memberList") or []

    member_count = clan_data.get("members")
    if not isinstance(member_count, int):
        member_count = len(member_list)

    total_member_trophies = sum((m.get("trophies") or 0) for m in member_list)
    avg_member_trophies = (
        round(total_member_trophies / member_count, 2) if member_count else 0.0
    )
    top_member_trophies = (
        max((m.get("trophies") or 0) for m in member_list) if member_list else 0
    )
    weekly_donations_total = sum((m.get("donations") or 0) for m in member_list)

    return {
        "clan_name": clan_data.get("name") or DEFAULT_CLAN_NAME,
        "member_count": member_count,
        "open_slots": max(0, MAX_CLAN_SIZE - member_count),
        "clan_score": clan_data.get("clanScore"),
        "clan_war_trophies": clan_data.get("clanWarTrophies"),
        "required_trophies": clan_data.get("requiredTrophies"),
        "donations_per_week_requirement": clan_data.get("donationsPerWeek"),
        "weekly_donations_total": weekly_donations_total,
        "total_member_trophies": total_member_trophies,
        "avg_member_trophies": avg_member_trophies,
        "top_member_trophies": top_member_trophies,
    }


def clan_content_hash(observation: dict) -> str:
    blob = json.dumps(observation, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_clan_tag(clan_data: dict, entity_key: str | None = None) -> str:
    """Canonical clan tag from the payload (legacy fallback), or entity_key."""
    tag = (clan_data or {}).get("tag") or entity_key or DEFAULT_CLAN_TAG
    return tag


def ingest_clan_state(
    app, payload: dict, observed_at: str, entity_key: str | None = None
) -> bool:
    """Ingest one /clans payload's clan-level state. Returns True if it changed.

    get-or-create by clan tag + content-hash dedup + save, all here.
    """
    tag = resolve_clan_tag(payload, entity_key)
    obs = build_clan_observation(payload)
    return app.observe_clan_state(tag, obs, observed_at, clan_content_hash(obs))
