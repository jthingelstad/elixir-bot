"""Clan Wars Intel Report formatter.

Converts structured analysis dicts into Discord-ready messages.
Uses lists (not tables) for Discord compatibility.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytz

CHICAGO = pytz.timezone("America/Chicago")

_THREAT_STARS = {1: "\u2605\u2606\u2606\u2606\u2606", 2: "\u2605\u2605\u2606\u2606\u2606", 3: "\u2605\u2605\u2605\u2606\u2606", 4: "\u2605\u2605\u2605\u2605\u2606", 5: "\u2605\u2605\u2605\u2605\u2605"}

_TYPE_LABELS = {"open": "Open", "inviteOnly": "Invite Only", "closed": "Closed"}


def _fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _role_summary(breakdown: dict) -> str:
    parts = []
    for role, label in [("leader", "Leader"), ("coLeader", "Co-Leader"), ("elder", "Elder"), ("member", "Member")]:
        count = breakdown.get(role, 0)
        if count:
            parts.append(f"{count} {label}{'s' if count != 1 else ''}")
    return ", ".join(parts) if parts else "Unknown"


def _format_clan_block(analysis: dict, brief: str | None = None) -> str:
    """Format a single competing clan's analysis into a Discord message block."""
    name = analysis["name"]
    tag = analysis["tag"]
    threat = max(1, min(5, int(analysis.get("threat_rating", 1) or 1)))
    fire = "\U0001f525" * threat
    roster = analysis.get("roster")
    war = analysis.get("war")
    profile_available = analysis.get("profile_available", False)

    tag_clean = tag.lstrip("#")
    header = (
        f"\u2694\ufe0f **[{name}](https://royaleapi.com/clan/{tag_clean})** "
        f"({tag}) {fire}"
    )
    lines = [header]

    if brief:
        lines.append(f"_{brief.strip()}_")

    if roster and profile_available:
        lines.append(
            f"- Roster: {roster['member_count']}/{roster['max_members']} members "
            f"| Avg {_fmt_num(roster['avg_trophies'])} trophies "
            f"| Avg Lvl {roster['avg_exp_level']}"
        )
        lines.append(
            f"- War Trophies: {_fmt_num(roster['war_trophies'])} "
            f"| Clan Score: {_fmt_num(roster['clan_score'])}"
        )
        clan_type = _TYPE_LABELS.get(roster.get("clan_type", ""), roster.get("clan_type", "?"))
        lines.append(
            f"- Type: {clan_type} "
            f"| Required: {_fmt_num(roster['required_trophies'])} trophies"
        )
        mc = roster["member_count"] or 1
        active_pct = round(roster["recently_active_count"] / mc * 100)
        lines.append(
            f"- Activity: {roster['recently_active_count']}/{roster['member_count']} "
            f"active in last 24h ({active_pct}%)"
        )
        lines.append(f"- Donations: {_fmt_num(roster['donations_per_week'])}/week")
        lines.append(f"- Leadership: {_role_summary(roster['role_breakdown'])}")
    elif not profile_available:
        lines.append("- *Clan profile unavailable — showing war data only*")

    if war:
        pc = war["participant_count"]
        lines.append(
            f"- War Engagement: {war['active_participants']}/{pc} participants "
            f"| {war['engagement_pct']}% active"
        )
        if war.get("total_fame", 0) > 0 or war.get("total_decks_used", 0) > 0:
            lines.append(
                f"- Season Progress: {_fmt_num(war['total_fame'])} fame "
                f"| {_fmt_num(war['total_decks_used'])} decks used"
            )

    if roster and roster.get("top_players"):
        top = ", ".join(
            f"{p['name']} ({_fmt_num(p['trophies'])})"
            for p in roster["top_players"][:5]
        )
        lines.append(f"- Top Players: {top}")

    return "\n".join(lines)


def format_intel_report(
    clan_analyses: list[dict],
    *,
    season_id: int | None = None,
    llm_summary: str | None = None,
    clan_briefs: dict[str, str] | None = None,
) -> list[str]:
    """Format the full intel report as a list of Discord messages.

    Returns one header message, optionally an LLM summary message,
    then one message per competing clan (excluding our clan).

    ``clan_briefs`` maps clan tag (with ``#`` prefix, matching ``analysis["tag"]``)
    to a short snarky recap string that is rendered in italics under the header.
    """
    now = datetime.now(timezone.utc).astimezone(CHICAGO)
    timestamp = now.strftime("%b %d, %Y at %I:%M %p CT")

    opponents = [a for a in clan_analyses if not a.get("is_us")]
    opponent_count = len(opponents)

    season_label = f" — Season {season_id}" if season_id else ""
    header_parts = [
        f"**Clan Wars Intel Report{season_label}**",
        f"{opponent_count} competing clans analyzed. Generated {timestamp}.",
    ]
    if llm_summary:
        header_parts.append("")
        header_parts.append(llm_summary)

    messages = ["\n".join(header_parts)]

    briefs = clan_briefs or {}
    for analysis in opponents:
        messages.append(_format_clan_block(analysis, brief=briefs.get(analysis["tag"])))

    return messages


def format_intel_summary_for_memory(clan_analyses: list[dict]) -> str:
    """Format a condensed summary suitable for storing as a memory.

    Returns a single text string the LLM can use as context input.
    """
    opponents = [a for a in clan_analyses if not a.get("is_us")]
    parts = []
    for a in opponents:
        stars = _THREAT_STARS.get(a.get("threat_rating", 1), "?")
        roster = a.get("roster") or {}
        war = a.get("war") or {}
        mc = roster.get("member_count", "?")
        avg_t = _fmt_num(roster.get("avg_trophies", 0)) if roster else "?"
        wt = _fmt_num(roster.get("war_trophies", 0)) if roster else "?"
        eng = f"{war.get('engagement_pct', 0)}%" if war else "?"
        parts.append(
            f"{a['name']} ({a['tag']}): {stars}, "
            f"{mc} members, avg {avg_t} trophies, "
            f"war trophies {wt}, engagement {eng}"
        )
    return "; ".join(parts)


__all__ = [
    "format_intel_report",
    "format_intel_summary_for_memory",
]
