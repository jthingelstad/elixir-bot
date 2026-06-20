"""Signal delivery entrypoints."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import db
import elixir_agent
from runtime.clan_chat_copy import (
    CLAN_CHAT_DEFAULT_MAX_CHARS,
    CLAN_CHAT_WELCOME_MAX_CHARS,
    DISCORD_INVITE_ROUTE,
    ClanChatCopyResult,
    clip_clan_chat_text,
    generate_clan_chat_copy,
    messages_from_agent_result,
)
from runtime.leader_action_observability import post_leader_action_skip
from runtime.leader_action_policy import can_post_leader_action
from runtime.leader_action_ui import LEADER_ACTION_UI_VERSION, post_leader_action_card
from runtime.signal_lanes import (
    SEASON_AWARDS_SIGNAL_TYPES,
    batch_source_key,
    build_lane_memory_context,
    is_arena_relay_celebration_signal,
    is_battle_mode_signal,
    is_leadership_only_signal,
    is_progression_signal,
    is_war_relay_signal,
    is_war_signal,
    signal_source_key,
)
from runtime.helpers import _channel_scope

log = logging.getLogger("elixir")

ARENA_RELAY_COOLDOWN_HOURS = 18
ARENA_RELAY_MAX_COPY_CHARS = CLAN_CHAT_DEFAULT_MAX_CHARS
ARENA_RELAY_WELCOME_MAX_COPY_CHARS = CLAN_CHAT_WELCOME_MAX_CHARS
PUBLIC_DEPARTURE_MIN_TENURE_DAYS = 14
CRITICAL_LEADER_ACTION_SIGNAL_TYPES = {
    "war_battle_day_final_hours",
    "war_final_battle_day",
}


def _lane_key_for_config(channel_config: dict) -> str:
    return channel_config.get("lane_key") or channel_config.get("lane") or ""


def _normalize_signal_cover_key(value) -> str:
    text = str(value or "").strip()
    return text.strip("|") or text


def _signal_cover_key_map(signals: list[dict] | tuple[dict, ...] | None) -> dict[str, str]:
    key_map: dict[str, str] = {}
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        canonical = signal_source_key(signal)
        if not canonical:
            continue
        for value in (
            canonical,
            signal.get("signal_key"),
            signal.get("signal_log_type"),
            signal.get("source_signal_key"),
        ):
            normalized = _normalize_signal_cover_key(value)
            if normalized:
                key_map[normalized] = canonical
    return key_map


def _normalized_cover_keys(raw_keys, signals: list[dict] | tuple[dict, ...] | None) -> set[str]:
    keys = [_normalize_signal_cover_key(key) for key in (raw_keys or [])]
    keys = [key for key in keys if key]
    if not signals:
        return set(keys)
    key_map = _signal_cover_key_map(signals)
    return {key_map[key] for key in keys if key in key_map}


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


async def _record_signal_events(
    signals: list[dict] | tuple[dict, ...] | None,
    *,
    source_system: str,
    source_detector: str | None = None,
) -> int:
    """Record signal observations into the canonical event stream.

    This must never block delivery. The event stream is an observation ledger,
    not the source of truth for whether the current post should go out.
    """
    recordable = [
        signal for signal in (signals or [])
        if isinstance(signal, dict) and not signal.get("event_key")
    ]
    if not recordable:
        return 0
    try:
        events = await asyncio.to_thread(
            db.record_signal_events,
            recordable,
            source_system=source_system,
            source_detector=source_detector,
        )
        return len(events or [])
    except Exception:
        log.warning(
            "event stream insert failed source_system=%s source_detector=%s signals=%d",
            source_system,
            source_detector,
            len(recordable),
            exc_info=True,
        )
        return 0


async def _upsert_decision_cases_from_signals(
    signals: list[dict] | tuple[dict, ...] | None,
    *,
    source_system: str | None = None,
) -> int:
    """Create durable cases from actionable signals without blocking delivery."""
    try:
        cases = await asyncio.to_thread(
            db.upsert_decision_cases_from_signals,
            signals or [],
            source_system=source_system,
        )
        return len(cases or [])
    except Exception:
        log.warning(
            "decision case upsert failed source_system=%s signals=%d",
            source_system,
            len(signals or []),
            exc_info=True,
        )
        return 0


async def _create_awareness_coverage_gap_intent(
    signals: list[dict] | tuple[dict, ...] | None,
    *,
    workflow: str | None,
    situation: dict | None,
    reason: str,
) -> dict | None:
    try:
        return await asyncio.to_thread(
            db.create_awareness_coverage_gap_intent,
            signals or [],
            workflow=workflow or "awareness",
            reason=reason,
            situation=situation,
        )
    except Exception:
        log.warning("communication coverage-gap intent create failed for workflow=%r", workflow, exc_info=True)
        return None


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
    return clip_clan_chat_text(text, limit=limit)


def _first_text(mapping: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text:
            return text
    return None


def _leader_action_skip_target(signals: list[dict] | tuple[dict, ...] | None) -> tuple[str | None, str | None]:
    """Best-effort target extraction for #elixir-log skip decisions."""
    name_keys = ("target_player_name", "player_name", "member_name", "current_name", "name")
    tag_keys = ("target_player_tag", "player_tag", "member_tag", "tag")
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        name = _first_text(signal, name_keys)
        tag = _first_text(signal, tag_keys)
        if name or tag:
            return name, tag
        for nested_key in ("target", "player", "member"):
            nested = signal.get(nested_key)
            if not isinstance(nested, dict):
                continue
            name = _first_text(nested, name_keys)
            tag = _first_text(nested, tag_keys)
            if name or tag:
                return name, tag
    return None, None


def _ordinal(value) -> str | None:
    if not isinstance(value, int) or value <= 0:
        return None
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _war_week_number(signal: dict) -> int | None:
    week = signal.get("week")
    if isinstance(week, int) and week > 0:
        return week
    section_index = signal.get("section_index")
    if isinstance(section_index, int) and section_index >= 0:
        return section_index + 1
    return None


def _war_period_label(signals: list[dict], *, include_week: bool = True) -> str | None:
    for signal in signals or []:
        season_id = signal.get("season_id")
        if not isinstance(season_id, int):
            continue
        if include_week:
            week = _war_week_number(signal)
            if week is not None:
                return f"S{season_id} W{week}"
        return f"S{season_id}"
    return None


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
        "Leader-actions Discord invite task:\n"
        f"{count_line}\n"
        "- Author 2-3 short Clash Royale clan-chat messages a leader can copy/paste in sequence.\n"
        "- Highlight why Discord is worth joining: war coordination, deck/screenshot help, milestone shoutouts, leader relay notes, or recent useful coordination.\n"
        f"- Include `{DISCORD_INVITE_ROUTE}` exactly once, preferably in the final copy/paste message.\n"
        "- Do not include raw URLs, markdown links, Discord-only formatting, message numbers, or labels inside the copy/paste messages.\n"
        f"- Keep each copy/paste message under {ARENA_RELAY_MAX_COPY_CHARS} characters.\n"
        "- Return `content` as a JSON array containing only the Clash Royale copy/paste messages. The code will add the leader-action card.\n"
    )
    if base_context:
        return f"{base_context}\n\n{instructions}"
    return instructions


