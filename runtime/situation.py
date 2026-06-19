"""Awareness-loop situation assembler.

Builds the single ``Situation`` payload handed to the awareness agent each
heartbeat tick. The situation collapses what used to be N per-signal context
envelopes into one end-to-end picture: time/phase, standing, all signals
since the last tick grouped by lane, recent channel posts (memory), roster
vitals, and an explicit list of hard-post-floor signals.

The assembler is pure: it takes a heartbeat tick result + clan/war and
queries the local DB for memory, event history, durable projects, and form
data. It does no Discord I/O and no LLM calls — that's the agent's job.
"""

from __future__ import annotations

import logging
from typing import Iterable

import db
import prompts
from storage.event_stream import EVENT_STREAM_WINDOWS
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
    "member-highlights": {"battle_mode", "milestone"},
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


# Names of situation blocks whose loader raised during the current
# build_situation call. Reset at the top of build_situation; surfaced in the
# returned dict as ``_degraded_blocks`` so degradation shows up in tick logs
# instead of silently shrinking the agent's view of the clan.
_degraded_blocks: list[str] = []


def _note_degraded(block: str) -> None:
    _degraded_blocks.append(block)


def _clan_phase_block() -> dict | None:
    """Clan age + phase classification (founding/establishing/established/
    mature). Always included so the awareness agent can frame posts against
    the clan's actual age rather than fall back to time-frozen prose.
    """
    try:
        return prompts.clan_phase()
    except Exception:
        log.warning("clan_phase_block load failed", exc_info=True)
        _note_degraded("clan_phase")
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
        _note_degraded("season_awards")
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
        _note_degraded(f"channel_memory:{subagent_key}")
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
        _note_degraded("roster_vitals")
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
        _note_degraded("due_revisits")
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
        log.warning("recent_agent_writes load failed", exc_info=True)
        _note_degraded("recent_agent_writes")
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


def _leader_action_board() -> dict:
    """Compact view of the arena-relay action board for the agent.

    Open cards mean the leader has an undecided ask about that member or
    topic — the agent should not duplicate it in a post. Recent decisions
    are the leader's latest judgments — the agent should not contradict or
    re-litigate them.
    """
    try:
        return db.leader_action_board_snapshot()
    except Exception:
        log.warning("leader_action_board load failed", exc_info=True)
        _note_degraded("leader_action_board")
        return {"open": [], "recent_decisions": []}


def _merge_count_maps(*maps: dict | None) -> dict:
    merged: dict[str, int] = {}
    for mapping in maps:
        for key, value in (mapping or {}).items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _merge_event_window_summaries(*summaries: dict | None) -> dict:
    merged: dict[str, dict] = {}
    for days in EVENT_STREAM_WINDOWS:
        key = f"{days}d"
        blocks = [(summary or {}).get(key) or {} for summary in summaries]
        merged[key] = {
            "days": days,
            "total": sum(int(block.get("total") or 0) for block in blocks),
            "by_type": _merge_count_maps(*(block.get("by_type") for block in blocks)),
            "by_scope": _merge_count_maps(*(block.get("by_scope") for block in blocks)),
        }
    return merged


def _compact_event(row: dict) -> dict:
    """Return prompt-safe event metadata without raw payload_json."""
    return {
        "event_key": row.get("event_key"),
        "event_type": row.get("event_type"),
        "observed_at": row.get("observed_at"),
        "scope": row.get("scope"),
        "source_system": row.get("source_system"),
        "source_detector": row.get("source_detector"),
        "source_signal_key": row.get("source_signal_key"),
        "subject_type": row.get("subject_type"),
        "subject_key": row.get("subject_key"),
        "season_id": row.get("season_id"),
        "war_week": row.get("war_week"),
    }


def _recent_events_block(*, include_leadership: bool, recent_limit: int = 20) -> dict:
    """Compact event-stream context for awareness.

    The raw 90-day stream remains queryable through tools/admin scripts. The
    prompt only gets aggregate windows plus a small recent-pulse list.
    """
    scopes = ["public", "leadership"] if include_leadership else ["public"]
    try:
        summaries = [
            db.summarize_events_by_window(windows=EVENT_STREAM_WINDOWS, scope=scope)
            for scope in scopes
        ]
        recent_rows: list[dict] = []
        for scope in scopes:
            recent_rows.extend(
                db.list_recent_events(days=7, scope=scope, limit=recent_limit)
            )
        recent_rows.sort(
            key=lambda row: (row.get("observed_at") or "", row.get("event_id") or 0),
            reverse=True,
        )
        return {
            "window_days": list(EVENT_STREAM_WINDOWS),
            "scope_filter": "public+leadership" if include_leadership else "public",
            "summaries": _merge_event_window_summaries(*summaries),
            "recent": [_compact_event(row) for row in recent_rows[:recent_limit]],
        }
    except Exception:
        log.warning("recent_events load failed", exc_info=True)
        _note_degraded("recent_events")
        return {
            "window_days": list(EVENT_STREAM_WINDOWS),
            "scope_filter": "public+leadership" if include_leadership else "public",
            "summaries": {},
            "recent": [],
        }


