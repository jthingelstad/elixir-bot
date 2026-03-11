import json

import db

from agent import app as _app
from agent.core import (
    MAX_CONTEXT_MEMBERS_DEFAULT,
    MAX_CONTEXT_MEMBERS_FULL,
    _create_chat_completion,
    log,
)
from agent.chat import _clan_context, _format_memory_context, _format_recent_posts, _parse_response
from agent.prompts import (
    _clanops_system,
    _event_system,
    _home_message_system,
    _interactive_system,
    _members_message_system,
    _observe_system,
    _promote_system,
    _reception_system,
    _roster_bios_system,
    _weekly_digest_system,
)
from agent.tool_policy import RESPONSE_SCHEMAS_BY_WORKFLOW, TOOLSETS_BY_WORKFLOW


def _chat_with_tools(*args, **kwargs):
    return _app._chat_with_tools(*args, **kwargs)


def _clan_trend_prompt_context(days=30, window_days=7):
    try:
        return db.build_clan_trend_summary_context(days=days, window_days=window_days) or ""
    except Exception as exc:
        log.warning("Clan trend summary context unavailable: %s", exc)
        return ""


def _roster_bio_context(clan_data, roster_data=None):
    members = roster_data.get("members", []) if roster_data else clan_data.get("memberList", clan_data.get("members", []))
    if not members:
        return ""

    lines = ["=== MEMBER PROFILE SNAPSHOT ==="]
    for member in members:
        tag = member.get("tag", "")
        canon_tag = tag if str(tag).startswith("#") else f"#{tag}"
        try:
            overview = db.get_member_overview(canon_tag)
        except Exception:
            overview = None
        if not overview:
            continue

        line = [
            f"- {overview.get('member_name') or member.get('name') or canon_tag}",
            f"tag: {overview.get('player_tag') or canon_tag}",
            f"role: {overview.get('role') or member.get('role') or 'member'}",
            f"trophies: {overview.get('trophies') or 0:,}",
        ]
        if overview.get("best_trophies"):
            line.append(f"best_trophies: {overview.get('best_trophies'):,}")
        if overview.get("joined_date"):
            line.append(f"joined_date: {overview.get('joined_date')}")
        if overview.get("donations_week") is not None:
            line.append(f"weekly_donations: {overview.get('donations_week')}")
        if overview.get("career_wins") is not None:
            line.append(
                f"career_record: {overview.get('career_wins') or 0} wins / {overview.get('career_losses') or 0} losses"
            )
        if overview.get("three_crown_wins") is not None:
            line.append(f"three_crown_wins: {overview.get('three_crown_wins') or 0}")
        if overview.get("war_day_wins") is not None:
            line.append(f"war_day_wins: {overview.get('war_day_wins') or 0}")
        if overview.get("current_favourite_card_name"):
            line.append(f"favorite_card: {overview.get('current_favourite_card_name')}")

        recent_form = overview.get("recent_form") or {}
        if recent_form:
            line.append(f"recent_form: {recent_form.get('summary')}")

        signature_cards = ((overview.get("signature_cards") or {}).get("cards") or [])
        if signature_cards:
            top_cards = ", ".join(card.get("name", "") for card in signature_cards[:3] if card.get("name"))
            if top_cards:
                line.append(f"signature_cards: {top_cards}")

        current_deck = ((overview.get("current_deck") or {}).get("cards") or [])
        if current_deck:
            deck_cards = ", ".join(card.get("name", "") for card in current_deck[:4] if card.get("name"))
            if deck_cards:
                line.append(f"current_deck_sample: {deck_cards}")

        war_status = overview.get("war_status") or {}
        war_season = war_status.get("season") or {}
        if war_season:
            line.append(
                "current_season_war: "
                f"{war_season.get('races_played') or 0} races, "
                f"{war_season.get('total_fame') or 0:,} fame, "
                f"{war_season.get('total_decks_used') or 0} decks"
            )

        if overview.get("bio"):
            line.append(f"existing_bio: {overview.get('bio')}")
        if overview.get("profile_highlight"):
            line.append(f"existing_highlight: {overview.get('profile_highlight')}")
        if member.get("note"):
            line.append(f"manual_note: {member.get('note')}")
        lines.append(" | ".join(line))

    return "\n".join(lines)


