"""Signal family classification and Discord lane helpers.

This module owns the legacy signal-to-lane planner plus shared lane memory
context helpers. It does not run independent channel agents; proactive
production delivery now flows through the awareness loop and communication
intents.
"""

from __future__ import annotations

import db
import prompts
from signal_keys import batch_source_key, signal_source_key
from memory_store import list_memories
from storage.contextual_memory import upsert_summary_memory

# Durable milestones — celebratory and infrequent, per-player. Routed to
# #player-highlights. Clan-aggregate records (clan_war_trophies_record) live
# in CLAN_EVENT_SIGNAL_TYPES instead — not a personal achievement.
PROGRESSION_SIGNAL_TYPES = {
    "arena_change",
    "player_level_up",
    "career_wins_milestone",
    "cr_account_anniversary",
    "new_card_unlocked",
    "new_champion_unlocked",
    "card_level_milestone",
    "card_evolution_unlocked",
    "badge_earned",
    "badge_level_milestone",
    "achievement_star_milestone",
    "best_trophies_peak",
    "challenge_performance_milestone",
}

# Volatile battle-mode activity outside of war — hot streaks, trophy pushes,
# Ranked / Path of Legend promotions/demotions, Ultimate Champion reaches, global
# rank attainments. Routed to #player-highlights.
BATTLE_MODE_SIGNAL_TYPES = {
    "battle_hot_streak",
    "battle_trophy_push",
    "path_of_legend_promotion",
    "path_of_legend_demotion",
    "ultimate_champion_reached",
    "path_of_legend_global_rank_attained",
}

OPTIONAL_PROGRESSION_SIGNAL_TYPES = {
    "badge_level_milestone",
    "card_evolution_unlocked",
}