def _with_clan_chat_feedback_context(base_context: str, memory_context: dict | None) -> str:
    durable = (memory_context or {}).get("durable_memories") or []
    lines = []
    for item in durable[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("event_id") or item.get("memory_id") or "").strip()
        body = str(item.get("body") or item.get("summary") or "").strip()
        if title and body:
            lines.append(f"- {title}: {body}")
        elif body:
            lines.append(f"- {body}")
    if not lines:
        return base_context
    return f"{base_context}\n\nRecent leader feedback for this clan-chat copy type:\n" + "\n".join(lines)


def _result_content_items(result: dict | None) -> list[str]:
    if isinstance(result, ClanChatCopyResult):
        return list(result.messages)
    if not isinstance(result, dict):
        return []
    messages = messages_from_agent_result(result)
    if messages:
        return messages
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

    membership = profile.get("membership_summary") or {}
    if membership.get("is_returning"):
        prior = membership.get("prior_stints")
        join_count = membership.get("join_count")
        parts = ["returning member"]
        if isinstance(join_count, int) and join_count > 1:
            parts.append(f"{join_count} recorded POAP KINGS stints")
        if membership.get("last_left_at"):
            parts.append(f"last left {membership['last_left_at']}")
        elif isinstance(prior, int) and prior > 0:
            parts.append(f"{prior} prior stint{'s' if prior != 1 else ''}")
        facts.append(f"- Clan membership: {'; '.join(parts)}")

    age_years = _profile_number(profile.get("cr_account_age_years"))
    if age_years:
        facts.append(f"- Years played/account age: {age_years} years")

    collection_summary = profile.get("card_collection_summary") or {}
    maxed_cards = _profile_number(collection_summary.get("maxed_cards_count"))
    if maxed_cards:
        facts.append(f"- Max-level cards: {maxed_cards:,}")

    collection_level = _profile_number(profile.get("cr_collection_level"))
    if collection_level:
        facts.append(f"- Collection Level: {collection_level:,}")

    badge_tier = _profile_number(profile.get("cr_collection_level_badge_tier"))
    badge_max = _profile_number(profile.get("cr_collection_level_badge_max_tier"))
    if badge_tier and badge_max:
        facts.append(f"- Collection Level badge tier: {badge_tier}/{badge_max}")

    favorite_card = str(profile.get("current_favourite_card_name") or "").strip()
    if favorite_card:
        facts.append(f"- Favorite card: {favorite_card}")

    highest_card_level = _profile_number(collection_summary.get("highest_level"))
    if highest_card_level:
        facts.append(f"- Highest card level: {highest_card_level}")

    challenge_max = _profile_number(profile.get("challenge_max_wins"))
    if challenge_max:
        facts.append(f"- Best challenge run: {challenge_max} wins")

    banner_count = _profile_number(profile.get("cr_banner_count"))
    if banner_count:
        facts.append(f"- Banner collection: {banner_count:,}")

    emote_count = _profile_number(profile.get("cr_emote_count"))
    if emote_count:
        facts.append(f"- Emote collection: {emote_count:,}")

    war_wins = _profile_number(profile.get("cr_clan_war_wins"))
    if war_wins:
        facts.append(f"- Fallback only - clan war wins: {war_wins:,}")

    battle_wins = _profile_number(profile.get("cr_battle_wins"))
    if battle_wins:
        facts.append(f"- Fallback only - battle wins: {battle_wins:,}")

    trophies = _profile_number(profile.get("trophies") or profile.get("current_trophies"))
    if trophies:
        facts.append(f"- Fallback only - current trophies: {trophies:,}")

    best_trophies = _profile_number(profile.get("best_trophies"))
    if best_trophies:
        facts.append(f"- Fallback only - best trophies: {best_trophies:,}")

    return facts


def _member_join_welcome_context(base_context: str | None, signal: dict, profile_facts: list[str] | None = None) -> str:
    facts = profile_facts if profile_facts is not None else _member_join_profile_facts(signal)
    fact_block = "\n".join(facts) if facts else "- No profile facts available beyond the join signal."
    instructions = (
        "Leader-actions new-member welcome task:\n"
        f"{fact_block}\n"
        "- Author one short Clash Royale clan-chat welcome a leader can copy/paste.\n"
        "- Include the member name exactly as provided when available.\n"
        "- Include `POAP KINGS` exactly in the copy/paste message.\n"
        "- If Clan membership says returning member, say welcome back rather than welcoming them as brand new.\n"
        "- Sound like a real leader typing in Clash Royale clan chat, not a polished announcement.\n"
        "- Use one or two distinctive profile facts when available. Prefer years played/account age, Collection Level, max-level cards, Collection Level badge tier, favorite card, challenge best, banner count, or emote count.\n"
        "- Use plain win counts or trophies only as fallback facts when nothing more distinctive is available.\n"
        "- Make the player feel like POAP KINGS noticed something specific about their profile.\n"
        "- Do not mention war state, boat defenses, Discord, onboarding, instructions, or what the player should do next.\n"
        "- Avoid corporate/promo phrases like 'serious battle experience', 'bring that energy', or 'we are looking for'.\n"
        "- Do not invent achievements, personality, role, Discord status, or future behavior.\n"
        "- Do not include raw URLs, markdown links, Discord-only formatting, message numbers, or labels inside the copy/paste message.\n"
        f"- Keep the copy/paste message under {ARENA_RELAY_WELCOME_MAX_COPY_CHARS} characters and ideally under 18 words.\n"
        "- Return `content` as a single string containing only the Clash Royale copy/paste message. The code will add the leader-action card.\n"
    )
    if base_context:
        return f"{base_context}\n\n{instructions}"
    return instructions


def _welcome_profile_fact_markers(profile_facts: list[str] | None) -> list[str]:
    markers = []
    for fact in profile_facts or []:
        text = str(fact or "").strip()
        if not text or text.startswith("- Name:") or text.startswith("- Player tag:"):
            continue
        if text.lower().startswith("- clan membership:"):
            if "returning member" in text.lower() and "welcome back" not in markers:
                markers.append("welcome back")
            continue
        for value in re.findall(r"\d[\d,]*", text):
            marker = value.replace(",", "")
            if marker and marker not in markers:
                markers.append(marker)
        if "Favorite card:" in text:
            card = text.split("Favorite card:", 1)[1].strip().lower()
            if card and card not in markers:
                markers.append(card)
    return markers


def _welcome_profile_is_returning(profile_facts: list[str] | None) -> bool:
    return any("clan membership:" in str(fact or "").lower() and "returning member" in str(fact or "").lower() for fact in profile_facts or [])


def _welcome_copy_mentions_profile_fact(copy: str, profile_facts: list[str] | None) -> bool:
    markers = _welcome_profile_fact_markers(profile_facts)
    if not markers:
        return True
    clean_copy = copy.lower().replace(",", "")
    return any(marker in clean_copy for marker in markers)


