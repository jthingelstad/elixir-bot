"""Memory post-processing: summary distillation, inference extraction, and observational fact storage."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from anthropic import APIError, APIConnectionError

from agent.core import _create_chat_completion

log = logging.getLogger("elixir_agent.memory_tasks")

# Model selection is handled by _model_for_workflow() in agent.core —
# memory_inference and memory_distill route to the observation model.

# ── Summary distillation ───────────────────────────────────────────────────

_DISTILL_SYSTEM = (
    "Summarize the following message in 1-2 concise sentences. "
    "Capture the key intent or information. Output only the summary, nothing else."
)


def distill_summary(text: str) -> str | None:
    """Generate a 1-2 sentence summary of the given text via a lightweight LLM call."""
    text = (text or "").strip()
    if not text:
        return None
    # Very short messages are already their own summary
    if len(text) <= 120:
        return text

    try:
        resp = _create_chat_completion(
            workflow="memory_distill",
            model=None,
            messages=[
                {"role": "system", "content": _DISTILL_SYSTEM},
                {"role": "user", "content": text[:2000]},
            ],
            temperature=0.3,
            max_tokens=100,
            timeout=15,
        )
        content = resp.choices[0].message.content
        if content and content.strip():
            return content.strip()
        return None
    except (APIError, APIConnectionError):
        log.warning("distill_summary failed", exc_info=True)
        return None


# ── Inference fact extraction ──────────────────────────────────────────────

_INFERENCE_SYSTEM = """\
You are an analyst extracting durable facts from Clash Royale clan Discord messages.

Extract facts worth remembering long-term. Good examples:
- Member preferences ("king_thing prefers concise war summaries")
- Clan milestones ("reached 44 members in April 2026")
- Member roles/notes ("raquaza is primary war leader and founder")
- Leadership decisions ("Free Pass Royale awarded to top war contributor each season")
- Notable achievements ("Alpha hit 8000 trophies for the first time")
- Behavioral patterns ("Bravo consistently participates in every war race")

