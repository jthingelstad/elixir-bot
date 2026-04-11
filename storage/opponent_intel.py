"""Opponent clan analysis for Clan Wars Intel Reports.

Pure data functions — takes CR API response dicts, returns structured analysis.
No Discord knowledge, no database access.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone


def _parse_cr_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        clean = value.split(".")[0]
        return datetime.strptime(clean, "%Y%m%dT%H%M%S")
    except (ValueError, TypeError):
        return None


def _hours_since(dt: datetime | None, now: datetime | None = None) -> float | None:
    if dt is None:
        return None
    ref = now or datetime.now(timezone.utc).replace(tzinfo=None)
    delta = ref - dt
    return max(0.0, delta.total_seconds() / 3600)


def analyze_clan_roster(clan_profile: dict, *, now: datetime | None = None) -> dict:
    """Analyze a single clan's roster from its /clans/{tag} API response.

    Returns a dict with roster metrics. Does not include war participant data.
    """
    members = clan_profile.get("memberList") or []
    member_count = len(members)

    trophies = [m.get("trophies", 0) for m in members]
    exp_levels = [m.get("expLevel", 0) for m in members]

    role_counts: dict[str, int] = {}
    recently_active = 0
    active_within_week = 0
    top_players = []

    for m in sorted(members, key=lambda x: x.get("trophies", 0), reverse=True):
        role = m.get("role", "member")
        role_counts[role] = role_counts.get(role, 0) + 1

        last_seen_dt = _parse_cr_time(m.get("lastSeen"))
        hours = _hours_since(last_seen_dt, now=now)
        if hours is not None:
            if hours <= 24:
                recently_active += 1
            if hours <= 168:
                active_within_week += 1

        if len(top_players) < 5:
            top_players.append({
                "name": m.get("name", "?"),
                "trophies": m.get("trophies", 0),
                "role": role,
                "exp_level": m.get("expLevel", 0),
            })

    return {
        "tag": clan_profile.get("tag", ""),
        "name": clan_profile.get("name", "Unknown"),
        "member_count": member_count,
        "max_members": 50,
        "clan_score": clan_profile.get("clanScore", 0),
        "war_trophies": clan_profile.get("clanWarTrophies", 0),
        "clan_type": clan_profile.get("type", "unknown"),
        "required_trophies": clan_profile.get("requiredTrophies", 0),
        "donations_per_week": clan_profile.get("donationsPerWeek", 0),
        "avg_trophies": round(statistics.mean(trophies), 0) if trophies else 0,
        "median_trophies": round(statistics.median(trophies), 0) if trophies else 0,
        "max_trophies": max(trophies) if trophies else 0,
        "avg_exp_level": round(statistics.mean(exp_levels), 1) if exp_levels else 0,
        "role_breakdown": role_counts,
        "recently_active_count": recently_active,
        "active_within_week_count": active_within_week,
        "top_players": top_players,
    }


def analyze_war_participants(clan_war_entry: dict) -> dict:
    """Analyze a clan's war participants from the currentriverrace clans[] entry.

    Returns a dict with war engagement metrics.
    """
    participants = clan_war_entry.get("participants") or []
    participant_count = len(participants)

    total_fame = sum(p.get("fame", 0) for p in participants)
    total_repair = sum(p.get("repairPoints", 0) for p in participants)
    total_decks = sum(p.get("decksUsed", 0) for p in participants)
    total_decks_today = sum(p.get("decksUsedToday", 0) for p in participants)

    active_participants = sum(1 for p in participants if p.get("decksUsed", 0) > 0)
    full_deck_today = sum(1 for p in participants if p.get("decksUsedToday", 0) >= 4)
    zero_deck_today = sum(1 for p in participants if p.get("decksUsedToday", 0) == 0)

    return {
        "tag": clan_war_entry.get("tag", ""),
        "name": clan_war_entry.get("name", "Unknown"),
        "fame": clan_war_entry.get("fame", 0),
        "repair_points": clan_war_entry.get("repairPoints", 0),
        "period_points": clan_war_entry.get("periodPoints", 0),
        "clan_score": clan_war_entry.get("clanScore", 0),
        "participant_count": participant_count,
        "total_fame": total_fame,
        "total_repair_points": total_repair,
        "total_decks_used": total_decks,
        "total_decks_today": total_decks_today,
        "active_participants": active_participants,
        "full_deck_today": full_deck_today,
        "zero_deck_today": zero_deck_today,
        "engagement_pct": round(active_participants / participant_count * 100, 1) if participant_count else 0,
        "avg_decks_used": round(total_decks / participant_count, 1) if participant_count else 0,
    }


def compute_threat_rating(roster: dict | None, war: dict | None) -> int:
    """Compute a 1-5 threat rating from combined roster and war metrics.

    Higher rating = more dangerous opponent.
    """
    score = 0.0
    weights_used = 0.0

    if roster:
        # War trophies (0-10 scale, max around 5000+)
        wt = min(roster.get("war_trophies", 0) / 500, 10)
        score += wt * 3
        weights_used += 3

        # Average trophies (0-10 scale, max around 8000+)
        at = min(roster.get("avg_trophies", 0) / 800, 10)
        score += at * 2
        weights_used += 2

        # Roster fullness
        member_pct = roster.get("member_count", 0) / roster.get("max_members", 50) * 10
        score += member_pct * 1
        weights_used += 1

        # Activity (recently active within 24h)
        mc = roster.get("member_count", 1) or 1
        activity_pct = roster.get("recently_active_count", 0) / mc * 10
        score += activity_pct * 2
        weights_used += 2

        # Donations
        don = min(roster.get("donations_per_week", 0) / 2000, 10)
        score += don * 1
        weights_used += 1

    if war:
        # Engagement percentage
        eng = war.get("engagement_pct", 0) / 10
        score += eng * 2
        weights_used += 2

    if weights_used == 0:
        return 1

    normalized = score / weights_used  # 0-10 scale
    rating = max(1, min(5, round(normalized / 2)))
    return rating


def build_intel_report(
    war_data: dict,
    clan_profiles: dict[str, dict | None],
    our_tag: str,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Build the full intel report for all competing clans.

    Args:
        war_data: Response from get_current_war()
        clan_profiles: tag (uppercase, no #) -> clan profile dict (or None if fetch failed)
        our_tag: Our clan's tag (no #)

    Returns a list of clan analysis dicts sorted by threat rating (highest first).
    Each dict has keys: tag, name, is_us, roster, war, threat_rating, profile_available.
    """
    our_tag_clean = our_tag.lstrip("#").upper()

    # Build war participant analysis from the war response
    war_clans = war_data.get("clans") or []
    our_war_entry = war_data.get("clan")
    if our_war_entry:
        war_clans_all = [our_war_entry] + [c for c in war_clans if c.get("tag", "").lstrip("#").upper() != our_tag_clean]
    else:
        war_clans_all = war_clans

    analyses = []
    for clan_entry in war_clans_all:
        tag = (clan_entry.get("tag") or "").lstrip("#").upper()
        is_us = tag == our_tag_clean

        war_analysis = analyze_war_participants(clan_entry)

        profile = clan_profiles.get(tag)
        roster_analysis = analyze_clan_roster(profile, now=now) if profile else None
        profile_available = profile is not None

        threat = compute_threat_rating(roster_analysis, war_analysis)

        analyses.append({
            "tag": f"#{tag}",
            "name": (profile or {}).get("name") or clan_entry.get("name") or "Unknown",
            "is_us": is_us,
            "roster": roster_analysis,
            "war": war_analysis,
            "threat_rating": threat,
            "profile_available": profile_available,
        })

    # Sort: our clan last, then by threat rating descending
    analyses.sort(key=lambda a: (a["is_us"], -a["threat_rating"]))

    return analyses


__all__ = [
    "analyze_clan_roster",
    "analyze_war_participants",
    "build_intel_report",
    "compute_threat_rating",
]