def _welcome_profile_fact_phrases(profile_facts: list[str] | None) -> list[str]:
    phrases = []
    for fact in profile_facts or []:
        text = str(fact or "").strip()
        if not text or text.startswith("- Name:") or text.startswith("- Player tag:"):
            continue
        label, _, value = text.lstrip("- ").partition(":")
        value = value.strip()
        if not value:
            continue
        if label == "Years played/account age":
            phrases.append(f"{value} played")
        elif label == "Max-level cards":
            phrases.append(f"{value} max cards")
        elif label == "Collection Level":
            phrases.append(f"Collection Level {value}")
        elif label == "Collection Level badge tier":
            phrases.append(f"Collection badge {value}")
        elif label == "Favorite card":
            phrases.append(f"{value} as a favorite card")
        elif label == "Highest card level":
            phrases.append(f"level {value} cards")
        elif label == "Best challenge run":
            phrases.append(f"{value} challenge best")
        elif label == "Banner collection":
            phrases.append(f"{value} banners")
        elif label == "Emote collection":
            phrases.append(f"{value} emotes")
        elif label == "Clan membership" and "returning member" in value.lower():
            phrases.append("back with POAP KINGS")
        elif label.startswith("Fallback only - battle wins"):
            phrases.append(f"{value} battle wins")
        elif label.startswith("Fallback only - current trophies"):
            phrases.append(f"{value} trophies")
        elif label.startswith("Fallback only - best trophies"):
            phrases.append(f"{value} best trophies")
        elif label.startswith("Fallback only - clan war wins"):
            phrases.append(f"{value} clan war wins")
    return phrases


def _fallback_welcome_relay_copy(signal: dict, profile_facts: list[str] | None) -> str:
    name = str((signal or {}).get("name") or "new member").strip() or "new member"
    phrases = _welcome_profile_fact_phrases(profile_facts)
    returning = _welcome_profile_is_returning(profile_facts)
    if len(phrases) >= 2:
        fact = f"{phrases[0]} and {phrases[1]}"
    elif phrases:
        fact = phrases[0]
    else:
        fact = "glad you are here"
    prefix = "Welcome back to POAP KINGS" if returning else "Welcome to POAP KINGS"
    return _clip_relay_copy(
        f"{prefix}, {name}! {fact} stands out.",
        limit=ARENA_RELAY_WELCOME_MAX_COPY_CHARS,
    )


def _build_fallback_welcome_relay_result(
    signals: list[dict],
    *,
    profile_facts: list[str] | None = None,
) -> dict | None:
    primary = (signals or [{}])[0] or {}
    copy = _fallback_welcome_relay_copy(primary, profile_facts)
    return _build_generated_welcome_relay_result(
        signals,
        {"content": copy},
        profile_facts=profile_facts,
    )


def _build_generated_welcome_relay_result(
    signals: list[dict],
    generated: dict | None,
    *,
    profile_facts: list[str] | None = None,
) -> dict | None:
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
    if "poap kings" not in copy_lower:
        return None
    if name != "new member" and name.lower() not in copy_lower:
        return None
    if _welcome_profile_is_returning(profile_facts) and "welcome back" not in copy_lower:
        return None
    if not _welcome_copy_mentions_profile_fact(copy, profile_facts):
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


def _top_war_champ_entries(signals: list[dict], *, limit: int = 3) -> list[dict]:
    for signal in signals or []:
        if signal.get("type") != "war_champ_standings":
            continue
        standings = signal.get("standings")
        if isinstance(standings, list):
            return [entry for entry in standings[:limit] if isinstance(entry, dict)]
    for signal in signals or []:
        if signal.get("type") != "season_awards_granted":
            continue
        standings = signal.get("war_champ")
        if isinstance(standings, list):
            return [entry for entry in standings[:limit] if isinstance(entry, dict)]
    return []


def _war_champ_entry_name(entry: dict) -> str | None:
    name = str(entry.get("name") or entry.get("player_name") or "").strip()
    return name or None


def _war_champ_entry_fame(entry: dict) -> int | None:
    for key in ("total_fame", "metric_value", "fame"):
        value = entry.get(key)
        if isinstance(value, int):
            return value
    return None


def _format_war_champ_standings_line(entries: list[dict], *, label: str | None) -> str | None:
    if not entries:
        return None
    leader = entries[0]
    leader_name = _war_champ_entry_name(leader)
    if not leader_name:
        return None
    leader_fame = _war_champ_entry_fame(leader)
    prefix = f"War Champ after {label}" if label else "War Champ race"
    if leader_fame is not None:
        line = f"{prefix}: {leader_name} leads with {leader_fame:,} fame"
    else:
        line = f"{prefix}: {leader_name} leads"
    chasers = [_war_champ_entry_name(entry) for entry in entries[1:3]]
    chasers = [name for name in chasers if name]
    if len(chasers) == 1:
        line += f"; {chasers[0]} is chasing"
    elif len(chasers) >= 2:
        line += f"; {chasers[0]} and {chasers[1]} are chasing"
    return f"{line}."


def _fallback_war_relay_messages(signals: list[dict]) -> list[str]:
    signals = signals or []
    label = _war_period_label(signals)
    messages: list[str] = []
    week_signal = next(
        (
            signal for signal in signals
            if signal.get("type") in {"war_week_complete", "war_completed"}
        ),
        None,
    )
    if week_signal:
        rank = _ordinal(week_signal.get("our_rank") or week_signal.get("race_rank"))
        fame = week_signal.get("our_fame") or week_signal.get("clan_fame") or week_signal.get("fame")
        prefix = f"{label} war recap" if label else "War week recap"
        if rank and isinstance(fame, int):
            messages.append(
                f"{prefix}: POAP KINGS finished {rank} with {fame:,} fame. Thanks to everyone who used decks."
            )
        elif rank:
            messages.append(
                f"{prefix}: POAP KINGS finished {rank}. Thanks to everyone who used decks."
            )
        else:
            messages.append(
                f"{prefix}: thanks to everyone who used decks and kept POAP KINGS moving."
            )

    champ_line = _format_war_champ_standings_line(
        _top_war_champ_entries(signals),
        label=label,
    )
    if champ_line:
        messages.append(champ_line)

    if not messages and any(signal.get("type") == "war_season_complete" for signal in signals):
        season_label = _war_period_label(signals, include_week=False)
        prefix = f"{season_label} war season" if season_label else "War season"
        messages.append(f"{prefix} complete. Thanks to everyone who kept showing up for POAP KINGS.")

    return messages[:2]


