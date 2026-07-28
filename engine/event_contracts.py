"""Canonical vocabulary and routing metadata for durable domain events.

Emitters own detection; this registry owns what may cross the event-stream
boundary.  A new event type is therefore incomplete until its stream, payload
floor, time semantics, lane, and hard-post policy are declared together.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.normalize import canonical_utc_timestamp


@dataclass(frozen=True)
class EventContract:
    stream: str
    lane: str
    required_payload: frozenset[str] = frozenset()
    time_semantics: str = "observed_between_polls"
    hard_post: bool = False


def _event(
    stream: str,
    lane: str,
    *required_payload: str,
    time_semantics: str = "observed_between_polls",
    hard_post: bool = False,
) -> EventContract:
    return EventContract(
        stream=stream,
        lane=lane,
        required_payload=frozenset(required_payload),
        time_semantics=time_semantics,
        hard_post=hard_post,
    )


EVENT_CONTRACTS: dict[str, EventContract] = {
    # player stream
    "career_wins_milestone": _event("player", "milestone", "milestone", "wins"),
    "best_trophies_peak": _event("player", "milestone", "boundary", "best_trophies"),
    "badge_earned": _event("player", "milestone", "badge_name"),
    "arena_changed": _event("player", "milestone", "arena_id", "arena_name"),
    "card_unlocked": _event("player", "milestone", "card_id", "card_name", "rarity"),
    "card_level_milestone": _event("player", "milestone", "card_id", "card_name", "milestone"),
    "collection_level_milestone": _event("player", "milestone", "milestone", "collection_level"),
    "pol_promotion": _event("player", "battle_mode", "league", "prev_league"),
    "ultimate_champion_reached": _event("player", "battle_mode", "league"),
    "pol_global_rank_attained": _event("player", "battle_mode", "from_rank", "to_rank", "league"),
    "pol_season_closed": _event("player", "battle_mode", "pol_season_id", time_semantics="exact"),
    # clan stream
    "member_joined": _event("clan", "clan_event", "name", hard_post=True),
    "member_left": _event("clan", "clan_event", "name"),
    "member_left_verified": _event("clan", "clan_event", "name", hard_post=True),
    "role_changed": _event(
        "clan",
        "clan_event",
        "new_role",
        "prev_role",
        "direction",
        hard_post=True,
    ),
    "weekly_donation_leader": _event("clan", "clan_event", "week_ending", "leaders"),
    "clan_score_milestone": _event("clan", "clan_event", "milestone", "clan_score"),
    "clan_league_changed": _event("clan", "clan_event", "league", "prev_league", "war_trophies"),
    "clan_birthday": _event("clan", "clan_event", "years", hard_post=True),
    # A tournament is a time-bounded event stream, not its own subsystem (#210).
    # It ends on the clan stream like any other bounded thing, and the awareness
    # loop narrates it — the same path season_closed and member_joined take.
    # hard_post because a finished tournament is a real, dated clan moment that
    # must not be silently skipped.
    "tournament_finished": _event(
        "clan",
        "clan_event",
        "name",
        "participants",
        "podium",
        time_semantics="exact",
        hard_post=True,
    ),
    "member_birthday": _event("clan", "clan_event", "name"),
    "join_anniversary": _event("clan", "clan_event", "name", "months"),
    "cr_account_anniversary": _event("clan", "clan_event", "name", "years"),
    "war_champ_lead_change": _event("war", "clan_event", "season_id", "new_leader"),
    "rookie_mvp_lead_change": _event("war", "clan_event", "season_id", "new_leader"),
    "pol_season_podium": _event(
        "clan",
        "battle_mode",
        "pol_season_id",
        "podium",
        time_semantics="exact",
        hard_post=True,
    ),
    # war stream
    "season_closed": _event("war", "war", "weeks", time_semantics="exact", hard_post=True),
    "week_finished": _event("war", "war", "our_rank", "our_fame", "standings", hard_post=True),
    "season_started": _event("war", "war", "season_id"),
    "colosseum_detected": _event("war", "war", "section_index"),
    "war_day_opened": _event("war", "war", "period_type", "day_index", "war_day_human"),
    "race_finished": _event("war", "war", "finished_at", time_semantics="exact"),
    # game stream
    "card_added": _event("game", "system", "name", time_semantics="exact"),
    "event_started": _event("game", "system", "title", time_semantics="exact"),
    "event_badge_earned": _event("game", "system", "badge_name", time_semantics="exact"),
}

_STREAM_BY_TABLE = {
    "player_events": "player",
    "clan_events": "clan",
    "war_events": "war",
    "game_events": "game",
}


def validate_event(
    *, table: str, event_type: str, payload: dict, observed_at
) -> tuple[EventContract, str]:
    stream = _STREAM_BY_TABLE.get(table)
    if stream is None:
        raise ValueError(f"unsupported event table: {table}")
    contract = EVENT_CONTRACTS.get(event_type)
    if contract is None:
        raise ValueError(f"undeclared event type: {event_type}")
    if contract.stream != stream:
        raise ValueError(f"event {event_type} belongs to {contract.stream}, not {stream}")
    missing = sorted(contract.required_payload - payload.keys())
    if missing:
        raise ValueError(f"event {event_type} missing payload fields: {missing}")
    canonical_at = canonical_utc_timestamp(observed_at)
    if canonical_at is None:
        raise ValueError(f"event {event_type} has invalid observed_at: {observed_at!r}")
    return contract, canonical_at


def hard_post_event_types() -> frozenset[str]:
    return frozenset(
        event_type for event_type, contract in EVENT_CONTRACTS.items() if contract.hard_post
    )


def lane_by_event_type() -> dict[str, str]:
    return {event_type: contract.lane for event_type, contract in EVENT_CONTRACTS.items()}


__all__ = [
    "EVENT_CONTRACTS",
    "EventContract",
    "hard_post_event_types",
    "lane_by_event_type",
    "validate_event",
]
