"""The scoped responder — one wake, one author, minutes not hours.

Agentic Loop v2, Phase 1 (docs/plans/agentic-loop.md). A wake fires; this builds
a small, precise context for exactly what happened, runs one chassis turn, and
hands the staged posts to the SAME delivery path the awareness brain uses.

Why this is not a second author, which is the failure that killed v4: a wake's
posts are composed in ONE turn from ONE set of facts, including the in-game
clan-chat sibling, and they are delivered through
``runtime.awareness.deliver.deliver_posts`` — the single path that owns the
hard-post floor, the durable outbox, idempotency, and copy policy. Nothing here
sends a message.

The floor is the safety property worth stating plainly. A wake carries the
signal keys it MUST cover. After the turn, coverage is checked against what
actually reached the outbox, never against what the model said it did. An
uncovered floor fails the wake, the cursors do not advance, and the daily
deliberation inherits the signals — the same guarantee the brain has always had,
enforced at a new call site.

The escalation ladder means a wake can only ever fail *upward*: the cheap model
first, the strong model if the cheap one produced nothing usable, and the daily
brain if both fell short. A wake never degrades into silence.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from agent import chassis
from engine.event_contracts import hard_post_event_types

log = logging.getLogger("elixir")

HARD_POST_EVENT_TYPES = hard_post_event_types()

# Jobs by wake class + event type. A wake with no job cannot be responded to and
# falls through to the daily brain — which is the correct default for an event
# type nobody has written a job for yet.
JOB_BY_EVENT_TYPE = {
    "member_joined": "welcome",
}

# The escalation ladder. Same attention, stronger composer.
_LADDER = {"lightweight": "wake_response", "chat": "wake_response_chat"}


def responder_enabled() -> bool:
    """Phase 1 ships OFF. The wake evaluator keeps shadowing either way."""
    return os.getenv("ELIXIR_WAKE_RESPONDER", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def job_for(events: list[dict]) -> str | None:
    """The job that covers this wake, or None if no job claims it.

    A wake whose events map to more than one job is NOT split into two turns —
    it falls through to the daily brain. Two composers for one moment is exactly
    the divergence this design exists to avoid, and a mixed batch is rare enough
    that paying a brain tick for it is the right trade.
    """
    if not events:
        return None
    jobs = {JOB_BY_EVENT_TYPE.get(e.get("event_type")) for e in events}
    # An UNMAPPED event in the batch is disqualifying, not ignorable. Dropping
    # the None here would hand the wake to the one job that matched while the
    # unmapped event rode along uncovered — and if it were a hard post, the
    # floor would fail the whole wake after paying for the turn.
    if len(jobs) != 1 or None in jobs:
        return None
    return jobs.pop()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_seed(events: list[dict], conn) -> dict:
    """The scoped read: this wake's events, resolved just enough to compose.

    Deliberately small. The point of the whole architecture is that a welcome
    does not need the clan's full situation — it needs THIS member. Everything
    else the turn wants, it fetches with a tool.

    What IS precomputed here is what the 2026-08-04 experiment showed models get
    wrong when left to infer it: whether a joiner is actually returning, and the
    live join floor (a remembered value once told a member who joined at 7,053
    that they were clear of a "2,000-trophy entry line" when the floor was
    7,000).
    """
    import prompts

    seed_events: list[dict] = []
    for event in events:
        entry = {
            "signal_key": event.get("signal_key"),
            "event_type": event.get("event_type"),
            "observed_at": event.get("observed_at"),
            "subject_tag": event.get("subject_tag"),
        }
        payload = event.get("payload")
        if isinstance(payload, dict):
            entry["payload"] = payload
        tag = event.get("subject_tag")
        if tag:
            entry["member"] = _member_facts(conn, tag)
        seed_events.append(entry)

    return {
        "wake": "hard_post" if any(_is_floor(e) for e in events) else "signal",
        "now": _utcnow(),
        "events": seed_events,
        "clan": {
            "name": "POAP KINGS",
            # The live floor, never a remembered one. CLAN.md substitutes the
            # current value; quoting a stale number told a member who joined at
            # 7,053 they were "well clear of our 2,000-trophy entry line".
            "required_trophies": prompts._live_required_trophies(),
        },
    }


def _is_floor(event: dict) -> bool:
    return (event.get("event_type") or "") in HARD_POST_EVENT_TYPES


def _member_facts(conn, tag: str) -> dict:
    """Identity plus the one inference a welcome must not get wrong."""
    facts: dict = {"player_tag": tag}
    row = conn.execute(
        "SELECT COALESCE(display_name, current_name) AS name, first_seen_at "
        "FROM players WHERE player_tag = ?",
        (tag,),
    ).fetchone()
    if row:
        facts["name"] = row["name"]
        facts["first_seen_at"] = row["first_seen_at"]
    stints = conn.execute(
        "SELECT joined_at, left_at FROM clan_memberships WHERE player_tag = ? ORDER BY joined_at",
        (tag,),
    ).fetchall()
    facts["clan_stints"] = [dict(r) for r in stints]
    # The distinction a welcome turns on. Left to infer from a stint list, a
    # model can and does read a returning member as brand new.
    facts["is_returning"] = len([s for s in stints if s["left_at"]]) > 0
    return facts


def floor_keys(events: list[dict]) -> frozenset:
    return frozenset(
        str(e.get("signal_key")) for e in events if _is_floor(e) and e.get("signal_key")
    )


def respond(
    wake: dict,
    *,
    deliver_fn,
    conn=None,
    on_event=None,
) -> dict:
    """Compose and deliver one wake. Returns an outcome dict.

    ``deliver_fn(read, plan) -> dict`` is the caller's binding of
    ``deliver_posts`` — the same callable the awareness loop uses, so the
    responder cannot invent a delivery path of its own.
    """
    import db

    events = wake.get("events") or []
    job = job_for(events)
    if job is None:
        return {"handled": False, "reason": "no job claims this wake"}

    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        seed = build_seed(events, conn)
    finally:
        if owns_conn:
            conn.close()

    floor = floor_keys(events)
    lanes = ("announcements",)
    surfaces = {chassis.SURFACE_DISCORD_ANNOUNCEMENTS, chassis.SURFACE_CLAN_CHAT}

    attempts = []
    tiers = ["lightweight"]
    if wake.get("wake_model") == "chat":
        tiers = ["chat"]
    elif os.getenv("ELIXIR_WAKE_ESCALATE", "1").strip().lower() not in ("0", "false", "no"):
        tiers.append("chat")

    for tier in tiers:
        attention = chassis.Attention(
            job=job,
            trigger={
                "wake_class": wake.get("wake_class"),
                "reason": wake.get("reason"),
                "signal_keys": [e.get("signal_key") for e in events],
            },
            scope=chassis.Scope(
                member_tags=tuple(e["subject_tag"] for e in events if e.get("subject_tag")),
                lanes=lanes,
                signal_keys=tuple(str(e.get("signal_key")) for e in events if e.get("signal_key")),
            ),
            surfaces=frozenset(surfaces),
            budget=chassis.Budget(model_family=tier, max_tokens=2000),
            floor=floor,
            workflow=_LADDER[tier],
        )
        episode = chassis.run_turn(attention, seed, on_event=on_event)
        attempts.append(episode)

        posts = episode.get("posts") or []
        covered = {k for post in posts for k in post.get("covers_signal_keys") or []}
        uncovered = sorted(floor - covered)
        if not posts:
            log.warning(
                "wake responder: %s tier produced no post (job=%s, rejections=%s)",
                tier,
                job,
                episode.get("rejections"),
            )
            continue
        if uncovered:
            log.warning("wake responder: %s tier left floor signals uncovered: %s", tier, uncovered)
            continue

        result = _deliver(episode, seed, floor, deliver_fn)
        result["episode"] = episode
        result["attempts"] = len(attempts)
        result["tier"] = tier
        if not result.get("failed"):
            return {"handled": True, **result}
        log.warning("wake responder: delivery failed on %s tier: %s", tier, result.get("reason"))

    return {
        "handled": False,
        "reason": "no tier produced a deliverable post; leaving for the daily deliberation",
        "attempts": len(attempts),
        "episodes": attempts,
    }


def _deliver(episode: dict, seed: dict, floor: frozenset, deliver_fn) -> dict:
    """Hand the staged posts to the one delivery path.

    The ``read`` handed to ``deliver_posts`` carries this wake's floor as
    ``hard_post_signals`` — that is what makes the floor check inside
    ``deliver_posts`` verify THIS wake's obligations rather than a whole tick's.
    """
    read = {
        "hard_post_signals": [
            {
                "signal_key": event.get("signal_key"),
                "event_type": event.get("event_type"),
                "subject_tag": event.get("subject_tag"),
            }
            for event in seed.get("events") or []
            if event.get("signal_key") in floor
        ],
        "signals_by_category": {},
        "recent_member_spotlights": [],
    }
    plan = {"posts": [dict(post) for post in episode.get("posts") or []]}
    try:
        return deliver_fn(read, plan)
    except Exception as exc:
        log.exception("wake responder: delivery raised")
        return {"delivered": 0, "failed": True, "reason": f"delivery raised: {exc}"}


def record_episode(episode: dict, outcome: dict) -> None:
    """Persist what this wake thought and did.

    The substrate the nightly reflection reads. Lives in the telemetry database
    for now — Phase 1 is deliberately migration-free, and an episode is
    observation, not clan state.
    """
    try:
        from storage import telemetry

        # The table is declared in storage/telemetry._SCHEMA, not here: schema
        # lives in one place per database, and an inline CREATE is how two
        # definitions of the same table start to drift.
        conn = telemetry.connect()
        conn.execute(
            "INSERT INTO wake_episodes (recorded_at, job, workflow, tier, handled, "
            "delivered, reason, episode_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utcnow(),
                episode.get("job"),
                episode.get("workflow"),
                outcome.get("tier"),
                1 if outcome.get("handled") else 0,
                int(outcome.get("delivered") or 0),
                str(outcome.get("reason") or "")[:400],
                json.dumps(episode, default=str)[:200000],
            ),
        )
        conn.commit()
    except Exception:
        log.debug("wake responder: episode record failed", exc_info=True)


__all__ = [
    "JOB_BY_EVENT_TYPE",
    "build_seed",
    "floor_keys",
    "job_for",
    "record_episode",
    "respond",
    "responder_enabled",
]