def _war_relay_context(base_context: str | None, signals: list[dict], memory_context: dict | None) -> str:
    label = _war_period_label(signals)
    required_label = f"- Use the short in-game label `{label}` exactly once or more." if label else "- If season/week is missing, do not invent it."
    instructions = (
        "Arena-relay Clan Wars recap task:\n"
        f"{required_label}\n"
        "- Author one or two short Clash Royale clan-chat messages a leader can copy/paste.\n"
        "- If a completed week is present, include the weekly recap: finish rank, fame, and a concise thanks to people who used decks.\n"
        "- If War Champ standings are present, include the current War Champ leader and one or two chasers. This can be a second message.\n"
        "- Use the exact in-game compact style for clan wars labels: Season 134 Week 3 is `S134 W3`.\n"
        "- Sound like a real clan leader in Clash Royale chat, not a Discord announcement or newsletter.\n"
        "- Do not over-explain rules, fairness, Discord, APIs, prompts, or hidden system behavior.\n"
        "- Do not include raw URLs, markdown links, Discord-only formatting, message numbers, or labels inside the copy/paste messages.\n"
        f"- Keep each copy/paste message under {ARENA_RELAY_MAX_COPY_CHARS} characters.\n"
        "- Return `content` as a JSON array containing only the Clash Royale copy/paste messages. The code will add the leader-action card.\n\n"
        "War signals:\n"
        f"```json\n{json.dumps(signals or [], indent=2, default=str)}\n```"
    )
    context = f"{base_context}\n\n{instructions}" if base_context else instructions
    return _with_clan_chat_feedback_context(context, memory_context)


def _build_generated_war_relay_result(signals: list[dict], generated: ClanChatCopyResult | dict | None) -> dict | None:
    copies = [_clip_relay_copy(item) for item in _result_content_items(generated)]
    copies = [item for item in copies if item]
    if not copies:
        return None
    copies = copies[:2]
    label = _war_period_label(signals)
    has_standings = bool(_top_war_champ_entries(signals))
    objective = "war_recap_and_champ_race" if has_standings else "war_recap"
    reason = (
        "Weekly war recaps and War Champ standings belong in game chat because many contributors never see Discord."
        if has_standings
        else "Weekly war recaps belong in game chat because many contributors never see Discord."
    )
    title = "Clan Wars relay"
    copy_instruction = (
        "📋 Copy the next message into Clash Royale."
        if len(copies) == 1
        else f"📋 Copy the next {len(copies)} messages into Clash Royale in order."
    )
    card = (
        f"**R? 📣 {title}**\n"
        f"🎯 `{objective}`\n"
        f"{copy_instruction}\n"
        f"🧠 {reason}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )
    relay_copy = copies[0] if len(copies) == 1 else copies
    relay_copy_text = "\n".join(copies)
    return {
        "event_type": "war_relay_brief",
        "summary": f"In-game war relay suggestion: {relay_copy_text}",
        "content": [card, *copies],
        "metadata": {
            "action_type": "in_game_relay",
            "objective": objective,
            "rationale": reason,
            "relay_copy": relay_copy,
            "relay_copy_text": relay_copy_text,
            "relay_copy_count": len(copies),
            "relay_target": "clash_royale_clan_chat",
            "copy_message_index": 1,
            "war_period_label": label,
            "authored_by": "elixir_agent",
        },
    }


async def _generate_war_relay_result(
    signals: list[dict],
    *,
    base_context: str | None = None,
    memory_context: dict | None = None,
) -> dict | None:
    label = _war_period_label(signals)
    required_terms = (label,) if label else ()
    if _top_war_champ_entries(signals):
        required_terms = tuple(term for term in (*required_terms, "War Champ") if term)
    generated = await generate_clan_chat_copy(
        intent="war_weekly_recap_relay",
        context=_war_relay_context(base_context, signals, memory_context),
        max_messages=2,
        max_chars=ARENA_RELAY_MAX_COPY_CHARS,
        required_terms=required_terms,
        forbidden_terms=("Discord", "http://", "https://", "www."),
        fallback_messages=_fallback_war_relay_messages(signals),
        metadata={"lane": "arena-relay", "war_period_label": label},
    )
    return _build_generated_war_relay_result(signals, generated)


def _season_awards_context(base_context: str | None, signals: list[dict], memory_context: dict | None) -> str:
    label = _war_period_label(signals, include_week=False)
    required_label = f"- Use the short season label `{label}` exactly once or more." if label else "- If season is missing, do not invent it."
    instructions = (
        "Arena-relay War Champ winner task:\n"
        f"{required_label}\n"
        "- Author one short Clash Royale clan-chat message a leader can copy/paste.\n"
        "- Announce the final War Champ winner for the completed season.\n"
        "- Include `War Champ`, the winner's name, and fame if provided.\n"
        "- Mention POAP KINGS only if it fits naturally.\n"
        "- Sound like in-game clan chat, not a trophy ceremony script.\n"
        "- Do not include raw URLs, markdown links, Discord-only formatting, message numbers, or labels inside the copy/paste message.\n"
        f"- Keep the copy/paste message under {ARENA_RELAY_MAX_COPY_CHARS} characters.\n"
        "- Return `content` as a single string containing only the Clash Royale copy/paste message. The code will add the leader-action card.\n\n"
        "Season awards signal:\n"
        f"```json\n{json.dumps(signals or [], indent=2, default=str)}\n```"
    )
    context = f"{base_context}\n\n{instructions}" if base_context else instructions
    return _with_clan_chat_feedback_context(context, memory_context)


def _fallback_war_champ_winner_message(signals: list[dict]) -> str | None:
    entries = _top_war_champ_entries(signals, limit=1)
    if not entries:
        return None
    winner = entries[0]
    name = _war_champ_entry_name(winner)
    if not name:
        return None
    fame = _war_champ_entry_fame(winner)
    label = _war_period_label(signals, include_week=False)
    prefix = f"{label} War Champ" if label else "War Champ"
    if fame is not None:
        return f"{prefix}: {name} wins it with {fame:,} fame. Huge season."
    return f"{prefix}: {name} wins it. Huge season."


def _build_generated_war_champ_winner_result(signals: list[dict], generated: ClanChatCopyResult | dict | None) -> dict | None:
    copies = [_clip_relay_copy(item) for item in _result_content_items(generated)]
    copies = [item for item in copies if item]
    if not copies:
        return None
    copy = copies[0]
    winner = (_top_war_champ_entries(signals, limit=1) or [{}])[0]
    reason = "The final War Champ winner is one of the clan's clearest season honors and belongs in game chat."
    card = (
        "**R? 📣 War Champ relay**\n"
        "🎯 `war_champ_winner`\n"
        "📋 Copy the next message into Clash Royale.\n"
        f"🧠 {reason}\n\n"
        "✅ done  ❌ decline  ↩️ reply with note"
    )
    return {
        "event_type": "war_champ_winner_relay",
        "summary": f"In-game War Champ relay suggestion: {copy}",
        "content": [card, copy],
        "metadata": {
            "action_type": "in_game_relay",
            "objective": "war_champ_winner",
            "rationale": reason,
            "relay_copy": copy,
            "relay_copy_text": copy,
            "relay_copy_count": 1,
            "relay_target": "clash_royale_clan_chat",
            "copy_message_index": 1,
            "target_player_tag": winner.get("tag"),
            "target_player_name": _war_champ_entry_name(winner),
            "war_period_label": _war_period_label(signals, include_week=False),
            "authored_by": "elixir_agent",
        },
    }


async def _generate_war_champ_winner_result(
    signals: list[dict],
    *,
    base_context: str | None = None,
    memory_context: dict | None = None,
) -> dict | None:
    winner_message = _fallback_war_champ_winner_message(signals)
    if not winner_message:
        return None
    winner = (_top_war_champ_entries(signals, limit=1) or [{}])[0]
    winner_name = _war_champ_entry_name(winner)
    label = _war_period_label(signals, include_week=False)
    required_terms = tuple(term for term in (label, "War Champ", winner_name) if term)
    generated = await generate_clan_chat_copy(
        intent="war_champ_winner_relay",
        context=_season_awards_context(base_context, signals, memory_context),
        max_messages=1,
        max_chars=ARENA_RELAY_MAX_COPY_CHARS,
        required_terms=required_terms,
        forbidden_terms=("Discord", "http://", "https://", "www."),
        fallback_messages=[winner_message],
        metadata={"lane": "arena-relay", "war_period_label": label},
    )
    return _build_generated_war_champ_winner_result(signals, generated)


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
        "decision_case_id": action.get("case_id"),
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
        "war_relay_brief",
        "war_champ_winner_relay",
    }


