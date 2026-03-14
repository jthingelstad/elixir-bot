from __future__ import annotations

import json
from hashlib import sha1

import db
import prompts
from memory_store import list_memories
from storage.contextual_memory import upsert_summary_memory

PROGRESSION_SIGNAL_TYPES = {
    "arena_change",
    "player_level_up",
    "career_wins_milestone",
    "path_of_legend_promotion",
    "new_card_unlocked",
    "new_champion_unlocked",
    "card_level_milestone",
    "badge_earned",
    "badge_level_milestone",
    "achievement_star_milestone",
    "battle_hot_streak",
    "battle_trophy_push",
}

CLAN_EVENT_SIGNAL_TYPES = {
    "member_join",
    "member_leave",
    "elder_promotion",
    "join_anniversary",
    "member_birthday",
    "clan_birthday",
    "donation_leaders",
}

LEADERSHIP_ONLY_SIGNAL_TYPES = {
    "inactive_members",
}

RELAYABLE_WAR_SIGNAL_TYPES = {
    "war_battle_day_started",
    "war_battle_day_live_update",
    "war_battle_rank_change",
    "war_battle_day_final_hours",
    "war_battle_day_complete",
    "war_final_practice_day",
    "war_final_battle_day",
    "war_practice_day_started",
    "war_practice_day_complete",
    "war_week_rollover",
    "war_season_rollover",
    "war_week_complete",
    "war_season_complete",
    "war_completed",
}

DURABLE_EVENT_SIGNAL_TYPES = {
    "member_join",
    "member_leave",
    "elder_promotion",
    "join_anniversary",
    "member_birthday",
    "clan_birthday",
    "war_week_complete",
    "war_season_complete",
    "weekly_clan_recap",
}


def signal_routing_summary() -> list[dict]:
    return [
        {
            "family": "war_*",
            "match": "all signals in the batch are war signals",
            "targets": [
                {"subagent": "river-race", "intent": "war_update", "required": True},
                {"subagent": "arena-relay", "intent": "war_relay", "required": False, "condition": "relay-worthy war update"},
                {"subagent": "leader-lounge", "intent": "war_ops_note", "required": False, "condition": "important rank swing, recovery need, or ops-relevant war state"},
            ],
        },
        {
            "family": "progression",
            "match": "all signals in the batch are progression signals",
            "targets": [
                {"subagent": "player-progress", "intent": "player_progress", "required": True},
            ],
        },
        {
            "family": "member_join",
            "match": "any signal in the batch is member_join",
            "targets": [
                {"subagent": "clan-events", "intent": "member_join_public", "required": True},
                {"subagent": "leader-lounge", "intent": "member_join_ops", "required": True},
                {"subagent": "arena-relay", "intent": "member_join_relay", "required": False},
            ],
        },
        {
            "family": "public_system_update",
            "match": "capability_unlock with clan audience",
            "targets": [
                {"subagent": "clan-events", "intent": "system_update", "required": True},
            ],
        },
        {
            "family": "leadership_only",
            "match": "leadership-only signal type or leadership audience",
            "targets": [
                {"subagent": "leader-lounge", "intent": "leadership_note", "required": True},
            ],
        },
        {
            "family": "clan_event",
            "match": "any clan event signal not matched earlier",
            "targets": [
                {"subagent": "clan-events", "intent": "clan_event_public", "required": True},
                {"subagent": "leader-lounge", "intent": "clan_event_ops", "required": False, "condition": "elder promotion"},
            ],
        },
        {
            "family": "fallback",
            "match": "anything else",
            "targets": [
                {"subagent": "leader-lounge", "intent": "leadership_note", "required": True},
            ],
        },
    ]


def signal_source_key(signal: dict) -> str:
    signal = signal or {}
    for key in ("signal_key", "signal_log_type"):
        value = (signal.get(key) or "").strip()
        if value:
            return value
    parts = [
        str(signal.get("type") or "signal"),
        str(signal.get("signal_date") or ""),
        str(signal.get("tag") or ""),
        str(signal.get("season_id") or ""),
        str(signal.get("week") or signal.get("section_index") or ""),
        str(signal.get("day_number") or ""),
        str(signal.get("milestone") or ""),
        str(signal.get("card_name") or ""),
    ]
    basis = "|".join(parts)
    if basis.strip("|"):
        return basis
    payload = json.dumps(signal, sort_keys=True, default=str)
    return f"signal:{sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def batch_source_key(signals: list[dict]) -> str:
    keys = sorted(signal_source_key(signal) for signal in (signals or []))
    payload = "|".join(keys)
    return f"batch:{sha1(payload.encode('utf-8')).hexdigest()[:16]}"


def is_war_signal(signal: dict) -> bool:
    return str((signal or {}).get("type") or "").startswith("war_")


def is_progression_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in PROGRESSION_SIGNAL_TYPES


def is_clan_event_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in CLAN_EVENT_SIGNAL_TYPES


def signal_audience(signal: dict) -> str:
    payload = (signal or {}).get("payload") or {}
    audience = (payload.get("audience") or (signal or {}).get("audience") or "").strip().lower()
    return audience or "clan"


def is_leadership_only_signal(signal: dict) -> bool:
    if (signal or {}).get("type") in LEADERSHIP_ONLY_SIGNAL_TYPES:
        return True
    return signal_audience(signal) == "leadership"


def is_public_system_signal(signal: dict) -> bool:
    return (signal or {}).get("type") == "capability_unlock" and signal_audience(signal) == "clan"


def _member_tag_from_signals(signals: list[dict]) -> str | None:
    for signal in signals or []:
        tag = signal.get("tag")
        if tag:
            return str(tag)
    return None