def _promotion_context(clan_data, war_data, roster_data=None):
    members = clan_data.get("memberList", clan_data.get("members", [])) or []
    lines = []

    clan_name = clan_data.get("name", "POAP KINGS")
    clan_tag = clan_data.get("tag", "#J2RGCRVG")
    member_count = clan_data.get("members", len(members))
    required_trophies = clan_data.get("requiredTrophies", 0)
    donations_per_week = clan_data.get("donationsPerWeek", 0)
    clan_score = clan_data.get("clanScore", 0)
    clan_war_trophies = clan_data.get("clanWarTrophies", 0)
    war_league = ((clan_data.get("warLeague") or {}).get("name")) or "Unknown"
    total_trophies = sum((member.get("trophies") or 0) for member in members)
    avg_level = round(
        sum((member.get("expLevel") or 0) for member in members) / len(members),
        1,
    ) if members else 0

    lines.append("=== CLAN SNAPSHOT ===")
    lines.append(f"name: {clan_name}")
    lines.append(f"tag: {clan_tag}")
    lines.append(f"members: {member_count}")
    lines.append(f"required_trophies: {required_trophies}")
    lines.append(f"combined_trophies: {total_trophies}")
    lines.append(f"avg_member_level: {avg_level}")
    lines.append(f"weekly_donations: {donations_per_week}")
    lines.append(f"clan_score: {clan_score}")
    lines.append(f"clan_war_trophies: {clan_war_trophies}")
    lines.append(f"war_league: {war_league}")

    trophy_leaders = sorted(
        members,
        key=lambda member: (member.get("trophies") or 0, -(member.get("clanRank") or 999)),
        reverse=True,
    )[:5]
    if trophy_leaders:
        lines.append("\n=== TROPHY LEADERS ===")
        for member in trophy_leaders:
            lines.append(
                f"- {member.get('name')} | {member.get('trophies', 0):,} trophies | "
                f"role: {member.get('role', 'member')}"
            )

    donation_leaders = [
        member for member in sorted(
            members,
            key=lambda member: (member.get("donations") or 0, member.get("trophies") or 0),
            reverse=True,
        )
        if (member.get("donations") or 0) > 0
    ][:5]
    if donation_leaders:
        lines.append("\n=== DONATION LEADERS ===")
        for member in donation_leaders:
            lines.append(
                f"- {member.get('name')} | {member.get('donations', 0)} donations | "
                f"{member.get('trophies', 0):,} trophies"
            )

    if roster_data:
        spotlight_candidates = sorted(
            roster_data.get("members", []),
            key=lambda member: (
                {"Leader": 3, "Co-Leader": 3, "Elder": 2, "Member": 1}.get(member.get("role"), 0),
                len(member.get("favorite_cards") or []),
                member.get("trophies") or 0,
                member.get("donations") or 0,
            ),
            reverse=True,
        )
        if spotlight_candidates:
            lines.append("\n=== MEMBER SPOTLIGHTS WITH SIGNATURE CARDS ===")
            for member in spotlight_candidates[:5]:
                favorite_cards = ", ".join(card.get("name", "") for card in (member.get("favorite_cards") or [])[:2] if card.get("name"))
                bio = (member.get("bio") or "").strip()
                bio_preview = bio.split(". ")[0].strip() if bio else ""
                line = f"- {member.get('name')}"
                if favorite_cards:
                    line += f" | signature cards: {favorite_cards}"
                line += (
                    f" | role: {member.get('role')} | "
                    f"{member.get('trophies', 0):,} trophies | donations: {member.get('donations', 0)}"
                )
                if member.get("highlight"):
                    line += f" | highlight: {member.get('highlight')}"
                if bio_preview:
                    line += f" | bio: {bio_preview}"
                lines.append(line)

    try:
        season_summary = db.get_war_season_summary(top_n=5)
    except Exception:
        season_summary = None
    if season_summary:
        lines.append("\n=== WAR SEASON LEADERS ===")
        for member in (season_summary.get("top_contributors") or [])[:5]:
            lines.append(
                f"- {member.get('member_name') or member.get('name')} | "
                f"{member.get('total_fame', 0):,} fame | races: {member.get('races_participated', 0)}"
            )

    return "\n".join(lines)

