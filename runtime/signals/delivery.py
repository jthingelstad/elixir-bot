"""Signal delivery entrypoints."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import db
import elixir_agent
from runtime.leader_action_policy import can_post_leader_action
from runtime.leader_action_ui import LEADER_ACTION_UI_VERSION, post_leader_action_card
from runtime.channel_subagents import (
    SEASON_AWARDS_SIGNAL_TYPES,
    build_subagent_memory_context,
    is_leadership_only_signal,
    signal_source_key,
)
from runtime.helpers import _channel_scope

log = logging.getLogger("elixir")

ARENA_RELAY_COOLDOWN_HOURS = 18
ARENA_RELAY_MAX_COPY_CHARS = 240
ARENA_RELAY_WELCOME_MAX_COPY_CHARS = 120
ARENA_RELAY_MAX_NUDGE_ACTIONS = 3
PUBLIC_DEPARTURE_MIN_TENURE_DAYS = 14
WAR_NUDGE_SIGNAL_TYPES = {
    "war_battle_phase_active",
    "war_battle_day_started",
    "war_battle_day_live_update",
    "war_battle_day_final_hours",
    "war_final_battle_day",
}
CRITICAL_LEADER_ACTION_SIGNAL_TYPES = {
    "war_battle_day_final_hours",
    "war_final_battle_day",
}
CELEBRATION_RELAY_SIGNAL_TYPES = {
    "career_wins_milestone",
    "cr_account_anniversary",
    "new_champion_unlocked",
    "new_card_unlocked",
    "player_level_up",
    "best_trophies_peak",
    "challenge_performance_milestone",
    "join_anniversary",
    "member_birthday",
    "clan_birthday",
}
DISCORD_INVITE_ROUTE = "POAPKINGS . COM > Members"


def _memory_context_with_leader_action_feedback(memory_context: dict | None, profiles: list[dict] | None) -> dict | None:
    profiles = profiles or []
    if not profiles:
        return memory_context
    merged = dict(memory_context or {})
    durable = list(merged.get("durable_memories") or [])
    existing_ids = {item.get("memory_id") for item in durable if isinstance(item, dict)}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        memory_id = profile.get("memory_id")
        if memory_id is not None and memory_id in existing_ids:
            continue
        durable.append(profile)
    merged["durable_memories"] = durable
    return merged


def _clip_relay_copy(text: str, limit: int = ARENA_RELAY_MAX_COPY_CHARS) -> str:
    body = " ".join((text or "").split())
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 3)].rstrip() + "..."


def _ordinal(value) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _signal_names(signals: list[dict], limit: int = 4) -> list[str]:
    names = []
    for signal in signals or []:
        name = str(signal.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _discord_member_counts() -> tuple[int | None, int | None]:
    try:
        members = db.list_members()
    except Exception:
        return None, None
    active = len(members)
    linked = sum(1 for member in members if member.get("in_discord"))
    return linked, active


def _discord_invite_relay_context(base_context: str | None) -> str:
    linked, active = _discord_member_counts()
    if isinstance(linked, int) and isinstance(active, int) and active > 0:
        count_line = f"- Discord-linked clan members: {linked}/{active}"
    else:
        count_line = "- Discord-linked clan member count: unavailable"
    instructions = (
        "Arena-relay Discord invite task:\n"
        f"{count_line}\n"
        "- Author 2-3 short Clash Royale clan-chat messages a leader can copy/paste in sequence.\n"
        "- Highlight why Discord is worth joining: war nudges, deck/screenshot help, milestone shoutouts, leader relay notes, or recent useful coordination.\n"
        f"- Include `{DISCORD_INVITE_ROUTE}` exactly once, preferably in the final copy/paste message.\n"
        "- Do not include raw URLs, markdown links, Discord-only formatting, message numbers, or labels inside the copy/paste messages.\n"
        f"- Keep each copy/paste message under {ARENA_RELAY_MAX_COPY_CHARS} characters.\n"
        "- Return `content` as a JSON array containing only the Clash Royale copy/paste messages. The code will add the arena-relay action card.\n"
    )
    if base_context:
        return f"{base_context}\n\n{instructions}"
    return instructions


def _result_content_items(result: dict | None) -> list[str]:
    if not isinstance(result, dict):
        return []
    content = result.get("content")
    if isinstance(content, list):
        return [str(item or "").strip() for item in content if str(item or "").strip()]
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    return []


def _build_generated_discord_invite_relay_result(signals: list[dict], generated: dict | None) -> dict | None:
    copies = [_clip_relay_copy(item) for item in _result_content_items(generated)]
    copies = [item for item in copies if item]
    if not copies:
        return None
    copies = copies[:3]
    relay_copy_text = "\n".join(copies)
    relay_copy_lower = relay_copy_text.lower()
    if "http://" in relay_copy_lower or "https://" in relay_copy_lower:
        return None
    if relay_copy_lower.count(DISCORD_INVITE_ROUTE.lower()) != 1:
        return None
    copy_count = len(copies)
    copy_instruction = (
        "📋 Copy the next message into Clash Royale."
        if copy_count == 1
        else f"📋 Copy the next {copy_count} messages into Clash Royale in order."
    )
    reason = "A weekly no-link reminder helps clan members see why Discord matters and find it through the website members page."
    card = (
        "**R? 💬 Discord invite relay**\n"
        "🎯 `discord_onboarding`\n"
        f"{copy_instruction}\n"
        f"🧠 {reason}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )
    relay_copy = copies[0] if copy_count == 1 else copies
    return {
        "event_type": "discord_invite_relay",
        "summary": f"In-game relay suggestion: {relay_copy_text}",
        "content": [card, *copies],
        "metadata": {
            "action_type": "discord_invite_relay",
            "objective": "discord_onboarding",
            "rationale": reason,
            "relay_copy": relay_copy,
            "relay_copy_text": relay_copy_text,
            "relay_copy_count": copy_count,
            "relay_target": "clash_royale_clan_chat",
            "copy_message_index": 1,
            "authored_by": "elixir_agent",
        },
    }


def _profile_number(value) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _member_join_profile(signal: dict) -> dict:
    tag = signal.get("tag") or signal.get("player_tag")
    if tag:
        try:
            return db.get_member_profile(tag) or {}
        except Exception:
            return {}
    return {}


def _member_join_profile_facts(signal: dict) -> list[str]:
    profile = _member_join_profile(signal)
    facts = []
    tag = signal.get("tag") or signal.get("player_tag")
    name = str(signal.get("name") or "").strip()
    if name:
        facts.append(f"- Name: {name}")
    if tag:
        facts.append(f"- Player tag: {tag}")

    war_wins = _profile_number(profile.get("cr_clan_war_wins"))
    if war_wins:
        facts.append(f"- Clan war wins: {war_wins:,}")

    battle_wins = _profile_number(profile.get("cr_battle_wins"))
    if battle_wins:
        facts.append(f"- Battle wins: {battle_wins:,}")

    collection_level = _profile_number(profile.get("cr_collection_level"))
    if collection_level:
        facts.append(f"- Collection Level: {collection_level:,}")

    trophies = _profile_number(profile.get("trophies") or profile.get("current_trophies"))
    if trophies:
        facts.append(f"- Current trophies: {trophies:,}")

    best_trophies = _profile_number(profile.get("best_trophies"))
    if best_trophies:
        facts.append(f"- Best trophies: {best_trophies:,}")

    return facts


def _member_join_welcome_context(base_context: str | None, signal: dict) -> str:
    facts = _member_join_profile_facts(signal)
    fact_block = "\n".join(facts) if facts else "- No profile facts available beyond the join signal."
    instructions = (
        "Arena-relay new-member welcome task:\n"
        f"{fact_block}\n"
        "- Author one short Clash Royale clan-chat welcome a leader can copy/paste.\n"
        "- Include the member name exactly as provided when available.\n"
        "- Sound like a real leader typing in Clash Royale clan chat, not a polished announcement.\n"
        "- Use one grounded profile fact when facts are available, but keep it casual.\n"
        "- Do not mention war state, boat defenses, Discord, onboarding, instructions, or what the player should do next.\n"
        "- Avoid corporate/promo phrases like 'serious battle experience', 'bring that energy', or 'we are looking for'.\n"
        "- Do not invent achievements, personality, role, Discord status, or future behavior.\n"
        "- Do not include raw URLs, markdown links, Discord-only formatting, message numbers, or labels inside the copy/paste message.\n"
        f"- Keep the copy/paste message under {ARENA_RELAY_WELCOME_MAX_COPY_CHARS} characters and ideally under 18 words.\n"
        "- Return `content` as a single string containing only the Clash Royale copy/paste message. The code will add the arena-relay action card.\n"
    )
    if base_context:
        return f"{base_context}\n\n{instructions}"
    return instructions


def _build_generated_welcome_relay_result(signals: list[dict], generated: dict | None) -> dict | None:
    primary = (signals or [{}])[0] or {}
    name = str(primary.get("name") or "new member").strip() or "new member"
    tag = primary.get("tag") or primary.get("player_tag")
    copies = [_clip_relay_copy(item, limit=ARENA_RELAY_WELCOME_MAX_COPY_CHARS) for item in _result_content_items(generated)]
    copies = [item for item in copies if item]
    if not copies:
        return None
    copy = copies[0]
    copy_lower = copy.lower()
    if "http://" in copy_lower or "https://" in copy_lower:
        return None
    if name != "new member" and name.lower() not in copy_lower:
        return None
    reason = "A profile-specific welcome helps new members feel seen in the in-game chat."
    card = (
        "**R? 👋 welcome relay**\n"
        "🎯 `new_member_welcome`\n"
        "📋 Copy the next message into Clash Royale.\n"
        f"🧠 {reason}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )
    return {
        "event_type": "welcome_relay",
        "summary": f"In-game relay suggestion: {copy}",
        "content": [card, copy],
        "metadata": {
            "action_type": "welcome_relay",
            "objective": "new_member_welcome",
            "rationale": reason,
            "relay_copy": copy,
            "relay_copy_text": copy,
            "relay_copy_count": 1,
            "relay_target": "clash_royale_clan_chat",
            "copy_message_index": 1,
            "target_player_tag": tag,
            "target_player_name": name,
            "authored_by": "elixir_agent",
        },
    }


def _is_low_value_public_departure(signal: dict) -> bool:
    if (signal or {}).get("type") != "member_leave":
        return False
    if (signal or {}).get("departure_kind") == "leader_removal":
        return False
    tenure_days = signal.get("tenure_days")
    if isinstance(tenure_days, int):
        return tenure_days < PUBLIC_DEPARTURE_MIN_TENURE_DAYS
    return False


def _filter_public_announcement_signals(signals: list[dict], *, target_channel_key: str) -> tuple[list[dict], list[dict]]:
    if target_channel_key != "clan-events":
        return (signals or []), []
    filtered = []
    suppressed = []
    for signal in signals or []:
        if _is_low_value_public_departure(signal):
            suppressed.append(signal)
        else:
            filtered.append(signal)
    return filtered, suppressed


def _members_from_group_signal(signal: dict) -> list[dict]:
    members = signal.get("members")
    if isinstance(members, list):
        return [m for m in members if isinstance(m, dict)]
    return []


def _celebration_relay_copy(signals: list[dict]) -> tuple[str, str, str] | None:
    signals = signals or []
    primary = signals[0] if signals else {}
    signal_type = primary.get("type")
    name = str(primary.get("name") or "").strip()

    if signal_type == "career_wins_milestone" and name:
        milestone = primary.get("milestone") or primary.get("new_wins")
        if isinstance(milestone, int):
            return (
                f"Big milestone: {name} just reached {milestone:,} lifetime wins. Drop a congrats when you see them.",
                "player_celebration",
                "Lifetime win milestones are rare enough to recognize in game chat.",
            )

    if signal_type == "cr_account_anniversary" and name:
        years = primary.get("new_years") or primary.get("years")
        if isinstance(years, int):
            return (
                f"CR cake day: {name}'s Clash Royale account is {years} years old. That is real staying power.",
                "player_celebration",
                "Years Played is account-age data in badge form, and round-year anniversaries are special.",
            )

    if signal_type == "new_champion_unlocked" and name:
        card = primary.get("card_name") or primary.get("new_card") or "a Champion"
        return (
            f"Congrats {name} on unlocking {card}. That is a huge progression moment.",
            "player_celebration",
            "Champion unlocks are special progression moments most members understand.",
        )

    if signal_type == "new_card_unlocked" and name:
        card = primary.get("card_name") or primary.get("new_card") or "a new card"
        rarity = str(primary.get("rarity") or "").strip().lower()
        if rarity in {"legendary", "champion"}:
            return (
                f"Congrats {name} on unlocking {card}. Big pull for the collection.",
                "player_celebration",
                "Rare card unlocks are good clan-chat celebration material.",
            )

    if signal_type == "player_level_up" and name:
        new_level = primary.get("new_level")
        if isinstance(new_level, int):
            return (
                f"Level-up shoutout: {name} reached King Level {new_level}. Nice grind.",
                "player_celebration",
                "King-level milestones are visible proof of long-term progress.",
            )

    if signal_type == "best_trophies_peak" and name:
        best = primary.get("new_best_trophies") or primary.get("new_value") or primary.get("best_trophies")
        if isinstance(best, int):
            return (
                f"New personal best: {name} hit {best:,} trophies. Give them a shout.",
                "player_celebration",
                "Personal-best trophy peaks are worth reinforcing in clan chat.",
            )

    if signal_type == "challenge_performance_milestone" and name:
        milestone = primary.get("milestone") or primary.get("challenge_max_wins")
        if isinstance(milestone, int):
            return (
                f"Challenge milestone: {name} reached {milestone} wins in a challenge. That is not easy.",
                "player_celebration",
                "Strong challenge runs are skill milestones the clan can appreciate.",
            )

    if signal_type == "join_anniversary":
        members = _members_from_group_signal(primary)
        if members:
            names = [str(m.get("name") or m.get("member_name") or "").strip() for m in members[:3]]
            names = [item for item in names if item]
            if names:
                joined = ", ".join(names)
                if len(members) > len(names):
                    joined += f" and {len(members) - len(names)} more"
                return (
                    f"Clan cake day: thanks to {joined} for sticking with POAP KINGS. Glad you are here.",
                    "clan_celebration",
                    "Clan tenure milestones are worth celebrating where all members can see them.",
                )

    if signal_type == "member_birthday":
        members = _members_from_group_signal(primary)
        names = [str(m.get("name") or m.get("member_name") or "").strip() for m in members[:3]]
        names = [item for item in names if item]
        if names:
            return (
                f"Birthday shoutout to {', '.join(names)}. Hope it is a good one.",
                "clan_celebration",
                "Member birthdays are warm clan moments leaders can carry into game chat.",
            )

    if signal_type == "clan_birthday":
        years = primary.get("years")
        clan_name = primary.get("clan_name") or "POAP KINGS"
        if isinstance(years, int) and years > 0:
            copy = f"{clan_name} cake day: {years} year{'s' if years != 1 else ''} of the clan. Thanks for building it together."
        else:
            copy = f"{clan_name} cake day. Thanks for building this clan together."
        return (
            copy,
            "clan_celebration",
            "Clan birthdays are shared identity moments that belong in game chat too.",
        )

    return None


def _arena_relay_copy(signals: list[dict]) -> tuple[str | list[str], str, str, str, str, str] | None:
    signals = signals or []
    types = {signal.get("type") for signal in signals}
    primary = signals[0] if signals else {}

    if types & CELEBRATION_RELAY_SIGNAL_TYPES:
        celebration = _celebration_relay_copy(signals)
        if celebration is not None:
            copy, objective, reason = celebration
            return (
                copy,
                objective,
                reason,
                "celebration_relay",
                "celebration relay",
                "celebration_relay",
            )

    if types & {"war_practice_phase_active", "war_practice_day_started", "war_final_practice_day"}:
        return (
            "Practice days are live. Please set boat defenses early so they are ready before battle days start.",
            "boat_defense_setup",
            "Practice timing is the main in-game action before battle days.",
            "in_game_relay",
            "In-game relay",
            "war_relay_brief",
        )

    if types & {"war_final_battle_day", "war_battle_day_final_hours"}:
        return (
            "Final battle day: use any remaining war decks today. Every deck helps lock River Chest rewards and finish the race strong.",
            "war_participation",
            "Final-day reminders are one of the most useful in-game relay moments.",
            "in_game_relay",
            "In-game relay",
            "war_relay_brief",
        )

    if types & {"war_battle_phase_active", "war_battle_day_started"}:
        return (
            "Battle day is live. Please use all 4 war decks when you can; every attack helps keep POAP KINGS moving.",
            "war_participation",
            "Battle-day start messages are useful for members who do not read Discord.",
            "in_game_relay",
            "In-game relay",
            "war_relay_brief",
        )

    if types & {"war_battle_day_live_update"}:
        rank = _ordinal(primary.get("race_rank") or primary.get("our_rank"))
        fame = primary.get("clan_fame") or primary.get("our_fame") or primary.get("fame")
        if rank and isinstance(fame, int):
            copy = f"War check: POAP KINGS is {rank} with {fame:,} fame. Use remaining decks today; every attack keeps pressure on."
        elif rank:
            copy = f"War check: POAP KINGS is {rank}. Use remaining decks today; every attack keeps pressure on."
        else:
            copy = "War check: use remaining decks today if you can. Every attack keeps pressure on and helps the clan chest."
        return (copy, "war_participation", "Current war state is timely enough to relay into game chat.", "in_game_relay", "In-game relay", "war_relay_brief")

    if types & {"war_attacks_complete"}:
        names = _signal_names(signals)
        if names:
            named = ", ".join(names)
            copy = f"Props to {named} for using all 4 war decks today. If you still have decks, jump in and help finish strong."
        else:
            copy = "Props to everyone using all 4 war decks today. If you still have decks, jump in and help finish strong."
        return (copy, "war_recognition", "Recognition can reinforce the exact behavior leaders want repeated.", "in_game_relay", "In-game relay", "war_relay_brief")

    if types & {"war_week_complete", "war_completed"}:
        rank = _ordinal(primary.get("our_rank") or primary.get("race_rank"))
        fame = primary.get("our_fame") or primary.get("clan_fame") or primary.get("fame")
        if rank and isinstance(fame, int):
            copy = f"War week complete: POAP KINGS finished {rank} with {fame:,} fame. Thanks to everyone who used decks."
        elif rank:
            copy = f"War week complete: POAP KINGS finished {rank}. Thanks to everyone who used decks."
        else:
            copy = "War week complete. Thanks to everyone who used decks and helped keep POAP KINGS moving."
        return (copy, "war_recognition", "Week-end recognition is useful in game chat because many contributors are not in Discord.", "in_game_relay", "In-game relay", "war_relay_brief")

    if types & {"war_season_complete"}:
        return (
            "War season complete. Thanks to everyone who kept showing up and using decks for POAP KINGS.",
            "war_recognition",
            "Season-end recognition belongs where the full clan can see it.",
            "in_game_relay",
            "In-game relay",
            "war_relay_brief",
        )

    return None


def _build_arena_relay_result(signals: list[dict]) -> dict | None:
    relay = _arena_relay_copy(signals)
    if relay is None:
        return None
    primary = (signals or [{}])[0] or {}
    copy, objective, reason, action_type, title, event_type = relay
    copies = copy if isinstance(copy, list) else [copy]
    copies = [_clip_relay_copy(item) for item in copies if str(item or "").strip()]
    if not copies:
        return None
    icon = {
        "celebration_relay": "🎉",
        "welcome_relay": "👋",
        "discord_invite_relay": "💬",
    }.get(action_type, "📣")
    copy_count = len(copies)
    copy_instruction = (
        "📋 Copy the next message into Clash Royale."
        if copy_count == 1
        else f"📋 Copy the next {copy_count} messages into Clash Royale in order."
    )
    card = (
        f"**R? {icon} {title}**\n"
        f"🎯 `{objective}`\n"
        f"{copy_instruction}\n"
        f"🧠 {reason}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )
    relay_copy = copies[0] if copy_count == 1 else copies
    relay_copy_text = "\n".join(copies)
    return {
        "event_type": event_type,
        "summary": f"In-game relay suggestion: {relay_copy_text}",
        "content": [card, *copies],
        "metadata": {
            "action_type": action_type,
            "objective": objective,
            "rationale": reason,
            "relay_copy": relay_copy,
            "relay_copy_text": relay_copy_text,
            "relay_copy_count": copy_count,
            "relay_target": "clash_royale_clan_chat",
            "copy_message_index": 1,
            "target_player_tag": primary.get("tag") or primary.get("player_tag"),
            "target_player_name": primary.get("name"),
        },
    }


def _attach_leader_action_to_result(result: dict, action: dict) -> dict:
    if not action:
        return result
    action_id = action.get("action_id")
    if action_id:
        result["summary"] = f"Leader action R{action_id}: {result.get('summary') or action.get('prompt_text')}"
        content = result.get("content")
        if isinstance(content, list) and content:
            content[0] = str(content[0] or "").replace("**R? ", f"**R{action_id} ", 1)
        else:
            result["content"] = str(content or "").replace("**R? ", f"**R{action_id} ", 1)
    metadata = result.setdefault("metadata", {})
    metadata.update({
        "leader_action_id": action.get("action_id"),
        "leader_action_key": action.get("action_key"),
        "leader_action_status": action.get("status"),
    })
    return result


def _leader_action_copy_messages_from_result(result: dict, metadata: dict | None = None) -> list[str]:
    metadata = metadata or {}
    relay_copy = metadata.get("relay_copy")
    if isinstance(relay_copy, list):
        return [str(item).strip() for item in relay_copy if str(item).strip()]
    if isinstance(relay_copy, str) and relay_copy.strip():
        return [relay_copy.strip()]
    content = result.get("content")
    if isinstance(content, list) and len(content) > 1:
        return [str(item).strip() for item in content[1:] if str(item).strip()]
    copy_text = metadata.get("relay_copy_text")
    if isinstance(copy_text, str) and copy_text.strip():
        return [line.strip() for line in copy_text.splitlines() if line.strip()]
    return []


def _leader_action_member_name(member: dict) -> str:
    return (
        member.get("member_ref")
        or member.get("name")
        or member.get("player_name")
        or member.get("tag")
        or member.get("player_tag")
        or "member"
    )


def _format_leader_action_card(action: dict, *, title: str, prompt_text: str, rationale: str) -> str:
    action_id = action.get("action_id")
    objective = action.get("objective") or "leader_action"
    action_type = action.get("action_type") or ""
    icon = {
        "in_game_relay": "📣",
        "war_nudge_recommendation": "👋",
        "promotion_recommendation": "⬆️",
        "demotion_recommendation": "⬇️",
        "kick_recommendation": "🚪",
        "celebration_relay": "🎉",
    }.get(action_type, "⚡")
    return (
        f"**R{action_id} {icon} {title}**\n"
        f"🎯 `{objective}`\n"
        "🛠️ Action\n"
        f"```text\n{prompt_text}\n```\n"
        f"🧠 {rationale}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )


def _war_nudge_candidates(limit: int = ARENA_RELAY_MAX_NUDGE_ACTIONS) -> list[dict]:
    war_day = db.get_current_war_day_state() or {}
    if war_day.get("phase") != "battle":
        return []
    candidates = []
    for member in war_day.get("used_none") or []:
        tag = member.get("tag") or member.get("player_tag")
        if not tag:
            continue
        candidates.append({
            "tag": tag,
            "name": _leader_action_member_name(member),
            "war_day_key": war_day.get("war_day_key"),
            "phase_display": war_day.get("phase_display"),
            "time_left_text": war_day.get("time_left_text"),
            "race_completed": bool(war_day.get("race_completed")),
            "member": member,
        })
        if len(candidates) >= limit:
            break
    return candidates


def _parse_recorded_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _arena_relay_recently_posted(recent_posts: list[dict], *, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = current - timedelta(hours=ARENA_RELAY_COOLDOWN_HOURS)
    for post in recent_posts or []:
        recorded = _parse_recorded_at(post.get("recorded_at") or post.get("created_at"))
        if recorded and recorded >= cutoff:
            return True
    return False


def _arena_relay_uses_cooldown(intent: str | None) -> bool:
    """Cooldown is for generic broadcast relays; action cards use policy caps."""
    return intent not in {
        "welcome_relay",
    }


def _arena_relay_uses_leader_action_policy(intent: str | None) -> bool:
    """Welcomes are opportunistic and should not consume action-board budget."""
    return intent not in {
        "welcome_relay",
    }


def _facade():
    from runtime.jobs import _signals as facade

    return facade


def _runtime_app():
    from runtime import app as runtime_app

    return runtime_app


def _bot():
    return _runtime_app().bot


async def _deliver_signal_outcome(outcome, signals, clan, war):
    facade = _facade()
    existing = await asyncio.to_thread(
        db.get_signal_outcome,
        outcome["source_signal_key"],
        outcome["target_channel_key"],
        outcome["intent"],
    )
    if existing and existing.get("delivery_status") == "delivered":
        return True

    await asyncio.to_thread(
        db.upsert_signal_outcome,
        outcome["source_signal_key"],
        outcome["source_signal_type"],
        outcome["target_channel_key"],
        outcome["target_channel_id"],
        outcome["intent"],
        required=outcome.get("required", True),
        delivery_status="planned",
        payload=outcome.get("payload"),
    )

    channel_config = facade._channel_config_by_key(outcome["target_channel_key"])
    channel = _bot().get_channel(channel_config["id"])
    if not channel:
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="failed",
            payload=outcome.get("payload"),
            error_detail="channel not found",
        )
        return False

    channel_id = channel_config["id"]
    delivery_signals, suppressed_public_signals = _filter_public_announcement_signals(
        signals,
        target_channel_key=outcome["target_channel_key"],
    )
    if suppressed_public_signals and not delivery_signals:
        reason = f"low_value_departure_under_{PUBLIC_DEPARTURE_MIN_TENURE_DAYS}d"
        log.info(
            "public announcement skipped: channel=%s reason=%s suppressed=%s",
            outcome["target_channel_key"],
            reason,
            [
                {
                    "type": signal.get("type"),
                    "tag": signal.get("tag"),
                    "tenure_days": signal.get("tenure_days"),
                }
                for signal in suppressed_public_signals
            ],
        )
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="skipped",
            payload={
                "signals": signals,
                "suppressed_signals": suppressed_public_signals,
            },
            error_detail=reason,
            mark_attempt=True,
        )
        return True
    if suppressed_public_signals:
        log.info(
            "public announcement filtered %s low-value departure signal(s) before generation",
            len(suppressed_public_signals),
        )

    recent_posts = await asyncio.to_thread(db.list_channel_messages, channel_id, 10, "assistant")
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=delivery_signals,
    )

    from runtime.channel_subagents import TOURNAMENT_SIGNAL_TYPES, WAR_RECAP_SIGNAL_TYPES

    is_tournament_batch = bool(delivery_signals) and all(
        (s or {}).get("type") in TOURNAMENT_SIGNAL_TYPES for s in delivery_signals
    )
    is_war_recap_batch = bool(delivery_signals) and all(
        (s or {}).get("type") in WAR_RECAP_SIGNAL_TYPES for s in delivery_signals
    )
    is_season_awards_batch = bool(delivery_signals) and all(
        (s or {}).get("type") in SEASON_AWARDS_SIGNAL_TYPES for s in delivery_signals
    )
    if is_tournament_batch or is_war_recap_batch or is_season_awards_batch:
        context = None
    else:
        context = facade._build_outcome_context(outcome, delivery_signals, clan, war)

    preauthored_result = None
    if len(delivery_signals) == 1 and delivery_signals[0].get("signal_key"):
        preauthored_result = facade._preauthored_system_signal_result(delivery_signals[0])

    arena_leader_action = None
    try:
        channel_name = getattr(channel, "name", None)
        if not isinstance(channel_name, str):
            channel_name = None
        channel_kind = getattr(channel, "type", None)
        if channel_kind is not None:
            channel_kind = str(channel_kind)

        if channel_config["subagent_key"] == "arena-relay":
            arena_types = {signal.get("type") for signal in delivery_signals or []}
            member_join_signals = [
                signal for signal in delivery_signals or []
                if signal.get("type") == "member_join"
            ]
            if _arena_relay_uses_cooldown(outcome.get("intent")) and _arena_relay_recently_posted(recent_posts):
                await asyncio.to_thread(
                    db.upsert_signal_outcome,
                    outcome["source_signal_key"],
                    outcome["source_signal_type"],
                    outcome["target_channel_key"],
                    outcome["target_channel_id"],
                    outcome["intent"],
                    required=outcome.get("required", True),
                    delivery_status="skipped",
                    payload={"signals": signals},
                    error_detail=f"arena_relay_cooldown:{ARENA_RELAY_COOLDOWN_HOURS}h",
                    mark_attempt=True,
                )
                return True
            if _arena_relay_uses_leader_action_policy(outcome.get("intent")):
                critical = bool({signal.get("type") for signal in delivery_signals or []} & CRITICAL_LEADER_ACTION_SIGNAL_TYPES)
                allowed, reason = await asyncio.to_thread(can_post_leader_action, critical=critical)
                if not allowed:
                    await asyncio.to_thread(
                        db.upsert_signal_outcome,
                        outcome["source_signal_key"],
                        outcome["source_signal_type"],
                        outcome["target_channel_key"],
                        outcome["target_channel_id"],
                        outcome["intent"],
                        required=outcome.get("required", True),
                        delivery_status="skipped",
                        payload={"signals": signals},
                        error_detail=f"leader_action_policy:{reason}",
                        mark_attempt=True,
                    )
                    return True
            if arena_types == {"discord_invite_reminder"}:
                action_memory_context = _memory_context_with_leader_action_feedback(
                    memory_context,
                    await asyncio.to_thread(
                        db.list_leader_action_feedback_profiles,
                        action_type="discord_invite_relay",
                        limit=1,
                    ),
                )
                generated = await asyncio.to_thread(
                    elixir_agent.generate_channel_update,
                    channel_config["name"],
                    channel_config["subagent_key"],
                    _discord_invite_relay_context(context),
                    recent_posts=recent_posts,
                    memory_context=action_memory_context,
                    leadership=(channel_config["memory_scope"] == "leadership"),
                )
                result = _build_generated_discord_invite_relay_result(delivery_signals, generated)
            elif outcome.get("intent") == "welcome_relay" and member_join_signals:
                action_memory_context = _memory_context_with_leader_action_feedback(
                    memory_context,
                    await asyncio.to_thread(
                        db.list_leader_action_feedback_profiles,
                        action_type="welcome_relay",
                        limit=1,
                    ),
                )
                generated = await asyncio.to_thread(
                    elixir_agent.generate_channel_update,
                    channel_config["name"],
                    channel_config["subagent_key"],
                    _member_join_welcome_context(context, member_join_signals[0]),
                    recent_posts=recent_posts,
                    memory_context=action_memory_context,
                    leadership=(channel_config["memory_scope"] == "leadership"),
                )
                result = _build_generated_welcome_relay_result(member_join_signals, generated)
            else:
                result = _build_arena_relay_result(delivery_signals)
            if result is not None:
                metadata = result.get("metadata") if isinstance(result, dict) else {}
                action_type = metadata.get("action_type") or "in_game_relay"
                baseline = await asyncio.to_thread(
                    db.build_leader_action_baseline,
                    action_type=action_type,
                    target_player_tag=metadata.get("target_player_tag"),
                    signals=delivery_signals,
                )
                action = await asyncio.to_thread(
                    db.create_leader_action_recommendation,
                    action_type=action_type,
                    objective=metadata.get("objective") or "war_participation",
                    prompt_text=metadata.get("relay_copy_text") or result.get("summary") or "",
                    rationale=metadata.get("rationale"),
                    target_channel_key=outcome["target_channel_key"],
                    target_channel_id=outcome["target_channel_id"],
                    target_player_tag=metadata.get("target_player_tag"),
                    target_player_name=metadata.get("target_player_name"),
                    source_signal_key=outcome["source_signal_key"],
                    source_signal_type=outcome["source_signal_type"],
                    copy_original_text=metadata.get("relay_copy_text"),
                    copy_current_text=metadata.get("relay_copy_text"),
                    ui_version=LEADER_ACTION_UI_VERSION,
                    baseline=baseline,
                )
                arena_leader_action = action
                result = _attach_leader_action_to_result(result, action)
        elif preauthored_result is not None:
            result = preauthored_result
        elif is_tournament_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_tournament_update,
                delivery_signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        elif is_war_recap_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_war_recap_update,
                delivery_signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        elif is_season_awards_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_season_awards_post,
                delivery_signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        else:
            result = await asyncio.to_thread(
                elixir_agent.generate_channel_update,
                channel_config["name"],
                channel_config["subagent_key"],
                context,
                recent_posts=recent_posts,
                memory_context=memory_context,
                leadership=(channel_config["memory_scope"] == "leadership"),
            )

        app = _runtime_app()
        if result is None:
            await app._maybe_alert_llm_failure("channel update")
            status = "failed" if outcome.get("required", True) else "skipped"
            await asyncio.to_thread(
                db.upsert_signal_outcome,
                outcome["source_signal_key"],
                outcome["source_signal_type"],
                outcome["target_channel_key"],
                outcome["target_channel_id"],
                outcome["intent"],
                required=outcome.get("required", True),
                delivery_status=status,
                payload=outcome.get("payload"),
                error_detail="generator returned null",
                mark_attempt=True,
            )
            return status == "skipped"

        app._clear_llm_failure_alert_if_recovered()
        metadata = result.get("metadata") if isinstance(result, dict) else None
        if isinstance(metadata, dict) and metadata.get("decision") == "no_post":
            reason = metadata.get("reason") or "unspecified"
            log.info(
                "channel_update no_post: channel=%s signal_type=%s signal_key=%s reason=%s",
                outcome["target_channel_key"],
                outcome["source_signal_type"],
                outcome["source_signal_key"],
                reason,
            )
            await asyncio.to_thread(
                db.upsert_signal_outcome,
                outcome["source_signal_key"],
                outcome["source_signal_type"],
                outcome["target_channel_key"],
                outcome["target_channel_id"],
                outcome["intent"],
                required=outcome.get("required", True),
                delivery_status="skipped",
                payload={"result": result, "signals": signals},
                error_detail=f"llm_no_post: {reason}",
                mark_attempt=True,
            )
            return True

        posts = app._entry_posts(result)
        metadata = result.get("metadata") if isinstance(result, dict) else {}
        sent_messages = []
        if channel_config["subagent_key"] == "arena-relay" and isinstance(metadata, dict) and metadata.get("leader_action_id"):
            action = arena_leader_action or await asyncio.to_thread(db.get_leader_action_by_id, metadata.get("leader_action_id"))
            if action:
                sent_messages = await post_leader_action_card(
                    channel,
                    action,
                    copy_messages=_leader_action_copy_messages_from_result(result, metadata),
                )
            else:
                sent_messages = await facade._post_to_elixir(channel, result)
        else:
            sent_messages = await facade._post_to_elixir(channel, result)
        if not isinstance(sent_messages, list):
            sent_messages = []
        if channel_config["subagent_key"] == "arena-relay":
            action_id = metadata.get("leader_action_id") if isinstance(metadata, dict) else None
            first_message = sent_messages[0] if sent_messages else None
            first_message_id = getattr(first_message, "id", None)
            if action_id and first_message_id is not None:
                await asyncio.to_thread(
                    db.update_leader_action_message,
                    action_id,
                    source_message_id=first_message_id,
                )
            copy_index = metadata.get("copy_message_index") if isinstance(metadata, dict) else None
            if action_id and isinstance(copy_index, int) and copy_index < len(sent_messages):
                copy_message_id = getattr(sent_messages[copy_index], "id", None)
                if copy_message_id is not None:
                    await asyncio.to_thread(
                        db.update_leader_action_copy_message,
                        action_id,
                        copy_message_id=copy_message_id,
                    )
        if (
            channel_config["subagent_key"] == "clan-events"
            and any(s.get("type") == "member_join" for s in delivery_signals)
        ):
            from modules.poap_kings import site as _pk_site

            if _pk_site.site_enabled():
                from runtime.jobs._site import _notify_poapkings_publish, _publish_member_join_blog_post

                join_body = "\n\n".join(posts)
                try:
                    blog_result = await asyncio.to_thread(
                        _publish_member_join_blog_post,
                        delivery_signals,
                        join_body,
                        result.get("summary"),
                    )
                    await _notify_poapkings_publish("member-join-blog", publish_result=blog_result)
                except Exception as exc:
                    log.error("Member join blog post publish failed: %s", exc, exc_info=True)
                    await _notify_poapkings_publish("member-join-blog", error_detail=str(exc))

        summary = result.get("summary")
        event_type = result.get("event_type") or outcome["intent"]
        for index, post in enumerate(posts):
            sent_message = sent_messages[index] if index < len(sent_messages) else None
            sent_message_id = getattr(sent_message, "id", None)
            post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
            post_event_type = event_type if index == 0 else f"{event_type}_part"
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel),
                "assistant",
                post,
                summary=post_summary,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_kind=channel_kind,
                workflow=channel_config["subagent_key"],
                event_type=post_event_type,
                discord_message_id=sent_message_id,
                raw_json={
                    "source_signal_key": outcome["source_signal_key"],
                    "intent": outcome["intent"],
                    "target_channel_key": outcome["target_channel_key"],
                    "result": result,
                    "suppressed_signals": suppressed_public_signals,
                },
            )

        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="delivered",
            payload={"result": result, "signals": signals, "suppressed_signals": suppressed_public_signals},
            mark_attempt=True,
            delivered=True,
        )
        body = "\n\n".join(posts)
        if channel_config["subagent_key"] != "arena-relay":
            await asyncio.to_thread(
                facade.maybe_upsert_signal_memory,
                source_signal_key=outcome["source_signal_key"],
                signal_type=(delivery_signals[0].get("type") or outcome["source_signal_type"]),
                body=body,
                outcome=outcome,
                signals=delivery_signals,
            )

        from agent.memory_tasks import store_observation_facts

        await asyncio.to_thread(store_observation_facts, delivery_signals, channel_id)
        if channel_config["subagent_key"] == "river-race" and facade._signal_group_needs_recap_memory(delivery_signals):
            await asyncio.to_thread(facade._store_recap_memories_for_signal_batch, delivery_signals, posts, channel_id)

        from runtime.helpers._common import _safe_create_task

        if channel_config["subagent_key"] != "arena-relay":
            _safe_create_task(
                facade._post_signal_memory(body, outcome, delivery_signals),
                name="signal_memory",
            )
        return True
    except Exception as exc:
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="failed",
            payload=outcome.get("payload"),
            error_detail=str(exc),
            mark_attempt=True,
        )
        log.error(
            "Signal outcome delivery failed for %s/%s: %s",
            outcome["source_signal_key"],
            outcome["target_channel_key"],
            exc,
            exc_info=True,
        )
        return False


async def _deliver_signal_group(signals, clan, war):
    facade = _facade()
    outcomes = facade.plan_signal_outcomes(signals)
    if not outcomes:
        return False
    results = []
    for outcome in outcomes:
        delivered = await facade._deliver_signal_outcome(outcome, signals, clan, war)
        results.append(delivered)
    rows = await asyncio.to_thread(db.list_signal_outcomes, outcomes[0]["source_signal_key"])
    if rows and all(row.get("delivery_status") in {"delivered", "skipped"} for row in rows):
        await facade._mark_signal_group_completed(signals)
        return True
    return all(results)


async def _deliver_arena_relay_sidecars(signals, clan, war) -> int:
    facade = _facade()
    delivered = 0
    for outcome in facade.plan_signal_outcomes(signals or []):
        if outcome.get("target_channel_key") != "arena-relay":
            continue
        ok = await facade._deliver_signal_outcome(outcome, signals, clan, war)
        if ok:
            delivered += 1
    delivered += await _deliver_war_nudge_sidecars(signals)
    return delivered


async def _deliver_war_nudge_sidecars(signals) -> int:
    types = {signal.get("type") for signal in signals or []}
    if not (types & WAR_NUDGE_SIGNAL_TYPES):
        return 0
    critical = bool(types & CRITICAL_LEADER_ACTION_SIGNAL_TYPES)
    try:
        channel_config = _facade()._channel_config_by_key("arena-relay")
    except Exception:
        log.info("war nudge sidecar skipped: arena-relay unavailable", exc_info=True)
        return 0
    app = _runtime_app()
    channel = app.bot.get_channel(channel_config["id"])
    if channel is None:
        log.warning("war nudge sidecar skipped: arena-relay channel not found")
        return 0

    candidates = await asyncio.to_thread(_war_nudge_candidates)
    posted = 0
    channel_name = getattr(channel, "name", "arena-relay")
    channel_kind = getattr(channel, "type", "text")
    if channel_kind is not None:
        channel_kind = str(channel_kind)

    for candidate in candidates:
        allowed, reason = await asyncio.to_thread(can_post_leader_action, critical=critical)
        if not allowed:
            log.info("war nudge sidecar skipped by policy: %s", reason)
            return posted
        name = candidate["name"]
        tag = candidate["tag"]
        prompt_text = f"Nudge {name} to use war decks today."
        if candidate.get("race_completed"):
            rationale = (
                f"{name} has not used war decks on {candidate.get('phase_display') or 'battle day'}; "
                "the race is finished, so this is for personal River Chest rewards."
            )
        else:
            rationale = (
                f"{name} has not used war decks on {candidate.get('phase_display') or 'battle day'}"
                + (f" with {candidate['time_left_text']} left" if candidate.get("time_left_text") else "")
                + "."
            )
        baseline = await asyncio.to_thread(
            db.build_leader_action_baseline,
            action_type="war_nudge_recommendation",
            target_player_tag=tag,
        )
        action = await asyncio.to_thread(
            db.create_leader_action_recommendation,
            action_type="war_nudge_recommendation",
            objective="war_participation",
            prompt_text=prompt_text,
            rationale=rationale,
            target_channel_key="arena-relay",
            target_channel_id=channel_config["id"],
            target_player_tag=tag,
            target_player_name=name,
            source_signal_key=f"war_nudge:{candidate.get('war_day_key') or 'unknown'}:{tag}",
            source_signal_type="war_nudge_recommendation",
            ui_version=LEADER_ACTION_UI_VERSION,
            baseline=baseline,
        )
        if not action or action.get("source_message_id"):
            continue
        content = _format_leader_action_card(
            action,
            title="war nudge recommendation",
            prompt_text=prompt_text,
            rationale=rationale,
        )
        sent_messages = await post_leader_action_card(channel, action, copy_messages=[])
        if not isinstance(sent_messages, list):
            sent_messages = []
        first_message = sent_messages[0] if sent_messages else None
        first_message_id = getattr(first_message, "id", None)
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            content,
            summary=f"Leader action R{action.get('action_id')}: war nudge recommendation",
            channel_id=channel_config["id"],
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow="arena-relay",
            event_type="war_nudge_recommendation",
            discord_message_id=first_message_id,
            raw_json={"leader_action": action},
        )
        posted += 1
    return posted


async def _deliver_awareness_post(post: dict, signals: list[dict]) -> bool:
    facade = _facade()
    from runtime.situation import CHANNEL_LANES

    channel_key = (post.get("channel") or "").strip()
    if channel_key not in CHANNEL_LANES:
        log.warning("awareness post rejected: unknown channel %r", channel_key)
        return False
    leads_with = (post.get("leads_with") or "").strip()
    if leads_with and leads_with not in CHANNEL_LANES[channel_key]:
        log.warning(
            "awareness post rejected: leads_with=%r not allowed on channel=%r (allowed=%s)",
            leads_with,
            channel_key,
            sorted(CHANNEL_LANES[channel_key]),
        )
        return False

    covers = list(post.get("covers_signal_keys") or [])
    if signals and not covers:
        log.warning(
            "awareness post rejected: empty covers_signal_keys channel=%r despite %d input signal(s)",
            channel_key,
            len(signals),
        )
        return False

    if covers:
        covers_set = set(covers)
        for sig in signals or []:
            if signal_source_key(sig) not in covers_set:
                continue
            if is_leadership_only_signal(sig) and channel_key != "leader-lounge":
                log.warning(
                    "awareness post rejected: leadership-only signal %s routed to public channel %s",
                    signal_source_key(sig),
                    channel_key,
                )
                return False

    try:
        channel_config = facade._channel_config_by_key(channel_key)
    except RuntimeError:
        log.warning("awareness post rejected: channel %r not configured", channel_key)
        return False
    channel = _bot().get_channel(channel_config["id"])
    if not channel:
        log.warning("awareness post rejected: channel %r not found in Discord", channel_key)
        return False

    content = post.get("content")
    if not content:
        log.warning("awareness post on %r had empty content", channel_key)
        return False

    result = {
        "event_type": post.get("event_type") or "awareness_update",
        "summary": post.get("summary"),
        "content": content,
    }
    try:
        await facade._post_to_elixir(channel, result)
    except Exception:
        log.error("awareness post send failed channel=%r", channel_key, exc_info=True)
        return False

    app = _runtime_app()
    posts = app._entry_posts(result)
    channel_id = channel_config["id"]
    channel_name = getattr(channel, "name", None)
    if not isinstance(channel_name, str):
        channel_name = None
    channel_kind = getattr(channel, "type", None)
    if channel_kind is not None:
        channel_kind = str(channel_kind)
    summary = result.get("summary")
    event_type = result.get("event_type")
    for index, body_part in enumerate(posts):
        post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
        post_event_type = event_type if index == 0 else f"{event_type}_part"
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            body_part,
            summary=post_summary,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow=channel_config["subagent_key"],
            event_type=post_event_type,
            raw_json={
                "source": "awareness_loop",
                "leads_with": post.get("leads_with"),
                "covers_signal_keys": post.get("covers_signal_keys") or [],
                "result": result,
            },
        )

    body = "\n\n".join(posts)
    for signal in signals or []:
        sig_key = signal_source_key(signal)
        if not sig_key or sig_key not in covers:
            continue
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            sig_key,
            signal.get("type") or "awareness_signal",
            channel_key,
            channel_id,
            event_type,
            required=True,
            delivery_status="delivered",
            payload={"result": result, "signals": [signal]},
            mark_attempt=True,
            delivered=True,
        )

    from runtime.helpers._common import _safe_create_task

    fake_outcome = {
        "intent": event_type,
        "target_channel_key": channel_key,
        "target_channel_id": channel_id,
        "source_signal_key": (covers[0] if covers else "awareness_loop"),
    }
    _safe_create_task(
        facade._post_signal_memory(body, fake_outcome, signals or []),
        name="awareness_signal_memory",
    )
    return True


async def _deliver_awareness_post_plan(plan: dict, signals: list[dict]) -> dict:
    facade = _facade()
    posts = (plan or {}).get("posts") or []
    delivered = 0
    rejected = 0
    covered: set[str] = set()
    attempted: set[str] = set()
    for post in posts:
        post_keys = {str(key) for key in (post.get("covers_signal_keys") or []) if key}
        attempted |= post_keys
        ok = await facade._deliver_awareness_post(post, signals or [])
        if ok:
            delivered += 1
            covered |= post_keys
        else:
            rejected += 1

    if covered:
        covered_signals = [
            s for s in (signals or [])
            if signal_source_key(s) in covered
        ]
        if covered_signals:
            await facade._mark_signal_group_completed(covered_signals)

    return {
        "delivered": delivered,
        "rejected": rejected,
        "covered_signal_keys": covered,
        "attempted_signal_keys": attempted,
    }


async def _deliver_signal_group_via_awareness(signals, clan, war, *, workflow: str | None = None) -> bool:
    facade = _facade()
    from heartbeat import HeartbeatTickResult
    from runtime.situation import build_situation, situation_is_quiet

    bundle = HeartbeatTickResult(signals=signals or [], clan=clan or {}, war=war or {})
    situation = build_situation(bundle)

    if situation_is_quiet(situation):
        log.info("awareness loop: quiet tick, skipping agent call")
        # A quiet tick means nothing here is worth posting; mark the inputs
        # handled so they don't re-emit every cycle. (Signals only get marked
        # completed after a confirmed post/skip — not speculatively up front —
        # so a planned post that fails delivery is retried, not burned.)
        if signals:
            await facade._mark_signal_group_completed(signals)
        return True

    tool_stats: dict = {}
    try:
        plan = await asyncio.to_thread(elixir_agent.run_awareness_tick, situation, tool_stats=tool_stats)
    except Exception as exc:
        log.error("awareness loop run_awareness_tick failed: %s", exc, exc_info=True)
        plan = None

    if plan is None:
        log.warning("awareness loop returned no plan; falling back to per-signal delivery")
        return await facade._deliver_signal_group(signals, clan, war)

    report = await facade._deliver_awareness_post_plan(plan, signals)
    relay_sidecars = await facade._deliver_arena_relay_sidecars(signals, clan, war)

    hard_required_keys = {hp.get("signal_key") for hp in (situation.get("hard_post_signals") or [])}
    covered_keys = report["covered_signal_keys"]
    uncovered = [
        signal for signal in (signals or [])
        if signal_source_key(signal) in hard_required_keys
        and signal_source_key(signal) not in covered_keys
    ]
    fallback_failed_keys: set[str] = set()
    all_ok = True
    if uncovered:
        log.warning(
            "awareness loop: %d hard-post-floor signal(s) uncovered; falling back per-signal",
            len(uncovered),
        )
        for signal in uncovered:
            ok = await facade._deliver_signal_group([signal], clan, war)
            if not ok:
                fallback_failed_keys.add(signal_source_key(signal))
            all_ok = all_ok and ok

    attempted_keys = report.get("attempted_signal_keys") or set()
    # Soft signals the agent planned to post but whose delivery failed. Do NOT
    # mark these completed — leaving them unmarked lets the next tick retry,
    # instead of permanently burning a post that never reached Discord.
    post_failed_keys = {
        signal_source_key(signal)
        for signal in (signals or [])
        if signal_source_key(signal) in attempted_keys
        and signal_source_key(signal) not in covered_keys
        and signal_source_key(signal) not in hard_required_keys
    }

    considered_skipped = [
        signal for signal in (signals or [])
        if signal_source_key(signal) not in covered_keys
        and signal_source_key(signal) not in fallback_failed_keys
        and signal_source_key(signal) not in hard_required_keys
        and signal_source_key(signal) not in post_failed_keys
    ]
    if considered_skipped:
        await facade._mark_signal_group_completed(considered_skipped)

    revisit_keys_seen = set(covered_keys)
    revisit_keys_seen.update(signal_source_key(s) for s in (considered_skipped or []))
    revisit_keys_seen.update(signal_source_key(s) for s in (uncovered or []))
    if revisit_keys_seen:
        try:
            await asyncio.to_thread(db.mark_revisited, sorted(revisit_keys_seen))
        except Exception:
            log.warning("mark_revisited failed", exc_info=True)

    log.info(
        "awareness_tick_result workflow=%r delivered=%d rejected=%d covered=%d considered_skipped=%d "
        "hard_fallback=%d hard_fallback_failed=%d relay_sidecars=%d signals_in=%d skipped_reason=%r",
        workflow,
        report["delivered"],
        report["rejected"],
        len(covered_keys),
        len(considered_skipped),
        len(uncovered),
        len(fallback_failed_keys),
        relay_sidecars,
        len(signals or []),
        (plan or {}).get("skipped_reason"),
    )

    uncovered_keys = {signal_source_key(s) for s in uncovered}
    signal_outcomes: list[dict] = []
    for signal in (signals or []):
        key = signal_source_key(signal)
        if key in covered_keys:
            status = "covered"
        elif key in fallback_failed_keys:
            status = "fallback_failed"
        elif key in uncovered_keys:
            status = "fallback"
        elif key in post_failed_keys:
            status = "post_failed"
        else:
            status = "skipped"
        signal_outcomes.append({
            "signal_key": key,
            "signal_type": signal.get("type") or "",
            "status": status,
        })
    try:
        await asyncio.to_thread(
            db.record_awareness_tick,
            workflow=workflow,
            signals_in=len(signals or []),
            posts_delivered=report["delivered"],
            posts_rejected=report["rejected"],
            covered_keys=len(covered_keys),
            considered_skipped=len(considered_skipped),
            hard_fallback=len(uncovered),
            hard_fallback_failed=len(fallback_failed_keys),
            all_ok=all_ok,
            skipped_reason=(plan or {}).get("skipped_reason"),
            signal_outcomes=signal_outcomes,
            write_calls_issued=int(tool_stats.get("write_calls_issued", 0)),
            write_calls_succeeded=int(tool_stats.get("write_calls_succeeded", 0)),
            write_calls_denied=int(tool_stats.get("write_calls_denied", 0)),
        )
    except Exception:
        log.warning("record_awareness_tick failed", exc_info=True)

    return bool(all_ok)
