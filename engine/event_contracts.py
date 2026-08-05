"""Canonical vocabulary and routing metadata for durable domain events.

Emitters own detection; this registry owns what may cross the event-stream
boundary.  A new event type is therefore incomplete until its stream, payload
floor, time semantics, lane, hard-post policy, **and wake policy** are declared
together.

``wake`` answers *when does this event deserve cognition?* and is deliberately
orthogonal to ``hard_post``, which answers *must this event eventually be
covered?*  A hard-post with ``wake="digest"`` is still guaranteed a post — it
just waits for the daily deliberation.  A ``wake="immediate"`` soft event gets
prompt attention with no coverage guarantee at all.

  - ``immediate`` — fire a scoped responder turn on the next engine tick.
  - ``batch``     — coalesce with its class for up to ``BATCH_WINDOW_MINUTES``
                    so three badges become one post, then fire.
  - ``digest``    — no wake; the daily deliberation reads it with everything
                    else.  This is the default: most event types are texture,
                    not news.
  - ``never``     — excluded from wake consideration entirely.

``wake_model`` picks the composer tier for the wake this event lands in
(``lightweight`` = Haiku, ``chat`` = Sonnet).  The 2026-08-04 scoped-composer
experiment is the evidence for the split: roster moments (join / leave / role)
are Haiku-safe behind the delivery validator, while war and season narrative —
streak math, league semantics, multi-signal batches — measurably needs Sonnet.

Changing when Elixir speaks about an event is therefore a **one-line data
change here**, never a code path in the responder.  See
docs/plans/agentic-loop.md; a per-event-type branch in the responder is the
tell that we are rebuilding v4's ``runtime/signals/delivery.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.normalize import canonical_utc_timestamp

WAKE_CLASSES = frozenset({"immediate", "batch", "digest", "never"})
WAKE_MODELS = frozenset({"lightweight", "chat"})

# Both halves of the badge split. Every reader that asks "is this a badge event?"
# must ask it through this, because historical rows (pre 2026-08-04) carry
# `badge_earned` regardless of tier — a reader that hardcodes one name silently
# drops either the new Legendary events or the entire back catalogue.
BADGE_EVENT_TYPES = frozenset({"badge_earned", "legendary_badge_earned"})

# All three halves of the ranked-promotion split. Same rule as the badges: a
# reader that hardcodes one name silently drops either the new Champion-tier
# events or the entire pre-split back catalogue, where every promotion — up to
# and including Ultimate Champion — arrived as `pol_promotion`.
RANKED_PROMOTION_EVENT_TYPES = frozenset(
    {"pol_promotion", "champion_league_reached", "ultimate_champion_reached"}
)


@dataclass(frozen=True)
class EventContract:
    stream: str
    lane: str
    required_payload: frozenset[str] = frozenset()
    time_semantics: str = "observed_between_polls"
    hard_post: bool = False
    wake: str = "digest"
    wake_model: str = "lightweight"


def _event(
    stream: str,
    lane: str,
    *required_payload: str,
    time_semantics: str = "observed_between_polls",
    hard_post: bool = False,
    wake: str = "digest",
    wake_model: str = "lightweight",
) -> EventContract:
    if wake not in WAKE_CLASSES:
        raise ValueError(f"unknown wake class: {wake!r}")
    if wake_model not in WAKE_MODELS:
        raise ValueError(f"unknown wake model: {wake_model!r}")
    return EventContract(
        stream=stream,
        lane=lane,
        required_payload=frozenset(required_payload),
        time_semantics=time_semantics,
        hard_post=hard_post,
        wake=wake,
        wake_model=wake_model,
    )


EVENT_CONTRACTS: dict[str, EventContract] = {
    # player stream
    "career_wins_milestone": _event("player", "milestone", "milestone", "wins"),
    "best_trophies_peak": _event("player", "milestone", "boundary", "best_trophies"),
    # Card Mastery grind. ~40 of the clan's 102 badges in 20 days, and the brain
    # posted about none of them — routing it to digest is what the brain already
    # decides, made cheap. `legendary_badge_earned` is the notable sibling.
    "badge_earned": _event("player", "milestone", "badge_name", "badge_tier"),
    # A one-off (level-less) badge: awarded once, never again. Rare enough to be
    # worth saying the day it happens (4 in 20 days), which is why it is its own
    # type rather than a payload check inside badge_earned.
    "legendary_badge_earned": _event(
        "player", "milestone", "badge_name", "badge_tier", wake="immediate"
    ),
    "arena_changed": _event("player", "milestone", "arena_id", "arena_name", wake="batch"),
    "card_unlocked": _event("player", "milestone", "card_id", "card_name", "rarity"),
    "card_level_milestone": _event("player", "milestone", "card_id", "card_name", "milestone"),
    "collection_level_milestone": _event("player", "milestone", "milestone", "collection_level"),
    # The ranked ladder splits three ways on the destination tier, because the
    # clan does not treat a Master-tier bump and a Champion-tier arrival as the
    # same news: 20% of promotions into leagues 1-3 reached a post, against 60%
    # into 4-6 and 100% into 7. League 4 is where the game renames the tier to
    # "Champion", so the boundary is the game's own.
    "pol_promotion": _event("player", "battle_mode", "league", "prev_league", "league_tier"),
    "champion_league_reached": _event(
        "player", "battle_mode", "league", "prev_league", "league_tier", wake="immediate"
    ),
    "ultimate_champion_reached": _event(
        "player", "battle_mode", "league", "league_tier", wake="immediate"
    ),
    "pol_global_rank_attained": _event("player", "battle_mode", "from_rank", "to_rank", "league"),
    "pol_season_closed": _event("player", "battle_mode", "pol_season_id", time_semantics="exact"),
    # clan stream
    # The join is the canonical immediate wake: the newcomer is in the app NOW,
    # and this is the case the retired join trigger already proved.
    "member_joined": _event("clan", "clan_event", "name", hard_post=True, wake="immediate"),
    "member_left": _event("clan", "clan_event", "name"),
    "member_left_verified": _event("clan", "clan_event", "name", hard_post=True, wake="immediate"),
    "role_changed": _event(
        "clan",
        "clan_event",
        "name",
        "new_role",
        "prev_role",
        "direction",
        hard_post=True,
        wake="immediate",
    ),
    "weekly_donation_leader": _event("clan", "clan_event", "week_ending", "leaders"),
    "clan_score_milestone": _event("clan", "clan_event", "milestone", "clan_score"),
    # Rides along with the season-close narrative rather than earning its own
    # post: on 2026-08-03 the brain folded it into the season recap, and a
    # standalone league-change post would read as a demotion notice.
    "clan_league_changed": _event(
        "clan",
        "clan_event",
        "league",
        "prev_league",
        "war_trophies",
        wake="immediate",
        wake_model="chat",
    ),
    "clan_birthday": _event(
        "clan", "clan_event", "years", hard_post=True, wake="immediate", wake_model="chat"
    ),
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
        wake="immediate",
        wake_model="chat",
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
        wake="immediate",
        wake_model="chat",
    ),
    # war stream — the narrative tier. Streak math, league semantics and
    # multi-signal batches are exactly where the scoped-composer experiment
    # measured Haiku slipping (wrong award name, week off-by-one).
    "season_closed": _event(
        "war",
        "war",
        "weeks",
        time_semantics="exact",
        hard_post=True,
        wake="immediate",
        wake_model="chat",
    ),
    "week_finished": _event(
        "war",
        "war",
        "our_rank",
        "our_fame",
        "standings",
        hard_post=True,
        wake="immediate",
        wake_model="chat",
    ),
    "season_started": _event("war", "war", "season_id"),
    "colosseum_detected": _event("war", "war", "section_index"),
    "war_day_opened": _event("war", "war", "period_type", "day_index", "war_day_human"),
    "race_finished": _event("war", "war", "finished_at", time_semantics="exact"),
    # game stream — system texture. card_added in particular flooded channels
    # on backfill (see the brain data-source audit); it never wakes anything.
    "card_added": _event("game", "system", "name", time_semantics="exact", wake="never"),
    "event_started": _event("game", "system", "title", time_semantics="exact", wake="never"),
    "event_badge_earned": _event(
        "game", "system", "badge_name", time_semantics="exact", wake="never"
    ),
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


def wake_policy(event_type: str) -> tuple[str, str]:
    """``(wake_class, wake_model)`` for an event type.

    An unknown event type is ``("digest", "lightweight")``: something the
    registry has not declared must never wake the clan's voice on its own, but
    the daily deliberation still sees it.
    """
    contract = EVENT_CONTRACTS.get(event_type)
    if contract is None:
        return "digest", "lightweight"
    return contract.wake, contract.wake_model


def wake_event_types(wake_class: str) -> frozenset[str]:
    return frozenset(
        event_type
        for event_type, contract in EVENT_CONTRACTS.items()
        if contract.wake == wake_class
    )


__all__ = [
    "BADGE_EVENT_TYPES",
    "EVENT_CONTRACTS",
    "RANKED_PROMOTION_EVENT_TYPES",
    "WAKE_CLASSES",
    "WAKE_MODELS",
    "EventContract",
    "hard_post_event_types",
    "lane_by_event_type",
    "validate_event",
    "wake_event_types",
    "wake_policy",
]