def _arena_relay_uses_leader_action_policy(intent: str | None) -> bool:
    """Welcomes are opportunistic and should not consume action-board budget."""
    return intent not in {
        "welcome_relay",
    }


def _provisional_leader_action_policy_shape(signal_types: set[str]) -> tuple[str, str | None]:
    """Pre-generation action type/objective for budget and cooldown checks."""
    if signal_types == {"discord_invite_reminder"}:
        return "discord_invite_relay", None
    if signal_types & CELEBRATION_RELAY_SIGNAL_TYPES:
        return "celebration_relay", None
    if signal_types & {"war_attacks_complete"}:
        return "in_game_relay", "war_recognition"
    if signal_types & {"war_week_complete", "war_completed", "war_champ_standings", "war_season_complete"}:
        return "in_game_relay", "war_recap"
    if signal_types & {
        "war_practice_phase_active",
        "war_practice_day_started",
        "war_final_practice_day",
        "war_battle_phase_active",
        "war_battle_day_started",
        "war_battle_day_live_update",
        "war_battle_day_final_hours",
        "war_final_battle_day",
    }:
        return "in_game_relay", "war_participation"
    return "in_game_relay", None


def _facade():
    from runtime.jobs import _signals as facade

    return facade


def _runtime_app():
    from runtime import app as runtime_app

    return runtime_app


def _bot():
    return _runtime_app().bot


def _arena_relay_sidecar_intent(signals: list[dict]) -> str | None:
    signals = signals or []
    if not signals:
        return None
    if all(is_war_signal(signal) for signal in signals):
        if any(is_war_relay_signal(signal) for signal in signals):
            return "war_relay_brief"
        return None
    if any(signal.get("type") == "member_join" for signal in signals):
        return "welcome_relay"
    if any(signal.get("type") == "discord_invite_reminder" for signal in signals):
        return "discord_invite_relay"
    if any(signal.get("type") in SEASON_AWARDS_SIGNAL_TYPES for signal in signals):
        return "war_champ_winner_relay"
    if (
        all(is_progression_signal(signal) for signal in signals)
        or all(is_progression_signal(signal) or is_battle_mode_signal(signal) for signal in signals)
    ):
        if any(is_arena_relay_celebration_signal(signal) for signal in signals):
            return "celebration_relay"
    return None


def _arena_relay_sidecar_outcome(signals: list[dict], channel_config: dict) -> dict | None:
    signals = signals or []
    intent = _arena_relay_sidecar_intent(signals)
    if not intent:
        return None
    source_key = signal_source_key(signals[0]) if len(signals) == 1 else batch_source_key(signals)
    signal_type = signals[0].get("type") if len(signals) == 1 else "signal_batch"
    return {
        "source_signal_key": source_key,
        "source_signal_type": signal_type or "signal_batch",
        "target_channel_key": _lane_key_for_config(channel_config) or "arena-relay",
        "target_channel_id": channel_config.get("id"),
        "intent": intent,
        "required": False,
        "payload": {
            "signals": signals,
            "source": "arena_relay_sidecar_intent",
        },
        "delivery_status": "planned",
    }


async def _create_arena_relay_communication_intent(outcome: dict, signals: list[dict]) -> dict | None:
    signal_keys = [signal_source_key(signal) for signal in (signals or []) if isinstance(signal, dict)]
    event_keys = [
        signal.get("event_key") for signal in (signals or [])
        if isinstance(signal, dict) and signal.get("event_key")
    ]
    try:
        return await asyncio.to_thread(
            db.upsert_communication_intent,
            intent_key=f"arena_relay:{outcome['source_signal_key']}:{outcome['intent']}",
            workflow="arena-relay",
            intent_type="action_card",
            status=db.INTENT_PLANNED,
            target_channel_key=outcome.get("target_channel_key"),
            target_channel_id=outcome.get("target_channel_id"),
            source_signal_key=outcome.get("source_signal_key"),
            source_signal_type=outcome.get("source_signal_type"),
            covers_signal_keys=signal_keys,
            event_keys=event_keys,
            summary=f"Arena relay sidecar: {outcome.get('intent')}",
            payload={
                "source": "arena_relay_sidecar",
                "outcome": outcome,
            },
        )
    except Exception:
        log.warning(
            "arena relay communication intent create failed signal_key=%s intent=%s",
            outcome.get("source_signal_key"),
            outcome.get("intent"),
            exc_info=True,
        )
        return None


