"""Awareness-loop situation assembler.

Builds the single ``Situation`` payload handed to the awareness agent each
heartbeat tick. The situation collapses what used to be N per-signal context
envelopes into one end-to-end picture: time/phase, standing, all signals
since the last tick grouped by lane, recent channel posts (memory), roster
vitals, and an explicit list of hard-post-floor signals.

The assembler is pure: it takes a heartbeat tick result + clan/war and
queries the local DB for memory and form data. It does no Discord I/O and
no LLM calls — that's the agent's job.
"""

from __future__ import annotations

import logging
from typing import Iterable

import db
import prompts
from heartbeat import build_situation_time
from runtime.channel_subagents import (
    BATTLE_MODE_SIGNAL_TYPES,
    CLAN_EVENT_SIGNAL_TYPES,
    LEADERSHIP_ONLY_SIGNAL_TYPES,
    OPTIONAL_PROGRESSION_SIGNAL_TYPES,
    PROGRESSION_SIGNAL_TYPES,
    TOURNAMENT_SIGNAL_TYPES,
    is_war_signal,
    signal_audience,
    signal_source_key,
)

log = logging.getLogger("elixir")


# Signals that the awareness loop is REQUIRED to address. The agent picks
# tone, channel (within lane rules), and phrasing; existence is non-negotiable.
HARD_POST_SIGNAL_TYPES = frozenset({
    "war_battle_rank_change",
    "war_week_complete",
    "war_season_complete",
    "war_completed",
    "member_join",
    "member_leave",
    "capability_unlock",
})


# Channel allowlist used by post-plan validation. Each channel's lane keys
# describe the signal-family hints (`leads_with`) that may legitimately ship
# there.
CHANNEL_LANES: dict[str, set[str]] = {
    "river-race": {"war"},
    "trophy-road": {"battle_mode"},
    "player-progress": {"milestone"},
    "clan-events": {"clan_event"},
    "leader-lounge": {"war", "leadership", "clan_event"},
    "announcements": {"system"},
}


def classify_signal_lane(signal: dict) -> str:
    """Return the lane key for a signal: war / battle_mode / milestone /
    clan_event / leadership / system / unknown."""
    sig_type = (signal or {}).get("type") or ""
    if is_war_signal(signal):
        return "war"
    if sig_type in BATTLE_MODE_SIGNAL_TYPES:
        return "battle_mode"
    if sig_type in PROGRESSION_SIGNAL_TYPES:
        return "milestone"
    if sig_type in CLAN_EVENT_SIGNAL_TYPES or sig_type in TOURNAMENT_SIGNAL_TYPES:
        return "clan_event"
    if sig_type in LEADERSHIP_ONLY_SIGNAL_TYPES or signal_audience(signal) == "leadership":
        return "leadership"
    if sig_type == "capability_unlock":
        return "system"
    return "unknown"


def _annotate_signal(signal: dict) -> dict:
    """Attach a stable signal_key + lane to each signal so the agent has a
    deterministic identifier to echo back in `covers_signal_keys`."""
    annotated = dict(signal or {})
    if not annotated.get("signal_key"):
        annotated["signal_key"] = signal_source_key(signal)
    annotated["_lane"] = classify_signal_lane(signal)
    return annotated