def _projects_block() -> dict:
    try:
        projects = {
            "war_season": db.get_active_war_season_project_snapshot(),
        }
        projects.update(db.get_active_operating_project_snapshots() or {})
        return projects
    except Exception:
        log.warning("projects load failed", exc_info=True)
        _note_degraded("projects")
        return {
            "war_season": None,
            "clan_development": None,
            "onboarding": None,
            "recruitment": None,
        }


def _decision_cases_block() -> dict:
    try:
        return db.decision_case_snapshot()
    except Exception:
        log.warning("decision cases load failed", exc_info=True)
        _note_degraded("decision_cases")
        return {"due": [], "open": []}


def _already_delivered(signal: dict) -> bool:
    """True iff the signal's completion key is already in ``signal_log``.

    ``signal_log`` is a delivery-completion marker, not the observation
    ledger. Awareness records signals into ``game_event_stream`` before this
    filter runs, then drops completed signals before the agent can re-cover
    them. That preserves observability while retaining duplicate-post
    protection if a detector-level check is missed.

    On lookup error the signal is treated as delivered (suppressed): a missed
    announcement is recoverable on a later tick, while a duplicate post to the
    clan is the failure this filter exists to prevent. The suppression is
    logged at error level and surfaced via ``_degraded_blocks``.
    """
    log_type = (signal or {}).get("signal_log_type")
    if not log_type:
        return False
    try:
        return db.was_signal_completed_any_date(log_type)
    except Exception:
        log.error("_already_delivered lookup failed for %s; suppressing signal to avoid double-post", log_type, exc_info=True)
        _note_degraded(f"already_delivered:{log_type}")
        return True


def build_situation(
    tick_result,
    *,
    channel_keys: Iterable[str] | None = None,
    include_leadership_events: bool | None = None,
) -> dict:
    """Assemble the single Situation payload for one awareness tick.

    ``tick_result`` is a ``HeartbeatTickResult`` (signals + clan + war).
    Returns a dict whose top-level keys are stable for the agent's prompt:
    ``time``, ``standing``, ``signals_by_lane``, ``hard_post_signals``,
    ``channel_memory``, ``recent_events``, ``projects``, ``decision_cases``,
    ``roster_vitals``, ``due_revisits``, ``recent_agent_writes``.

    Signals whose ``signal_log_type`` is already marked complete in
    ``signal_log`` are dropped before assembly — preventing the agent from
    re-covering a signal that was already announced. In the awareness delivery
    path, event-stream recording happens before this prefilter.
    """
    del _degraded_blocks[:]
    all_signals = list(getattr(tick_result, "signals", None) or [])
    signals = [s for s in all_signals if not _already_delivered(s)]
    dropped = len(all_signals) - len(signals)
    if dropped:
        log.info("build_situation: dropped %d already-delivered signal(s)", dropped)
    clan = getattr(tick_result, "clan", None) or {}
    war = getattr(tick_result, "war", None) or {}

    if channel_keys is None:
        channel_keys = list(CHANNEL_LANES.keys())
    else:
        channel_keys = list(channel_keys)

    if include_leadership_events is None:
        include_leadership_events = "leader-lounge" in set(channel_keys)

    # Signals whose only presence is "optional progression" (badge milestones,
    # etc.) should not force an LLM call — the agent almost always skips them.
    noisy_signal_count = sum(
        1 for sig in signals
        if sig.get("type") not in OPTIONAL_PROGRESSION_SIGNAL_TYPES
    )

    due_revisits = _due_revisits()

    situation = {
        "time": build_situation_time(),
        "standing": _build_standing(war),
        "season_awards": _season_awards_block(),
        "clan_phase": _clan_phase_block(),
        "signals_by_lane": _group_signals_by_lane(signals),
        "hard_post_signals": _hard_post_signals(signals),
        "due_revisits": due_revisits,
        "recent_events": _recent_events_block(
            include_leadership=bool(include_leadership_events),
        ),
        "projects": _projects_block(),
        "decision_cases": _decision_cases_block(),
        "channel_memory": {
            key: _channel_memory_for(key) for key in channel_keys
        },
        "roster_vitals": _roster_vitals(),
        "recent_agent_writes": _recent_agent_writes(),
        "leader_action_board": _leader_action_board(),
        "_raw_signal_count": len(signals),
        "_noisy_signal_count": noisy_signal_count,
        "_due_revisit_count": len(due_revisits),
        "_clan_tag": (clan.get("tag") or "").strip(),
    }
    if _degraded_blocks:
        # One consolidated error line so several blocks failing in one tick
        # reads as systemic degradation, not scattered warnings.
        log.error(
            "build_situation: %d block(s) degraded this tick: %s",
            len(_degraded_blocks), ", ".join(_degraded_blocks),
        )
    situation["_degraded_blocks"] = list(_degraded_blocks)
    return situation


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
    decision_cases = situation.get("decision_cases") or {}
    if decision_cases.get("due"):
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