async def _deliver_signal_outcome(outcome, signals, clan, war):
    facade = _facade()
    communication_intent_id = outcome.get("communication_intent_id") or outcome.get("intent_id")
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
        intent_id=communication_intent_id,
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
            intent_id=communication_intent_id,
        )
        if communication_intent_id is not None:
            await asyncio.to_thread(
                db.mark_communication_intent_failed,
                communication_intent_id,
                error_detail="channel not found",
                target_channel_id=outcome["target_channel_id"],
            )
        return False

    channel_id = channel_config["id"]
    lane_key = _lane_key_for_config(channel_config)
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
            intent_id=communication_intent_id,
            mark_attempt=True,
        )
        if communication_intent_id is not None:
            await asyncio.to_thread(
                db.mark_communication_intent_skipped,
                communication_intent_id,
                skipped_reason=reason,
            )
        return True
    if suppressed_public_signals:
        log.info(
            "public announcement filtered %s low-value departure signal(s) before generation",
            len(suppressed_public_signals),
        )

    recent_posts = await asyncio.to_thread(db.list_channel_messages, channel_id, 10, "assistant")
    memory_context = await asyncio.to_thread(
        build_lane_memory_context,
        channel_config,
        signals=delivery_signals,
    )

    from runtime.signal_lanes import TOURNAMENT_SIGNAL_TYPES, WAR_RECAP_SIGNAL_TYPES

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

        if lane_key == "arena-relay":
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
                    intent_id=communication_intent_id,
                    mark_attempt=True,
                )
                if communication_intent_id is not None:
                    await asyncio.to_thread(
                        db.mark_communication_intent_skipped,
                        communication_intent_id,
                        skipped_reason=f"arena_relay_cooldown:{ARENA_RELAY_COOLDOWN_HOURS}h",
                    )
                return True
            if _arena_relay_uses_leader_action_policy(outcome.get("intent")):
                signal_types = {signal.get("type") for signal in delivery_signals or []}
                critical = bool(signal_types & CRITICAL_LEADER_ACTION_SIGNAL_TYPES)
                provisional_type, provisional_objective = _provisional_leader_action_policy_shape(signal_types)
                allowed, reason = await asyncio.to_thread(
                    can_post_leader_action,
                    critical=critical,
                    action_type=provisional_type,
                    objective=provisional_objective,
                )
                if not allowed:
                    target_name, target_tag = _leader_action_skip_target(delivery_signals)
                    await post_leader_action_skip(
                        source="signal_delivery",
                        action_type=provisional_type,
                        reason=f"policy:{reason}",
                        target_player_name=target_name,
                        target_player_tag=target_tag,
                        signal_types=signal_types,
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
                        payload={"signals": signals},
                        error_detail=f"leader_action_policy:{reason}",
                        intent_id=communication_intent_id,
                        mark_attempt=True,
                    )
                    if communication_intent_id is not None:
                        await asyncio.to_thread(
                            db.mark_communication_intent_skipped,
                            communication_intent_id,
                            skipped_reason=f"leader_action_policy:{reason}",
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
                generated = await generate_clan_chat_copy(
                    intent="discord_invite_relay",
                    context=_with_clan_chat_feedback_context(
                        _discord_invite_relay_context(context),
                        action_memory_context,
                    ),
                    max_messages=3,
                    max_chars=ARENA_RELAY_MAX_COPY_CHARS,
                    exact_once_terms=(DISCORD_INVITE_ROUTE,),
                    forbidden_terms=("http://", "https://", "www."),
                    metadata={"channel": channel_config["name"], "lane": lane_key},
                )
                result = _build_generated_discord_invite_relay_result(delivery_signals, generated)
            elif outcome.get("intent") == "welcome_relay" and member_join_signals:
                welcome_profile_facts = await asyncio.to_thread(_member_join_profile_facts, member_join_signals[0])
                action_memory_context = _memory_context_with_leader_action_feedback(
                    memory_context,
                    await asyncio.to_thread(
                        db.list_leader_action_feedback_profiles,
                        action_type="welcome_relay",
                        limit=1,
                    ),
                )
                name = str(member_join_signals[0].get("name") or "").strip()
                required_terms = ("POAP KINGS", name) if name else ("POAP KINGS",)
                generated = await generate_clan_chat_copy(
                    intent="welcome_relay",
                    context=_with_clan_chat_feedback_context(
                        _member_join_welcome_context(context, member_join_signals[0], profile_facts=welcome_profile_facts),
                        action_memory_context,
                    ),
                    max_messages=1,
                    max_chars=ARENA_RELAY_WELCOME_MAX_COPY_CHARS,
                    required_terms=required_terms,
                    forbidden_terms=("Discord", "boat defenses", "onboarding", "http://", "https://", "www."),
                    fallback_messages=[_fallback_welcome_relay_copy(member_join_signals[0], welcome_profile_facts)],
                    metadata={"channel": channel_config["name"], "lane": lane_key},
                )
                result = _build_generated_welcome_relay_result(
                    member_join_signals,
                    generated,
                    profile_facts=welcome_profile_facts,
                )
                if result is None:
                    result = _build_fallback_welcome_relay_result(
                        member_join_signals,
                        profile_facts=welcome_profile_facts,
                    )
            elif outcome.get("intent") == "war_relay_brief":
                action_memory_context = _memory_context_with_leader_action_feedback(
                    memory_context,
                    await asyncio.to_thread(
                        db.list_leader_action_feedback_profiles,
                        action_type="in_game_relay",
                        limit=1,
                    ),
                )
                result = await _generate_war_relay_result(
                    delivery_signals,
                    base_context=context,
                    memory_context=action_memory_context,
                )
            elif outcome.get("intent") == "war_champ_winner_relay":
                action_memory_context = _memory_context_with_leader_action_feedback(
                    memory_context,
                    await asyncio.to_thread(
                        db.list_leader_action_feedback_profiles,
                        action_type="in_game_relay",
                        limit=1,
                    ),
                )
                result = await _generate_war_champ_winner_result(
                    delivery_signals,
                    base_context=context,
                    memory_context=action_memory_context,
                )
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
                    case_id=metadata.get("case_id"),
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
                lane_key,
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
                intent_id=communication_intent_id,
                mark_attempt=True,
            )
            if communication_intent_id is not None:
                if status == "skipped":
                    await asyncio.to_thread(
                        db.mark_communication_intent_skipped,
                        communication_intent_id,
                        skipped_reason="generator returned null",
                    )
                else:
                    await asyncio.to_thread(
                        db.mark_communication_intent_failed,
                        communication_intent_id,
                        error_detail="generator returned null",
                        target_channel_id=outcome["target_channel_id"],
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
                intent_id=communication_intent_id,
                mark_attempt=True,
            )
            if communication_intent_id is not None:
                await asyncio.to_thread(
                    db.mark_communication_intent_skipped,
                    communication_intent_id,
                    skipped_reason=f"llm_no_post: {reason}",
                )
            return True

        posts = app._entry_posts(result)
        metadata = result.get("metadata") if isinstance(result, dict) else {}
        sent_messages = []
        if lane_key == "arena-relay" and isinstance(metadata, dict) and metadata.get("leader_action_id"):
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
        if lane_key == "arena-relay":
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
            lane_key == "clan-events"
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
        sent_message_ids = []
        for index, post in enumerate(posts):
            sent_message = sent_messages[index] if index < len(sent_messages) else None
            sent_message_id = getattr(sent_message, "id", None)
            if sent_message_id is not None:
                sent_message_ids.append(sent_message_id)
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
                workflow=lane_key,
                event_type=post_event_type,
                discord_message_id=sent_message_id,
                intent_id=communication_intent_id,
                raw_json={
                    "source_signal_key": outcome["source_signal_key"],
                    "intent": outcome["intent"],
                    "communication_intent_id": communication_intent_id,
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
            intent_id=communication_intent_id,
            mark_attempt=True,
            delivered=True,
        )
        if communication_intent_id is not None:
            await asyncio.to_thread(
                db.mark_communication_intent_delivered,
                communication_intent_id,
                target_channel_id=channel_id,
                message_ids=sent_message_ids,
                payload={
                    "result": result,
                    "delivered_post_count": len(posts),
                },
            )
        body = "\n\n".join(posts)
        if lane_key != "arena-relay":
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
        if lane_key == "river-race" and facade._signal_group_needs_recap_memory(delivery_signals):
            await asyncio.to_thread(facade._store_recap_memories_for_signal_batch, delivery_signals, posts, channel_id)

        from runtime.helpers._common import _safe_create_task

        if lane_key != "arena-relay":
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
            intent_id=communication_intent_id,
            mark_attempt=True,
        )
        if communication_intent_id is not None:
            try:
                await asyncio.to_thread(
                    db.mark_communication_intent_failed,
                    communication_intent_id,
                    error_detail=str(exc),
                    target_channel_id=outcome.get("target_channel_id"),
                )
            except Exception:
                log.warning("communication intent failure mark failed intent_id=%s", communication_intent_id, exc_info=True)
        log.error(
            "Signal outcome delivery failed for %s/%s: %s",
            outcome["source_signal_key"],
            outcome["target_channel_key"],
            exc,
            exc_info=True,
        )
        return False