def observe_and_post(clan_data, war_data, signals=None, recent_posts=None, memory_context=None):
    """Observation with signals from heartbeat. Returns dict or None.

    signals: list of signal dicts from heartbeat.tick(), or None when no detector output is being passed.
    recent_posts: list of recent message dicts from db.list_channel_messages().
    """
    context = _clan_context(clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT)

    if signals:
        signals_text = json.dumps(signals, indent=2, default=str)
        user_msg = (
            f"=== HEARTBEAT SIGNALS ===\n{signals_text}\n\n"
            f"{context}"
        )
    else:
        user_msg = context

    user_msg += _format_recent_posts(recent_posts)
    user_msg += _format_memory_context(memory_context)

    return _chat_with_tools(
        _observe_system(), user_msg,
        workflow="observation",
        allowed_tools=TOOLSETS_BY_WORKFLOW["observe"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["observation"],
        strict_json=True,
    )


def respond_in_reception(question, author_name, clan_data, memory_context=None):
    """Onboarding Q&A in #reception. No tools needed. Returns dict or None."""
    members = clan_data.get("memberList", clan_data.get("members", []))
    roster = "\n".join(
        f"  {m.get('name', '?')} ({m.get('tag', '?')})"
        for m in members
    ) or "  (roster unavailable)"
    user_msg = (
        f"New member '{author_name}' asks: {question}\n\n"
        f"=== CLAN ROSTER ===\n{roster}"
    )
    user_msg += _format_memory_context(memory_context)
    return _chat_with_tools(
        _reception_system(), user_msg,
        temperature=0.7, max_tokens=400,
        workflow="reception",
        allowed_tools=TOOLSETS_BY_WORKFLOW["reception"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["reception"],
        strict_json=True,
        return_errors=True,
    )


def respond_in_channel(question, author_name, channel_name, workflow, clan_data, war_data,
                       conversation_history=None, memory_context=None, proactive=False):
    """Channel Q&A for interactive/clanops workflows."""
    if workflow not in {"interactive", "clanops"}:
        raise ValueError(f"unsupported channel workflow: {workflow}")
    context = _clan_context(clan_data, war_data, max_members=MAX_CONTEXT_MEMBERS_DEFAULT)
    trend_context = _clan_trend_prompt_context()
    speaker = "Observed message from" if proactive else "Message from"
    user_msg = f"{speaker} '{author_name}' in {channel_name}: {question}\n\n{context}"
    if trend_context:
        user_msg += f"\n\n{trend_context}"
    user_msg += _format_memory_context(memory_context)
    workflow_key = f"{workflow}_proactive" if proactive else workflow
    system_prompt = (
        _interactive_system(channel_name, proactive=proactive)
        if workflow == "interactive"
        else _clanops_system(channel_name, proactive=proactive)
    )
    return _chat_with_tools(
        system_prompt,
        user_msg,
        conversation_history=conversation_history,
        workflow=workflow_key,
        allowed_tools=TOOLSETS_BY_WORKFLOW[workflow_key],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW[workflow_key],
        strict_json=True,
        return_errors=True,
    )


# ── Event-driven message generation ──────────────────────────────────────────

def generate_message(event, context, recent_posts=None):
    """Generate a single Discord message for an event using the LLM.

    event: short description of what happened (e.g. "new_member_discord_join")
    context: string with all relevant details for the LLM
    recent_posts: optional list of recent post dicts to avoid repetition.

    Returns message text, or None on failure.
    """
    user_msg = f"Event: {event}\n\n{context}"
    user_msg += _format_recent_posts(recent_posts)
    messages = [
        {"role": "system", "content": _event_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow=f"event:{event}",
            messages=messages,
            temperature=0.7,
            max_tokens=300,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("generate_message error (%s): %s", event, e)
        return None


# ── Site content generation for poapkings.com ────────────────────────────────

def generate_home_message(clan_data, war_data, previous_message, roster_data=None):
    """Generate a message for the poapkings.com home page. Returns text or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    prev_text = f"Your previous message: {previous_message}" if previous_message else "(none yet)"
    user_msg = f"{context}\n\n{prev_text}\n\nWrite your next message for the home page."

    messages = [
        {"role": "system", "content": _home_message_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="site_home_message",
            messages=messages,
            temperature=0.9,
            max_tokens=300,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("Home message API error: %s", e)
        return None


def generate_members_message(clan_data, war_data, previous_message, roster_data=None):
    """Generate a message for the poapkings.com members page. Returns text or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    prev_text = f"Your previous message: {previous_message}" if previous_message else "(none yet)"
    user_msg = f"{context}\n\n{prev_text}\n\nWrite your next message for the members page."

    messages = [
        {"role": "system", "content": _members_message_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="site_members_message",
            messages=messages,
            temperature=0.9,
            max_tokens=400,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("Members message API error: %s", e)
        return None


def generate_roster_bios(clan_data, war_data, roster_data=None):
    """Generate roster intro and per-member bios. Returns dict or None."""
    context = _clan_context(
        clan_data, war_data,
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    member_profile_context = _roster_bio_context(clan_data, roster_data=roster_data)
    members = clan_data.get("memberList", clan_data.get("members", []))
    member_tags = [m.get("tag", "") for m in members]
    user_msg = (
        f"{context}\n\n"
        f"{member_profile_context}\n\n"
        f"Generate an intro and bio for each member.\n"
        f"Member tags to cover: {', '.join(member_tags)}"
    )
    return _chat_with_tools(
        _roster_bios_system(), user_msg,
        temperature=0.8, max_tokens=2000,
        workflow="roster_bios",
        allowed_tools=TOOLSETS_BY_WORKFLOW["roster_bios"],
        response_schema=RESPONSE_SCHEMAS_BY_WORKFLOW["roster_bios"],
        strict_json=True,
    )


def generate_promote_content(clan_data, war_data=None, roster_data=None):
    """Generate promotional messages for 5 channels. Returns dict or None."""
    context = _clan_context(
        clan_data, war_data or {},
        roster_data=roster_data,
        max_members=MAX_CONTEXT_MEMBERS_FULL,
    )
    promotion_context = _promotion_context(clan_data, war_data or {}, roster_data=roster_data)
    user_msg = (
        f"{context}\n\n"
        f"{promotion_context}\n\n"
        "Generate promotional messages for all 5 channels. Use the promotion snapshot heavily."
    )

    messages = [
        {"role": "system", "content": _promote_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="site_promote_content",
            messages=messages,
            temperature=0.8,
            max_tokens=1500,
            timeout=60,
        )
        return _parse_response(resp.choices[0].message.content or "null")
    except Exception as e:
        log.error("Promote API error: %s", e)
        return None


def generate_weekly_digest(summary_context, previous_message=""):
    """Generate a long-form weekly clan recap for Discord. Returns text or None."""
    prev_text = f"Your previous weekly recap: {previous_message}" if previous_message else "(no previous recap provided)"
    user_msg = f"{summary_context}\n\n{prev_text}\n\nWrite this week's clan recap."
    messages = [
        {"role": "system", "content": _weekly_digest_system()},
        {"role": "user", "content": user_msg},
    ]
    try:
        resp = _create_chat_completion(
            workflow="weekly_digest",
            messages=messages,
            temperature=0.8,
            max_tokens=1200,
            timeout=60,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.lower() == "null":
            return None
        return text
    except Exception as e:
        log.error("Weekly digest API error: %s", e)
        return None

__all__ = [
    "observe_and_post",
    "respond_in_reception",
    "respond_in_channel",
    "generate_message",
    "generate_home_message",
    "generate_members_message",
    "generate_roster_bios",
    "generate_promote_content",
    "generate_weekly_digest",
]