ARENA_RELAY_CELEBRATION_SIGNAL_TYPES = {
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

CLAN_EVENT_SIGNAL_TYPES = {
    "member_join",
    "member_leave",
    "elder_promotion",
    "join_anniversary",
    "member_birthday",
    "clan_birthday",
    "donation_leaders",
    "weekly_donation_leader",
    "member_active_again",
    "award_earned",
    "season_awards_granted",
    "clan_war_trophies_record",
}

# Clan-aggregate records — same lane as CLAN_EVENT_SIGNAL_TYPES but tagged
# separately so the post path can apply the no-"season high" framing.
CLAN_RECORD_SIGNAL_TYPES = {
    "clan_war_trophies_record",
}

TOURNAMENT_SIGNAL_TYPES = {
    "tournament_watching_started",
    "tournament_started",
    "tournament_lead_change",
    "tournament_ended",
    "tournament_participant_joined",
    "tournament_battle_played",
}

# War-recap signals — routed through a dedicated clean-context generator to
# keep the LLM from confabulating season/standings details from RAG memory
# or stale clan context (the 04-19 "Season 130 closes" misfire). Channels
# stay as normal (#river-race, #announcements, #leaders) via the
# existing war routing rule; only the generator changes.
WAR_RECAP_SIGNAL_TYPES = {
    "war_completed",
    "war_champ_standings",
    "war_season_complete",
}

WAR_RELAY_SIGNAL_TYPES = {
    "war_practice_phase_active",
    "war_practice_day_started",
    "war_final_practice_day",
    "war_battle_phase_active",
    "war_battle_day_started",
    "war_battle_day_live_update",
    "war_battle_day_final_hours",
    "war_final_battle_day",
    "war_attacks_complete",
    "war_week_complete",
    "war_completed",
    "war_champ_standings",
    "war_season_complete",
}

# Season awards — one aggregated post to #clan-events when a season's
# podium grants land. Replaces the old per-award Discord spam (~12 fires).
# Uses a dedicated clean-context generator so the signal payload is the
# only ground truth for names, fame, and ranks.
SEASON_AWARDS_SIGNAL_TYPES = {
    "season_awards_granted",
}

LEADERSHIP_ONLY_SIGNAL_TYPES = {
    "api_event_sentinel",
    "api_schema_sentinel",
    "inactive_members",
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
    "award_earned",
}


def signal_routing_summary() -> list[dict]:
    return [
        {
            "family": "war_*",
            "match": "all signals in the batch are war signals",
            "targets": [
                {"lane": "river-race", "intent": "war_update", "required": True},
                {"lane": "arena-relay", "intent": "war_relay_brief", "required": False, "condition": "war state is useful for an in-game clan chat relay"},
                {"lane": "leader-lounge", "intent": "war_ops_note", "required": False, "condition": "important rank swing, recovery need, or ops-relevant war state"},
            ],
        },
        {
            "family": "badge_level_milestone",
            "match": "all signals in the batch are badge level milestones",
            "targets": [
                {"lane": "member-highlights", "intent": "player_progress", "required": False},
            ],
        },
        {
            "family": "battle_mode",
            "match": "all signals in the batch are battle-mode signals (hot streak, trophy push, PoL promotion)",
            "targets": [
                {"lane": "member-highlights", "intent": "battle_mode_update", "required": True},
            ],
        },
        {
            "family": "progression",
            "match": "all signals in the batch are non-optional durable milestones",
            "targets": [
                {"lane": "member-highlights", "intent": "player_progress", "required": True},
            ],
        },
        {
            "family": "member_join",
            "match": "any signal in the batch is member_join",
            "targets": [
                {"lane": "clan-events", "intent": "member_join_public", "required": True},
                {"lane": "leader-lounge", "intent": "member_join_ops", "required": True},
                {"lane": "arena-relay", "intent": "welcome_relay", "required": False},
            ],
        },
        {
            "family": "public_system_update",
            "match": "capability_unlock with clan audience",
            "targets": [
                {"lane": "announcements", "intent": "system_update", "required": True},
            ],
        },
        {
            "family": "leadership_only",
            "match": "leadership-only signal type or leadership audience",
            "targets": [
                {"lane": "leader-lounge", "intent": "leadership_note", "required": True},
            ],
        },
        {
            "family": "tournament",
            "match": "any tournament signal",
            "targets": [
                {"lane": "clan-events", "intent": "tournament_live_update", "required": True},
            ],
        },
        {
            "family": "clan_event",
            "match": "any clan event signal not matched earlier",
            "targets": [
                {"lane": "clan-events", "intent": "clan_event_public", "required": True},
                {"lane": "leader-lounge", "intent": "clan_event_ops", "required": False, "condition": "elder promotion"},
            ],
        },
        {
            "family": "fallback",
            "match": "anything else",
            "targets": [
                {"lane": "leader-lounge", "intent": "leadership_note", "required": True},
            ],
        },
    ]


def is_war_signal(signal: dict) -> bool:
    return str((signal or {}).get("type") or "").startswith("war_")


def is_war_relay_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in WAR_RELAY_SIGNAL_TYPES


def is_progression_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in PROGRESSION_SIGNAL_TYPES


def is_arena_relay_celebration_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in ARENA_RELAY_CELEBRATION_SIGNAL_TYPES


def is_battle_mode_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in BATTLE_MODE_SIGNAL_TYPES


def is_tournament_signal(signal: dict) -> bool:
    return (signal or {}).get("type") in TOURNAMENT_SIGNAL_TYPES


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


def _signal_memory_event_id(source_signal_key: str, outcome: dict) -> str:
    return f"{source_signal_key}:{outcome['target_channel_key']}"


def plan_signal_outcomes(signals: list[dict]) -> list[dict]:
    """Legacy deterministic signal-to-lane planner.

    The awareness loop is the canonical proactive path. This planner remains
    for compatibility and for narrow sidecar behavior while older tests and
    runtime shims are retired.
    """
    signals = signals or []
    if not signals:
        return []

    source_key = signal_source_key(signals[0]) if len(signals) == 1 else batch_source_key(signals)
    signal_type = signals[0].get("type") or "signal_batch"
    outcomes = []

    def add(lane: str, intent: str, *, required: bool):
        channel = prompts.discord_singleton_lane(lane)
        outcomes.append({
            "source_signal_key": source_key,
            "source_signal_type": signal_type,
            "target_channel_key": channel["lane_key"],
            "target_channel_id": channel["id"],
            "intent": intent,
            "required": required,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        })

    if all(is_war_signal(signal) for signal in signals):
        add("river-race", "war_update", required=True)
        if any(is_war_relay_signal(signal) for signal in signals):
            add("arena-relay", "war_relay_brief", required=False)
        if any(
            signal.get("type") in {"war_battle_rank_change", "war_week_complete", "war_completed", "war_race_finished_live"}
            or signal.get("needs_lead_recovery")
            or (signal.get("race_rank") and signal.get("race_rank", 1) > 1)
            for signal in signals
        ):
            add("leader-lounge", "war_ops_note", required=False)
        return outcomes

    if all((signal.get("type") in OPTIONAL_PROGRESSION_SIGNAL_TYPES) for signal in signals):
        add("member-highlights", "player_progress", required=False)
        return outcomes

    if all(is_battle_mode_signal(signal) for signal in signals):
        add("member-highlights", "battle_mode_update", required=True)
        return outcomes

    if all(is_progression_signal(signal) for signal in signals):
        add("member-highlights", "player_progress", required=True)
        if any(is_arena_relay_celebration_signal(signal) for signal in signals):
            add("arena-relay", "celebration_relay", required=False)
        return outcomes

    # Mixed durable + battle-mode batches now share one public player-story lane.
    if all(
        is_progression_signal(signal) or is_battle_mode_signal(signal)
        for signal in signals
    ):
        add("member-highlights", "member_highlights", required=True)
        if any(is_arena_relay_celebration_signal(signal) for signal in signals):
            add("arena-relay", "celebration_relay", required=False)
        return outcomes

    if any((signal.get("type") == "member_join") for signal in signals):
        add("clan-events", "member_join_public", required=True)
        add("leader-lounge", "member_join_ops", required=True)
        add("arena-relay", "welcome_relay", required=False)
        return outcomes

    if any((signal.get("type") == "discord_invite_reminder") for signal in signals):
        add("arena-relay", "discord_invite_relay", required=False)
        return outcomes

    if any(is_public_system_signal(signal) for signal in signals):
        add("announcements", "system_update", required=True)
        return outcomes

    if any(is_leadership_only_signal(signal) for signal in signals):
        add("leader-lounge", "leadership_note", required=True)
        return outcomes

    if any(is_tournament_signal(signal) for signal in signals):
        add("clan-events", "tournament_live_update", required=True)
        return outcomes

    if any(signal.get("type") in SEASON_AWARDS_SIGNAL_TYPES for signal in signals):
        add("clan-events", "season_awards_post", required=True)
        add("arena-relay", "war_champ_winner_relay", required=False)
        return outcomes

    if any(is_clan_event_signal(signal) for signal in signals):
        add("clan-events", "clan_event_public", required=True)
        if any(signal.get("type") == "elder_promotion" for signal in signals):
            add("leader-lounge", "clan_event_ops", required=False)
        if any(is_arena_relay_celebration_signal(signal) for signal in signals):
            add("arena-relay", "celebration_relay", required=False)
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
    "ARENA_RELAY_CELEBRATION_SIGNAL_TYPES",
    "BATTLE_MODE_SIGNAL_TYPES",
    "batch_source_key",
    "is_arena_relay_celebration_signal",
    "is_battle_mode_signal",
    "is_progression_signal",
    "is_war_signal",
    "maybe_upsert_signal_memory",
    "plan_signal_outcomes",
    "signal_routing_summary",
    "signal_source_key",
]