def _group_signals_by_lane(signals: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for signal in signals or []:
        annotated = _annotate_signal(signal)
        grouped.setdefault(annotated["_lane"], []).append(annotated)
    return grouped


def _hard_post_signals(signals: Iterable[dict]) -> list[dict]:
    out = []
    for signal in signals or []:
        sig_type = signal.get("type") or ""
        if sig_type in HARD_POST_SIGNAL_TYPES:
            out.append({
                "signal_key": signal_source_key(signal),
                "type": sig_type,
                "tag": signal.get("tag"),
                "name": signal.get("name"),
            })
    return out


def _build_standing(war: dict | None) -> dict | None:
    """Compact standing summary — rank, fame, deficit-to-leader, engagement,
    plus rival clan scoreboard so the agent can talk about the race state.

    Ranks by ``periodPoints`` (cumulative war-week score) which is the actual
    competitive metric. The clan-level ``fame`` field in the API is often 0
    for all clans; ``periodPoints`` always reflects the real standings.
    """
    war = war or {}
    clans = war.get("clans") or []
    clan_obj = war.get("clan") or {}
    if not clan_obj.get("tag"):
        return None
    our_tag = clan_obj["tag"].strip("#").upper()
    ranked = sorted(
        clans,
        key=lambda c: (c.get("periodPoints") or 0, c.get("fame") or 0, c.get("clanScore") or 0),
        reverse=True,
    )
    our_points = None
    our_rank = None
    leader_points = (ranked[0].get("periodPoints") or 0) if ranked else 0
    for rank, c in enumerate(ranked, start=1):
        tag = (c.get("tag") or "").strip("#").upper()
        if tag == our_tag:
            our_points = c.get("periodPoints") or 0
            our_rank = rank
            break
    if our_points is None:
        return None
    scoreboard = [
        {"name": c.get("name"), "points": c.get("periodPoints") or 0, "fame": c.get("fame") or 0}
        for c in ranked
    ]
    return {
        "rank": our_rank,
        "points": our_points,
        "leader_points": leader_points,
        "deficit_to_leader": (leader_points - our_points) if (our_rank != 1) else 0,
        "lead_over_second": (our_points - (ranked[1].get("periodPoints") or 0)) if (our_rank == 1 and len(ranked) > 1) else 0,
        "field_size": len(ranked),
        "scoreboard": scoreboard,
    }


def _clan_phase_block() -> dict | None:
    """Clan age + phase classification (founding/establishing/established/
    mature). Always included so the awareness agent can frame posts against
    the clan's actual age rather than fall back to time-frozen prose.
    """
    try:
        return prompts.clan_phase()
    except Exception:
        log.warning("clan_phase_block load failed", exc_info=True)
        return None


def _season_awards_block() -> dict | None:
    """Current-season standings for War Champ / Iron King / Donation Champ /
    Rookie MVP, in the same shape as the season_awards_granted signal payload.

    Always included so the awareness agent can answer 'who's leading?' or
    'is anyone on track for Iron King?' without re-deriving from raw fame
    or donation rows.
    """
    try:
        return db.get_season_awards_standings()
    except Exception:
        log.warning("season_awards_block load failed", exc_info=True)
        return None


def _channel_memory_for(subagent_key: str, *, recent_limit: int = 5) -> dict:
    """Pull recent assistant posts for one channel so the agent knows what it
    has already said. Pure DB read, no Discord call."""
    try:
        config = prompts.discord_singleton_subagent(subagent_key)
    except (ValueError, KeyError):
        return {"recent_posts": []}
    try:
        recent = db.list_channel_messages(config["id"], recent_limit, "assistant") or []
    except Exception:
        log.warning("channel_memory load failed for %s", subagent_key, exc_info=True)
        recent = []
    return {
        "channel_id": config["id"],
        "recent_posts": [
            {
                "summary": row.get("summary"),
                "recorded_at": row.get("recorded_at"),
                "event_type": row.get("event_type"),
            }
            for row in recent
        ],
    }


def _roster_vitals(limit: int = 20) -> list[dict]:
    """Compact roster anchor: hot-streak members + clan summary snippet.

    Read-only scouting input. Not for verbatim posting — the agent uses it to
    decide whether anyone is doing something noteworthy this tick.
    """
    out: list[dict] = []
    try:
        hot = db.get_members_on_hot_streak() or []
        for entry in hot[:limit]:
            out.append({
                "kind": "hot_streak",
                "tag": entry.get("tag"),
                "name": entry.get("name") or entry.get("current_name"),
                "streak": entry.get("current_streak"),
            })
    except Exception:
        log.warning("roster_vitals: hot streak load failed", exc_info=True)
    return out


def _due_revisits(limit: int = 20) -> list[dict]:
    """Pending revisits whose ``due_at`` has passed. Agent-facing fields only —
    ``revisit_id`` / ``created_by_workflow`` are runtime bookkeeping.
    """
    try:
        import db
        rows = db.list_due_revisits(limit=limit)
    except Exception:
        log.warning("due_revisits lookup failed", exc_info=True)
        return []
    return [
        {
            "signal_key": row.get("signal_key"),
            "due_at": row.get("due_at"),
            "rationale": row.get("rationale"),
            "scheduled_at": row.get("created_at"),
        }
        for row in rows
    ]


def _recent_agent_writes(limit: int = 10) -> list[dict]:
    """Compact view of recent leadership-scope memories Elixir has written.

    Fed into the Situation so the awareness agent can see what watches /
    followups / observations it has already recorded and avoid duplicating
    them. Filters to memories authored by the awareness or synthesis loops
    (``elixir_inference`` / ``elixir_synthesis`` source types) to keep the
    view tight; excludes human-authored leader notes which are a different
    channel.
    """
    try:
        from memory_store import list_memories
        # Fetch a broader set then filter to elixir-authored in Python since
        # the filter API only supports a single source_type at a time.
        memories = list_memories(viewer_scope="leadership", limit=limit * 3)
    except Exception:
        return []
    out = []
    for m in memories:
        if m.get("source_type") not in {"elixir_inference", "elixir_synthesis"}:
            continue
        out.append({
            "memory_id": m.get("memory_id"),
            "title": m.get("title"),
            "tags": m.get("tags") or [],
            "member_tag": m.get("member_tag"),
            "created_at": m.get("created_at"),
        })
        if len(out) >= limit:
            break
    return out


def _already_delivered(signal: dict) -> bool:
    """True iff the signal's log key is already in ``signal_log``.

    Belt-and-suspenders: each detector self-checks before emitting, but if a
    detector-level check is missed (restart, cursor reset, concurrent tick),
    this filter drops the signal before the agent can re-cover it. Safer than
    relying on the agent to read channel_memory and self-skip.
    """
    log_type = (signal or {}).get("signal_log_type")
    if not log_type:
        return False
    try:
        return db.was_signal_sent_any_date(log_type)
    except Exception:
        log.warning("_already_delivered lookup failed for %s", log_type, exc_info=True)
        return False


def build_situation(tick_result, *, channel_keys: Iterable[str] | None = None) -> dict:
    """Assemble the single Situation payload for one awareness tick.

    ``tick_result`` is a ``HeartbeatTickResult`` (signals + clan + war).
    Returns a dict whose top-level keys are stable for the agent's prompt:
    ``time``, ``standing``, ``signals_by_lane``, ``hard_post_signals``,
    ``channel_memory``, ``roster_vitals``, ``due_revisits``,
    ``recent_agent_writes``.

    Signals whose ``signal_log_type`` is already in ``signal_log`` are dropped
    before assembly — preventing the agent from re-covering a signal that was
    already announced.
    """
    all_signals = list(getattr(tick_result, "signals", None) or [])
    signals = [s for s in all_signals if not _already_delivered(s)]
    dropped = len(all_signals) - len(signals)
    if dropped:
        log.info("build_situation: dropped %d already-delivered signal(s)", dropped)
    clan = getattr(tick_result, "clan", None) or {}
    war = getattr(tick_result, "war", None) or {}

    if channel_keys is None:
        channel_keys = list(CHANNEL_LANES.keys())

    # Signals whose only presence is "optional progression" (badge milestones,
    # etc.) should not force an LLM call — the agent almost always skips them.
    noisy_signal_count = sum(
        1 for sig in signals
        if sig.get("type") not in OPTIONAL_PROGRESSION_SIGNAL_TYPES
    )

    due_revisits = _due_revisits()

    return {
        "time": build_situation_time(),
        "standing": _build_standing(war),
        "season_awards": _season_awards_block(),
        "clan_phase": _clan_phase_block(),
        "signals_by_lane": _group_signals_by_lane(signals),
        "hard_post_signals": _hard_post_signals(signals),
        "due_revisits": due_revisits,
        "channel_memory": {
            key: _channel_memory_for(key) for key in channel_keys
        },
        "roster_vitals": _roster_vitals(),
        "recent_agent_writes": _recent_agent_writes(),
        "_raw_signal_count": len(signals),
        "_noisy_signal_count": noisy_signal_count,
        "_due_revisit_count": len(due_revisits),
        "_clan_tag": (clan.get("tag") or "").strip(),
    }


def situation_is_quiet(situation: dict) -> bool:
    """Fast-path: should the awareness agent call be skipped entirely?

    Quiet means: no *noisy* signals (optional-progression signals like badge
    milestones don't count — the agent almost always skips them), no hard-post
    floors, and no time-boundary pressure (>1h from any battle-day deadline).
    Mirrors the existing ``_clan_awareness_tick`` early-return so quiet ticks
    keep costing nothing.
    """
    # Fall back to _raw_signal_count for older callers that didn't annotate
    # a situation with _noisy_signal_count (tests, legacy payloads).
    noisy = situation.get("_noisy_signal_count")
    if noisy is None:
        noisy = situation.get("_raw_signal_count") or 0
    if noisy:
        return False
    if situation.get("hard_post_signals"):
        return False
    # A pending revisit is something Elixir told itself to look at — wake the
    # agent even if the raw signal list is empty.
    if situation.get("due_revisits"):
        return False
    time_block = situation.get("time") or {}
    hours_remaining = time_block.get("hours_remaining_in_day")
    # Within an hour of a battle-day deadline → not quiet, agent should look.
    if (
        time_block.get("phase") == "battle"
        and hours_remaining is not None
        and hours_remaining <= 1
    ):
        return False
    return True


__all__ = [
    "CHANNEL_LANES",
    "HARD_POST_SIGNAL_TYPES",
    "build_situation",
    "classify_signal_lane",
    "situation_is_quiet",
]