async def _deliver_signal_group(signals, clan, war, *, source_system: str = "signal_delivery", source_detector: str | None = None):
    """Transition-only legacy signal router.

    Scheduled/runtime paths should use `_deliver_signal_group_via_awareness`
    or an intent-first specialized path. This remains for legacy behavior
    coverage and for narrow compatibility while direct callers are retired.
    """
    facade = _facade()
    await _record_signal_events(
        signals,
        source_system=source_system,
        source_detector=source_detector,
    )
    await _upsert_decision_cases_from_signals(signals, source_system=source_system)
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
    try:
        channel_config = facade._channel_config_by_key("arena-relay")
    except RuntimeError:
        log.warning("arena relay sidecar skipped: arena-relay channel not configured")
        return 0
    outcome = _arena_relay_sidecar_outcome(signals or [], channel_config)
    if not outcome:
        return 0
    intent = await _create_arena_relay_communication_intent(outcome, signals or [])
    if intent and intent.get("intent_id") is not None:
        outcome = dict(outcome)
        outcome["communication_intent_id"] = intent["intent_id"]
    ok = await facade._deliver_signal_outcome(outcome, signals, clan, war)
    if ok:
        delivered += 1
    return delivered


async def _create_awareness_post_intent(post: dict, signals: list[dict], *, workflow: str | None, situation: dict | None) -> dict | None:
    try:
        return await asyncio.to_thread(
            db.create_awareness_post_intent,
            post,
            signals or [],
            workflow=workflow or "awareness",
            situation=situation,
        )
    except Exception:
        log.warning(
            "communication intent create failed for awareness post channel=%r",
            (post or {}).get("channel"),
            exc_info=True,
        )
        return None


async def _create_awareness_skip_intent(plan: dict, signals: list[dict], *, workflow: str | None, situation: dict | None) -> dict | None:
    skipped_reason = (plan or {}).get("skipped_reason")
    if not skipped_reason:
        return None
    try:
        return await asyncio.to_thread(
            db.create_awareness_skip_intent,
            signals or [],
            workflow=workflow or "awareness",
            skipped_reason=skipped_reason,
            situation=situation,
        )
    except Exception:
        log.warning("communication skip intent create failed for workflow=%r", workflow, exc_info=True)
        return None


async def _mark_awareness_intent_failed(
    intent: dict | None,
    reason: str,
    *,
    target_channel_id: str | int | None = None,
    payload: dict | None = None,
) -> None:
    intent_id = (intent or {}).get("intent_id")
    if intent_id is None:
        return
    try:
        await asyncio.to_thread(
            db.mark_communication_intent_failed,
            intent_id,
            error_detail=reason,
            target_channel_id=target_channel_id,
            payload=payload,
        )
    except Exception:
        log.warning("communication intent failure mark failed intent_id=%s", intent_id, exc_info=True)


async def _deliver_awareness_post(post: dict, signals: list[dict], *, intent: dict | None = None) -> bool:
    facade = _facade()
    from runtime.situation import CHANNEL_LANES

    channel_key = (post.get("channel") or "").strip()
    if channel_key not in CHANNEL_LANES:
        log.warning("awareness post rejected: unknown channel %r", channel_key)
        await _mark_awareness_intent_failed(intent, f"unknown channel: {channel_key or '<empty>'}")
        return False
    leads_with = (post.get("leads_with") or "").strip()
    if leads_with and leads_with not in CHANNEL_LANES[channel_key]:
        log.warning(
            "awareness post rejected: leads_with=%r not allowed on channel=%r (allowed=%s)",
            leads_with,
            channel_key,
            sorted(CHANNEL_LANES[channel_key]),
        )
        await _mark_awareness_intent_failed(intent, f"lane mismatch: {leads_with} not allowed on {channel_key}")
        return False

    raw_covers = list(post.get("covers_signal_keys") or [])
    covers = sorted(_normalized_cover_keys(raw_covers, signals))
    post["covers_signal_keys"] = covers
    if signals and not covers:
        reason = "empty covers_signal_keys" if not raw_covers else "covers_signal_keys did not match input signals"
        log.warning(
            "awareness post rejected: %s channel=%r covers=%s input_signals=%d",
            reason,
            channel_key,
            raw_covers,
            len(signals),
        )
        await _mark_awareness_intent_failed(intent, reason)
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
                await _mark_awareness_intent_failed(intent, "leadership-only signal routed to public channel")
                return False

    try:
        channel_config = facade._channel_config_by_key(channel_key)
    except RuntimeError:
        log.warning("awareness post rejected: channel %r not configured", channel_key)
        await _mark_awareness_intent_failed(intent, f"channel not configured: {channel_key}")
        return False
    channel = _bot().get_channel(channel_config["id"])
    if not channel:
        log.warning("awareness post rejected: channel %r not found in Discord", channel_key)
        await _mark_awareness_intent_failed(
            intent,
            f"channel not found: {channel_key}",
            target_channel_id=channel_config["id"],
        )
        return False

    content = post.get("content")
    if not content:
        log.warning("awareness post on %r had empty content", channel_key)
        await _mark_awareness_intent_failed(intent, "empty content", target_channel_id=channel_config["id"])
        return False

    result = {
        "event_type": post.get("event_type") or "awareness_update",
        "summary": post.get("summary"),
        "content": content,
    }
    try:
        sent_messages = await facade._post_to_elixir(channel, result)
    except Exception as exc:
        log.error("awareness post send failed channel=%r", channel_key, exc_info=True)
        await _mark_awareness_intent_failed(
            intent,
            str(exc),
            target_channel_id=channel_config["id"],
            payload={"result": result},
        )
        return False
    if not isinstance(sent_messages, list):
        sent_messages = []

    app = _runtime_app()
    posts = app._entry_posts(result)
    channel_id = channel_config["id"]
    lane_key = _lane_key_for_config(channel_config)
    channel_name = getattr(channel, "name", None)
    if not isinstance(channel_name, str):
        channel_name = None
    channel_kind = getattr(channel, "type", None)
    if channel_kind is not None:
        channel_kind = str(channel_kind)
    summary = result.get("summary")
    event_type = result.get("event_type")
    intent_id = (intent or {}).get("intent_id")
    sent_message_ids = []
    for index, body_part in enumerate(posts):
        sent_message = sent_messages[index] if index < len(sent_messages) else None
        sent_message_id = getattr(sent_message, "id", None)
        if sent_message_id is not None:
            sent_message_ids.append(sent_message_id)
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
            workflow=lane_key,
            event_type=post_event_type,
            discord_message_id=sent_message_id,
            intent_id=intent_id,
            raw_json={
                "source": "awareness_loop",
                "communication_intent_id": intent_id,
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
            intent_id=intent_id,
            mark_attempt=True,
            delivered=True,
        )

    if intent_id is not None:
        try:
            await asyncio.to_thread(
                db.mark_communication_intent_delivered,
                intent_id,
                target_channel_id=channel_id,
                message_ids=sent_message_ids,
                payload={
                    "result": result,
                    "delivered_post_count": len(posts),
                    "covered_signal_keys": covers,
                },
            )
        except Exception:
            log.warning("communication intent delivered mark failed intent_id=%s", intent_id, exc_info=True)

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