Do NOT extract:
- Routine greetings or small talk
- Temporary status that changes daily (current trophies, today's deck)
- Information that is just repeating game data without context
- Facts that are only relevant for the current conversation

Return a JSON array. Each element:
{"title": "short label", "body": "full fact text", "confidence": 0.5-0.95, "scope": "leadership"|"public", "tags": ["tag1"], "member_tag": "#TAG or null"}

confidence guide: 0.9+ for explicit statements, 0.7-0.9 for strong implications, 0.5-0.7 for weak inferences.
scope: use "leadership" for ops/decisions/personnel, "public" for achievements/milestones visible to all.

If nothing is worth extracting, return an empty array: []
Respond with ONLY the JSON array, no other text."""


def extract_inference_facts(content: str, context_label: str | None = None) -> list[dict]:
    """Extract durable facts from conversation or signal content."""
    content = (content or "").strip()
    if not content:
        return []

    user_msg = content[:3000]
    if context_label:
        user_msg = f"[Context: {context_label}]\n\n{user_msg}"

    try:
        resp = _create_chat_completion(
            workflow="memory_inference",
            model=None,
            messages=[
                {"role": "system", "content": _INFERENCE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=500,
            timeout=20,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return []
        facts = json.loads(raw)
        if not isinstance(facts, list):
            return []
        valid = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if not fact.get("title") or not fact.get("body"):
                continue
            conf = float(fact.get("confidence", 0.7))
            conf = max(0.5, min(0.95, conf))
            valid.append({
                "title": str(fact["title"]).strip(),
                "body": str(fact["body"]).strip(),
                "confidence": conf,
                "scope": fact.get("scope", "leadership") if fact.get("scope") in ("leadership", "public") else "leadership",
                "tags": [str(t).strip().lower() for t in (fact.get("tags") or []) if t],
                "member_tag": str(fact["member_tag"]).strip() if fact.get("member_tag") else None,
            })
        return valid
    except (json.JSONDecodeError, ValueError, TypeError):
        log.debug("extract_inference_facts JSON parse failed", exc_info=True)
        return []
    except (APIError, APIConnectionError):
        log.debug("extract_inference_facts failed", exc_info=True)
        return []


# ── Inference fact persistence ─────────────────────────────────────────────


def save_inference_facts(facts: list[dict], channel_id: str | int | None = None, conn=None) -> int:
    """De-duplicate and persist extracted inference facts. Returns count saved."""
    from memory_store import attach_tags, create_memory, search_memories

    saved = 0
    for fact in (facts or []):
        try:
            existing = search_memories(
                fact["title"],
                viewer_scope="system_internal",
                include_system_internal=True,
                filters={"source_type": "elixir_inference"},
                limit=3,
                conn=conn,
            )
            duplicate = False
            body_lower = fact["body"].lower()
            for result in existing:
                existing_body = (result.memory.get("body") or "").lower()
                # Simple overlap: skip if the existing body contains most of the new fact
                if body_lower in existing_body or existing_body in body_lower:
                    duplicate = True
                    break
                # Check title overlap too
                existing_title = (result.memory.get("title") or "").lower()
                if fact["title"].lower() == existing_title:
                    duplicate = True
                    break
            if duplicate:
                continue

            memory = create_memory(
                title=fact["title"],
                body=fact["body"],
                summary=fact["body"][:220],
                source_type="elixir_inference",
                is_inference=True,
                confidence=fact["confidence"],
                created_by="elixir:inference",
                scope=fact["scope"],
                channel_id=str(channel_id) if channel_id else None,
                member_tag=fact.get("member_tag"),
                conn=conn,
            )
            if memory and fact.get("tags"):
                attach_tags(memory["memory_id"], fact["tags"], actor="elixir:inference", conn=conn)
            saved += 1
        except (sqlite3.Error, KeyError, TypeError):
            log.warning("save_inference_facts: failed to save fact %r", fact.get("title"), exc_info=True)
    return saved


# ── Observational fact storage ────────────────────────────────────────────
#
# Structured signals carry complete data (tag, name, card, level, etc.).
# Instead of running LLM extraction on the generated post text, write
# facts directly from the signal dict — deterministic, fast, reliable.


def _card_level_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    card = signal.get("card_name")
    new_level = signal.get("new_level")
    tag = signal.get("tag")
    if not card or new_level is None or not tag:
        return None
    return {
        "title": f"{name}: {card} lv{new_level}",
        "body": f"{name} upgraded {card} to level {new_level}",
        "event_type": "card_level_milestone",
        "event_id": f"card_level:{tag}:{card}",
        "scope": "public",
        "tags": ["card-upgrade", "observation"],
    }


def _player_level_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    new_level = signal.get("new_level")
    tag = signal.get("tag")
    if new_level is None or not tag:
        return None
    return {
        "title": f"{name}: exp lv{new_level}",
        "body": f"{name} reached experience level {new_level}",
        "event_type": "player_level_up",
        "event_id": f"player_level:{tag}",
        "scope": "public",
        "tags": ["level-up", "observation"],
    }


def _pol_promotion_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    new_league = signal.get("new_league_number")
    trophies = signal.get("trophies")
    tag = signal.get("tag")
    if new_league is None or not tag:
        return None
    body = f"{name} promoted to Path of Legend league {new_league}"
    if trophies is not None:
        body += f" with {trophies} trophies"
    return {
        "title": f"{name}: PoL league {new_league}",
        "body": body,
        "event_type": "path_of_legend_promotion",
        "event_id": f"pol_league:{tag}",
        "scope": "public",
        "tags": ["path-of-legend", "observation"],
    }


def _card_unlocked_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    card = signal.get("card_name")
    rarity = signal.get("rarity")
    tag = signal.get("tag")
    if not card or not tag:
        return None
    body = f"{name} unlocked {card}"
    if rarity:
        body += f" ({rarity})"
    return {
        "title": f"{name}: unlocked {card}",
        "body": body,
        "event_type": "new_card_unlocked",
        "event_id": f"card_unlock:{tag}:{card}",
        "scope": "public",
        "tags": ["card-unlock", "observation"],
    }


def _badge_earned_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    badge_label = signal.get("badge_label") or signal.get("badge_name")
    badge_name = signal.get("badge_name")
    tag = signal.get("tag")
    if not badge_label or not tag:
        return None
    return {
        "title": f"{name}: {badge_label}",
        "body": f"{name} earned the {badge_label} badge",
        "event_type": "badge_earned",
        "event_id": f"badge:{tag}:{badge_name or badge_label}",
        "scope": "public",
        "tags": ["badge", "observation"],
    }


def _hot_streak_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    streak = signal.get("streak")
    win_rate = signal.get("win_rate")
    tag = signal.get("tag")
    if not streak or not tag:
        return None
    body = f"{name} is on a {streak}-game winning streak"
    if win_rate is not None:
        body += f" ({win_rate}% win rate)"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "title": f"{name}: {streak}-win streak",
        "body": body,
        "event_type": "battle_hot_streak",
        "event_id": f"hot_streak:{tag}:{today}",
        "scope": "public",
        "tags": ["battle-form", "observation"],
    }


def _trophy_push_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    trophy_delta = signal.get("trophy_delta")
    from_trophies = signal.get("from_trophies")
    to_trophies = signal.get("to_trophies")
    battle_count = signal.get("battle_count")
    tag = signal.get("tag")
    if not trophy_delta or not tag:
        return None
    body = f"{name} pushed {trophy_delta} trophies"
    if from_trophies is not None and to_trophies is not None:
        body += f" ({from_trophies}\u2192{to_trophies})"
    if battle_count:
        body += f" over {battle_count} battles"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "title": f"{name}: trophy push",
        "body": body,
        "event_type": "battle_trophy_push",
        "event_id": f"trophy_push:{tag}:{today}",
        "scope": "public",
        "tags": ["battle-form", "observation"],
    }


def _career_wins_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    milestone = signal.get("milestone")
    tag = signal.get("tag")
    if not milestone or not tag:
        return None
    return {
        "title": f"{name}: {milestone} career wins",
        "body": f"{name} reached {milestone} career wins",
        "event_type": "career_wins_milestone",
        "event_id": f"career_wins:{tag}:{milestone}",
        "scope": "public",
        "tags": ["milestone", "observation"],
    }


def _achievement_star_fact(signal: dict) -> dict | None:
    name = signal.get("name", "Unknown")
    achievement = signal.get("achievement_name")
    stars = signal.get("new_stars")
    tag = signal.get("tag")
    if not achievement or stars is None or not tag:
        return None
    return {
        "title": f"{name}: {achievement} \u2605{stars}",
        "body": f"{name} earned {stars} stars on {achievement}",
        "event_type": "achievement_star_milestone",
        "event_id": f"achievement:{tag}:{achievement}",
        "scope": "public",
        "tags": ["achievement", "observation"],
    }


# signal_type → fact mapper
_SIGNAL_FACT_MAP = {
    "card_level_milestone": _card_level_fact,
    "player_level_up": _player_level_fact,
    "path_of_legend_promotion": _pol_promotion_fact,
    "new_card_unlocked": _card_unlocked_fact,
    "new_champion_unlocked": _card_unlocked_fact,
    "badge_earned": _badge_earned_fact,
    "badge_level_milestone": _badge_earned_fact,
    "battle_hot_streak": _hot_streak_fact,
    "battle_trophy_push": _trophy_push_fact,
    "career_wins_milestone": _career_wins_fact,
    "achievement_star_milestone": _achievement_star_fact,
}


def store_observation_facts(signals: list[dict], channel_id: str | int | None = None, conn=None) -> int:
    """Store structured observation facts from signals. Returns count stored."""
    from db import _canon_tag, get_connection
    from storage.contextual_memory import upsert_summary_memory

    saved = 0
    close = conn is None
    conn = conn or get_connection()
    try:
        for signal in signals or []:
            tag = signal.get("tag")
            if not tag:
                continue
            signal_type = signal.get("type")
            mapper = _SIGNAL_FACT_MAP.get(signal_type)
            if not mapper:
                continue
            fact = mapper(signal)
            if not fact:
                continue
            member_row = conn.execute(
                "SELECT member_id FROM members WHERE player_tag = ?",
                (_canon_tag(tag),),
            ).fetchone()
            member_id = member_row["member_id"] if member_row else None
            try:
                result = upsert_summary_memory(
                    event_type=fact["event_type"],
                    event_id=fact["event_id"],
                    title=fact["title"],
                    body=fact["body"],
                    scope=fact["scope"],
                    created_by="elixir:observation",
                    tags=fact.get("tags"),
                    member_tag=tag,
                    member_id=member_id,
                    metadata={"channel_id": str(channel_id)} if channel_id else None,
                    conn=conn,
                )
                if result:
                    saved += 1
            except (sqlite3.Error, KeyError, TypeError):
                log.warning("store_observation_facts: failed for signal %s", signal_type, exc_info=True)
        return saved
    finally:
        if close:
            conn.close()


__all__ = [
    "distill_summary",
    "extract_inference_facts",
    "save_inference_facts",
    "store_observation_facts",
]