def _signal_memory_event_id(source_signal_key: str, outcome: dict) -> str:
    return f"{source_signal_key}:{outcome['target_channel_key']}"


def build_subagent_memory_context(channel_config: dict, *, discord_user_id=None, signals=None):
    member_tag = _member_tag_from_signals(signals or [])
    context = db.build_memory_context(
        discord_user_id=discord_user_id,
        member_tag=member_tag,
        channel_id=channel_config["id"],
        viewer_scope=channel_config.get("memory_scope") or "public",
    )
    if not channel_config.get("durable_memory_enabled"):
        return context

    filters = {}
    if member_tag:
        filters["member_tag"] = member_tag
    elif any(signal.get("week") is not None and signal.get("season_id") is not None for signal in (signals or [])):
        signal = next(
            signal for signal in signals
            if signal.get("week") is not None and signal.get("season_id") is not None
        )
        filters["war_week_id"] = f"{signal.get('season_id')}:{signal.get('week')}"
    elif any(signal.get("season_id") is not None for signal in (signals or [])):
        signal = next(signal for signal in signals if signal.get("season_id") is not None)
        filters["war_season_id"] = str(signal.get("season_id"))
    else:
        return context

    durable_memories = list_memories(
        viewer_scope=channel_config.get("memory_scope") or "public",
        filters=filters,
        limit=5,
    )
    if durable_memories:
        context["durable_memories"] = durable_memories
    return context


def plan_signal_outcomes(signals: list[dict]) -> list[dict]:
    signals = signals or []
    if not signals:
        return []

    source_key = signal_source_key(signals[0]) if len(signals) == 1 else batch_source_key(signals)
    signal_type = signals[0].get("type") or "signal_batch"
    outcomes = []

    def add(channel_subagent: str, intent: str, *, required: bool):
        channel = prompts.discord_singleton_subagent(channel_subagent)
        outcomes.append({
            "source_signal_key": source_key,
            "source_signal_type": signal_type,
            "target_channel_key": channel["subagent_key"],
            "target_channel_id": channel["id"],
            "intent": intent,
            "required": required,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        })

    if all(is_war_signal(signal) for signal in signals):
        add("river-race", "war_update", required=True)
        if any((signal.get("type") in RELAYABLE_WAR_SIGNAL_TYPES) or (signal.get("type") == "war_completed" and (signal.get("won") or signal.get("our_rank") == 1)) for signal in signals):
            add("arena-relay", "war_relay", required=False)
        if any(
            signal.get("type") in {"war_battle_rank_change", "war_week_complete", "war_completed"}
            or signal.get("needs_lead_recovery")
            or (signal.get("race_rank") and signal.get("race_rank", 1) > 1)
            for signal in signals
        ):
            add("leader-lounge", "war_ops_note", required=False)
        return outcomes

    if all(is_progression_signal(signal) for signal in signals):
        add("player-progress", "player_progress", required=True)
        return outcomes

    if any((signal.get("type") == "member_join") for signal in signals):
        add("clan-events", "member_join_public", required=True)
        add("leader-lounge", "member_join_ops", required=True)
        add("arena-relay", "member_join_relay", required=False)
        return outcomes

    if any(is_public_system_signal(signal) for signal in signals):
        add("clan-events", "system_update", required=True)
        return outcomes

    if any(is_leadership_only_signal(signal) for signal in signals):
        add("leader-lounge", "leadership_note", required=True)
        return outcomes

    if any(is_clan_event_signal(signal) for signal in signals):
        add("clan-events", "clan_event_public", required=True)
        if any(signal.get("type") == "elder_promotion" for signal in signals):
            add("leader-lounge", "clan_event_ops", required=False)
        return outcomes

    add("leader-lounge", "leadership_note", required=True)
    return outcomes


def maybe_upsert_signal_memory(*, source_signal_key: str, signal_type: str, body: str,
                               outcome: dict, signals: list[dict], conn=None) -> dict | None:
    if signal_type not in DURABLE_EVENT_SIGNAL_TYPES:
        return None
    text = (body or "").strip()
    if not text:
        return None
    memory_event_id = _signal_memory_event_id(source_signal_key, outcome)
    first = (signals or [{}])[0]
    existing = list_memories(
        viewer_scope="system_internal",
        include_system_internal=True,
        filters={"event_type": signal_type, "event_id": memory_event_id},
        limit=1,
        conn=conn,
    )
    metadata = {
        "source_signal_key": source_signal_key,
        "memory_event_id": memory_event_id,
        "target_channel_key": outcome["target_channel_key"],
        "outcome_intent": outcome["intent"],
        "member_tag": first.get("tag"),
        "war_week_id": f"{first.get('season_id')}:{first.get('week')}" if first.get("season_id") is not None and first.get("week") is not None else None,
        "war_season_id": str(first.get("season_id")) if first.get("season_id") is not None else None,
    }
    scope = "leadership" if outcome["target_channel_key"] == "leader-lounge" else "public"
    return upsert_summary_memory(
        event_type=signal_type,
        event_id=memory_event_id,
        title=(outcome.get("intent") or signal_type).replace("_", " ").title(),
        body=text,
        scope=scope,
        created_by=f"elixir:{outcome['target_channel_key']}",
        metadata=metadata,
        conn=conn,
    )


__all__ = [
    "build_subagent_memory_context",
    "is_progression_signal",
    "is_war_signal",
    "maybe_upsert_signal_memory",
    "plan_signal_outcomes",
    "signal_routing_summary",
    "signal_source_key",
]