async def _deliver_awareness_post_plan(
    plan: dict,
    signals: list[dict],
    *,
    workflow: str | None = None,
    situation: dict | None = None,
) -> dict:
    facade = _facade()
    posts = (plan or {}).get("posts") or []
    delivered = 0
    rejected = 0
    invalid_cover_rejected = 0
    covered: set[str] = set()
    attempted: set[str] = set()
    if not posts:
        hard_required_keys = {hp.get("signal_key") for hp in ((situation or {}).get("hard_post_signals") or [])}
        skippable_signals = [
            signal for signal in (signals or [])
            if signal_source_key(signal) not in hard_required_keys
        ]
        due_cases = ((situation or {}).get("decision_cases") or {}).get("due") or []
        if skippable_signals or due_cases:
            await _create_awareness_skip_intent(
                plan or {},
                skippable_signals,
                workflow=workflow,
                situation=situation,
            )
    for post in posts:
        post_keys = _normalized_cover_keys(post.get("covers_signal_keys") or [], signals or [])
        if post_keys:
            post = dict(post)
            post["covers_signal_keys"] = sorted(post_keys)
        attempted |= post_keys
        intent = await _create_awareness_post_intent(
            post,
            signals or [],
            workflow=workflow,
            situation=situation,
        )
        ok = await facade._deliver_awareness_post(post, signals or [], intent=intent)
        if ok:
            delivered += 1
            covered |= post_keys
        else:
            rejected += 1
            if signals and not post_keys:
                invalid_cover_rejected += 1

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
        "invalid_cover_rejected": invalid_cover_rejected,
        "covered_signal_keys": covered,
        "attempted_signal_keys": attempted,
    }


async def _deliver_signal_group_via_awareness(signals, clan, war, *, workflow: str | None = None) -> bool:
    facade = _facade()
    from heartbeat import HeartbeatTickResult
    from runtime.situation import build_situation, situation_is_quiet

    await _record_signal_events(
        signals,
        source_system=workflow or "awareness",
        source_detector=workflow,
    )
    await _upsert_decision_cases_from_signals(signals, source_system=workflow or "awareness")
    bundle = HeartbeatTickResult(signals=signals or [], clan=clan or {}, war=war or {})
    if workflow == "player_intel":
        situation = build_situation(
            bundle,
            channel_keys=["member-highlights"],
            include_leadership_events=False,
            include_decision_cases=False,
            include_leader_action_board=False,
        )
    else:
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
        log.warning("awareness loop returned no plan; recording coverage gap")
        await _create_awareness_coverage_gap_intent(
            signals or [],
            workflow=workflow,
            situation=situation,
            reason="awareness loop returned no post plan",
        )
        try:
            await asyncio.to_thread(
                db.record_awareness_tick,
                workflow=workflow,
                signals_in=len(signals or []),
                posts_delivered=0,
                posts_rejected=0,
                covered_keys=0,
                considered_skipped=0,
                hard_fallback=len(situation.get("hard_post_signals") or []),
                hard_fallback_failed=len(situation.get("hard_post_signals") or []),
                all_ok=False,
                skipped_reason="awareness loop returned no post plan",
                signal_outcomes=[
                    {
                        "signal_key": signal_source_key(signal),
                        "signal_type": signal.get("type") or "",
                        "event_key": signal.get("event_key"),
                        "event_id": signal.get("event_id"),
                        "status": "coverage_gap",
                    }
                    for signal in (signals or [])
                ],
                write_calls_issued=int(tool_stats.get("write_calls_issued", 0)),
                write_calls_succeeded=int(tool_stats.get("write_calls_succeeded", 0)),
                write_calls_denied=int(tool_stats.get("write_calls_denied", 0)),
            )
        except Exception:
            log.warning("record_awareness_tick failed", exc_info=True)
        return False

    report = await facade._deliver_awareness_post_plan(
        plan,
        signals,
        workflow=workflow,
        situation=situation,
    )
    relay_sidecars = await facade._deliver_arena_relay_sidecars(signals, clan, war)

    hard_required_keys = {hp.get("signal_key") for hp in (situation.get("hard_post_signals") or [])}
    covered_keys = report["covered_signal_keys"]
    uncovered = [
        signal for signal in (signals or [])
        if signal_source_key(signal) in hard_required_keys
        and signal_source_key(signal) not in covered_keys
    ]
    invalid_cover_rejected = int(report.get("invalid_cover_rejected") or 0)
    actionable_rejected = max(0, int(report["rejected"]) - invalid_cover_rejected)
    all_ok = actionable_rejected == 0
    if uncovered:
        log.warning(
            "awareness loop: %d hard-post-floor signal(s) uncovered by post plan",
            len(uncovered),
        )
        await _create_awareness_coverage_gap_intent(
            uncovered,
            workflow=workflow,
            situation=situation,
            reason="hard-post-floor signal was not covered by the awareness post plan",
        )
        all_ok = False

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
    if invalid_cover_rejected and not covered_keys:
        # The agent tried to post, but the post covered no input signal. With
        # no successful sibling post, keep soft signals retryable instead of
        # burning them as intentionally skipped.
        post_failed_keys.update(
            signal_source_key(signal)
            for signal in (signals or [])
            if signal_source_key(signal) not in hard_required_keys
        )
        all_ok = False

    considered_skipped = [
        signal for signal in (signals or [])
        if signal_source_key(signal) not in covered_keys
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
        "hard_uncovered=%d relay_sidecars=%d signals_in=%d degraded_blocks=%d skipped_reason=%r",
        workflow,
        report["delivered"],
        report["rejected"],
        len(covered_keys),
        len(considered_skipped),
        len(uncovered),
        relay_sidecars,
        len(signals or []),
        len(situation.get("_degraded_blocks") or []),
        (plan or {}).get("skipped_reason"),
    )

    uncovered_keys = {signal_source_key(s) for s in uncovered}
    signal_outcomes: list[dict] = []
    for signal in (signals or []):
        key = signal_source_key(signal)
        if key in covered_keys:
            status = "covered"
        elif key in uncovered_keys:
            status = "coverage_gap"
        elif key in post_failed_keys:
            status = "post_failed"
        else:
            status = "skipped"
        signal_outcomes.append({
            "signal_key": key,
            "signal_type": signal.get("type") or "",
            "event_key": signal.get("event_key"),
            "event_id": signal.get("event_id"),
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
            hard_fallback_failed=len(uncovered),
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
