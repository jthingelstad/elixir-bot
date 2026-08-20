"""The wake evaluator — *what deserves cognition right now?*

Agentic Loop v2, Phase 0 (docs/plans/agentic-loop.md). This runs at the end of
every engine tick and answers one deterministic, $0 question: given the events
the awareness brain has not consumed yet, which of them deserve a scoped
responder turn *now*, which should coalesce for a while, and which can wait for
the daily deliberation.

It is the generalization of the join trigger, which has been
answering exactly this question for ``member_joined`` since the cadence was
widened to 4×/day. Every property that made the join trigger safe to run every
ten minutes is inherited here, deliberately:

- **Cursor-driven.** Pending means *past the awareness event cursor*, not "the
  emitter told us this tick". A wake consumes what is past the cursor and
  nothing else, so extra evaluation cannot double-post; that is a property of
  the design, not of this module. It is also self-healing across restarts —
  the situation is re-derived from durable state every tick.
- **Per-class high-water marks.** A class fires for a given event once and
  never again, even if the responder run that followed failed. Without that, a
  wake whose turn trips a policy would re-fire every ten minutes forever. The
  daily deliberation is the backstop, exactly as the scheduled cadence backstops
  the join trigger today.
- **Min-lead suppression.** Don't fire when the scheduled deliberation is
  already imminent; it would cover the same events within the lead time.
- **A daily wake budget.** The bound the join trigger never needed (one event
  type, a handful a week) and a general evaluator absolutely does. Past the
  cap, wakes degrade to digest and say so out loud rather than spending.

**Phase 0 is shadow-only.** ``evaluate`` is pure measurement: it records what it
*would* have fired to ``wake_observations`` in the telemetry DB and returns the
decision, but nothing composes and nothing posts. Shadow marks live under
separate cursor keys from the live ones, so turning Phase 1 on does not find
its high-water marks already advanced past events that were never actually
covered.

Grouping note: immediate events fire as **one wake per model tier**, not one
per event. That is what the brain already does — on 2026-07-18 a single post
covered two joins and a verified leave, and on 2026-08-03 one post covered
season close + colosseum week + league change. Splitting them would produce the
surface divergence the v4 architecture died of. The tier split (Haiku roster,
Sonnet narrative) keeps a cheap roster wake from being dragged to Sonnet prices
by an unrelated war event in the same tick.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

from db import managed_connection
from engine.event_contracts import wake_policy
from runtime.awareness.store import AWARENESS_EVENT_STREAMS, ensure_event_cursors

log = logging.getLogger("elixir")

# Cursor keys. Shadow and live are deliberately separate namespaces: a shadow
# run must never advance the marks the live responder will depend on.
SHADOW_CURSOR_PREFIX = "awareness:wake:shadow:"
LIVE_CURSOR_PREFIX = "awareness:wake:"
# Daily wake-budget counter, in the CLAN db. Deliberately NOT telemetry: this
# number can suppress a wake, and a decision may not depend on a file that is
# safe to delete.
BUDGET_CURSOR_PREFIX = "awareness:wake:budget:"

# How long a batch class may coalesce before it fires anyway, and the count that
# fires it early. Three badges in an hour should be one post, not three.
BATCH_WINDOW_MINUTES_DEFAULT = 60
BATCH_MAX_EVENTS = 3

# Don't fire when the scheduled deliberation is this close (minutes).
MIN_LEAD_MINUTES_DEFAULT = 20

# Worst-case wakes per day. A chaotic day degrades to digest rather than
# spending without bound; the daily deliberation still covers everything.
DAILY_WAKE_BUDGET_DEFAULT = 20

# The four streams do not agree on what to call the subject column: player_events
# keys on player_tag, clan/game on subject_tag, and war_events has no subject at
# all (its subject is the clan). Normalizing here keeps every downstream reader
# — the scoped seed most of all — on one field name.
_SUBJECT_COLUMN = {
    "player_events": "player_tag",
    "clan_events": "subject_tag",
    "war_events": None,
    "game_events": "subject_tag",
}


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def wake_enabled() -> bool:
    """Master switch for wake evaluation (Phase 0: shadow measurement)."""
    return _flag("ELIXIR_WAKE_POLICY")


def shadow_only() -> bool:
    """Phase 0 default. Phase 1 flips this for the classes it takes over."""
    return _flag("ELIXIR_WAKE_SHADOW")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("wake: bad %s=%r, using %s", name, raw, default)
        return default


def batch_window_minutes() -> int:
    return _int_env("ELIXIR_WAKE_BATCH_WINDOW_MINUTES", BATCH_WINDOW_MINUTES_DEFAULT)


def min_lead_minutes() -> int:
    return _int_env("ELIXIR_WAKE_MIN_LEAD_MINUTES", MIN_LEAD_MINUTES_DEFAULT)


def daily_wake_budget() -> int:
    return _int_env("ELIXIR_WAKE_DAILY_BUDGET", DAILY_WAKE_BUDGET_DEFAULT)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cursor_prefix(mode: str) -> str:
    return SHADOW_CURSOR_PREFIX if mode == "shadow" else LIVE_CURSOR_PREFIX


def _class_key(mode: str, wake_class: str, wake_model: str, job: str = "-") -> str:
    """Per-group high-water key.

    The job joined the key on 2026-08-05 (Phase 2). Old ``class:model`` rows are
    left behind as orphans on purpose: `_high_water` returns 0 for an unknown
    key, so each new group re-evaluates its stream once and the cursor-driven
    dedup means an already-covered event cannot produce a second post.
    """
    return f"{_cursor_prefix(mode)}{wake_class}:{wake_model}:{job}"


def _job_for_event_type(event_type: str) -> str:
    """The job name for grouping, or ``-`` for events no job claims.

    Imported lazily: `respond` imports the chassis, which imports prompts and the
    agent stack, and `wake` runs inside the engine tick where that would be a
    heavy and circular import at module load.
    """
    from runtime.awareness.respond import JOB_BY_EVENT_TYPE

    return JOB_BY_EVENT_TYPE.get(event_type) or "-"


def _high_water(conn: sqlite3.Connection, consumer_key: str) -> int:
    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ''",
        (consumer_key,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def pending_events(conn: sqlite3.Connection) -> list[dict]:
    """Every event row past the awareness cursors, across all four streams.

    "Not consumed" is measured against the awareness cursor, not against this
    tick's emissions: an event emitted three ticks ago whose awareness run
    failed is still pending and still deserves attention.
    """
    positions, _ = ensure_event_cursors(conn)
    out: list[dict] = []
    for stream, table in AWARENESS_EVENT_STREAMS.items():
        cursor = int(positions.get(stream) or 0)
        subject_column = _SUBJECT_COLUMN.get(table)
        subject_select = (
            f"{subject_column} AS subject_tag" if subject_column else "NULL AS subject_tag"
        )
        rows = conn.execute(
            # payload_json is not optional context — it is the event's substance.
            # Without it a farewell cannot see the leader's departure note, a
            # role change cannot tell a promotion from a demotion, and the podium
            # has no finishers. The responder has always read `payload`; until
            # 2026-08-05 nothing ever put one there, so the branch was live only
            # in tests.
            f"SELECT event_id, event_type, {subject_select}, observed_at, dedup_key, payload_json "
            f"FROM {table} WHERE event_id > ? ORDER BY event_id",
            (cursor,),
        ).fetchall()
        for r in rows:
            wake_class, wake_model = wake_policy(r["event_type"])
            entry = {
                "stream": stream,
                "event_id": int(r["event_id"]),
                "event_type": r["event_type"],
                "subject_tag": r["subject_tag"],
                "observed_at": r["observed_at"],
                "signal_key": r["dedup_key"]
                or f"{stream}:{r['event_type']}:{r['subject_tag']}:{r['observed_at']}",
                "wake_class": wake_class,
                "wake_model": wake_model,
            }
            payload = _payload(r["payload_json"])
            if payload is not None:
                entry["payload"] = payload
            out.append(entry)
    return out


def _payload(raw) -> dict | None:
    """Decode an event payload, tolerating a bad row rather than losing the tick.

    A malformed payload costs that event its detail; it must never cost the
    whole evaluation, because the events beside it may carry a hard-post floor.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except TypeError, ValueError:
        log.warning("wake: unparseable payload_json, continuing without it")
        return None
    return value if isinstance(value, dict) else None


def _age_minutes(observed_at: str | None, now: datetime) -> float:
    if not observed_at:
        return 0.0
    try:
        stamp = datetime.strptime(str(observed_at), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0.0
    return (now - stamp).total_seconds() / 60.0


def _budget_key(mode: str) -> str:
    return f"{BUDGET_CURSOR_PREFIX}{mode}"


def _wakes_today(conn: sqlite3.Connection, mode: str, now: datetime) -> int:
    """Wakes this mode has already spent today, from the CLAN database.

    Counted from ``stream_cursors`` keyed ``(awareness:wake:budget:<mode>,
    YYYY-MM-DD)`` — the table's ``scope_key`` column exists for exactly this and
    was otherwise unused, so a dated counter needs no migration.

    **This used to read the observations table in the telemetry database, and
    that was wrong on principle.** The telemetry file is operational history for
    humans: it must be safe to delete, and nothing Elixir *does* may depend on
    it. A budget that suppresses a wake is a decision, not a report, so its state
    belongs in the clan DB beside the cursors it works with.

    It was also wrong in fact. `observe()` only ever wrote `mode='shadow'` rows
    while the live path read `mode='live'`, so the count was 0 on every live tick
    and the cap had never once bound. Moving the counter to the point of spend
    fixes both at the same time.
    """
    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (_budget_key(mode), now.strftime("%Y-%m-%d")),
    ).fetchone()
    return int(row[0] or 0) if row else 0


@managed_connection
def record_spend(mode: str, *, count: int = 1, now: datetime = None, conn=None) -> int:
    """Charge ``count`` wakes against today's budget. Returns the new total.

    Called where the money is actually spent — after the responder has RUN, not
    after it has succeeded, because a failed turn costs the same as a good one.
    Old dates are left in place; db-maintenance purges them with everything else.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO stream_cursors (consumer_key, scope_key, cursor_int, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(consumer_key, scope_key) DO UPDATE SET "
        "cursor_int = COALESCE(stream_cursors.cursor_int, 0) + excluded.cursor_int, "
        "updated_at = excluded.updated_at",
        (_budget_key(mode), stamp, max(0, int(count)), _utcnow()),
    )
    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ?",
        (_budget_key(mode), stamp),
    ).fetchone()
    return int(row[0] or 0) if row else 0


@managed_connection
def evaluate(
    *,
    mode: str | None = None,
    next_scheduled_at: datetime | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection = None,
) -> dict:
    """Decide which wakes are owed. Read-only; marks move in :func:`mark_fired`.

    Returns ``{"mode", "wakes": [...], "held": [...], "pending": N,
    "budget_remaining": N}`` where each wake carries ``wake_class``,
    ``wake_model``, ``events``, ``high_water`` (a per-stream dict) and
    ``reason``.
    """
    mode = mode or ("shadow" if shadow_only() else "live")
    now = now or datetime.now(timezone.utc)

    if not wake_enabled():
        return {"mode": mode, "wakes": [], "held": [], "pending": 0, "budget_remaining": 0}

    events = pending_events(conn)
    if not events:
        return {"mode": mode, "wakes": [], "held": [], "pending": 0, "budget_remaining": 0}

    lead_ok = True
    lead_reason = ""
    if next_scheduled_at is not None:
        if next_scheduled_at.tzinfo is None:
            next_scheduled_at = next_scheduled_at.replace(tzinfo=timezone.utc)
        lead = (next_scheduled_at - now).total_seconds() / 60.0
        if 0 <= lead < min_lead_minutes():
            lead_ok = False
            lead_reason = f"scheduled deliberation in {lead:.0f}m (< {min_lead_minutes()}m lead)"

    budget = daily_wake_budget()
    spent = _wakes_today(conn, mode, now)

    # Group by (class, model, job). Events of the same tier AND the same job ride
    # together — one author, one post, no divergence.
    #
    # The job is in the key because without it Phase 2 defeats itself. A join and
    # a verified departure are both (immediate, lightweight): they would land in
    # one wake, `job_for` would see {welcome, farewell}, refuse to pick, and the
    # whole wake would fall to the daily brain — silently, and precisely on the
    # busy ticks that matter most. Class stays in the key because it carries
    # timing: a batch group waits for its window, and an immediate legendary
    # badge must not be held behind an arena climb just because they share a job.
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for event in events:
        if event["wake_class"] in ("digest", "never"):
            continue
        job = _job_for_event_type(event["event_type"])
        groups.setdefault((event["wake_class"], event["wake_model"], job), []).append(event)

    wakes: list[dict] = []
    held: list[dict] = []
    window = batch_window_minutes()

    for (wake_class, wake_model, job), group in sorted(groups.items()):
        consumer_key = _class_key(mode, wake_class, wake_model, job)
        mark = _high_water(conn, consumer_key)
        fresh = [e for e in group if e["event_id"] > mark]
        if not fresh:
            continue

        oldest = min((e["observed_at"] or "") for e in fresh)
        if wake_class == "batch":
            age = max(_age_minutes(e["observed_at"], now) for e in fresh)
            if age < window and len(fresh) < BATCH_MAX_EVENTS:
                held.append(
                    {
                        "wake_class": wake_class,
                        "wake_model": wake_model,
                        "events": fresh,
                        "reason": (
                            f"coalescing {len(fresh)} event(s), oldest {age:.0f}m "
                            f"of {window}m window"
                        ),
                    }
                )
                continue

        reason = f"{len(fresh)} {wake_class} event(s): " + ", ".join(
            sorted({e["event_type"] for e in fresh})
        )

        if not lead_ok:
            held.append(
                {
                    "wake_class": wake_class,
                    "wake_model": wake_model,
                    "events": fresh,
                    "reason": lead_reason,
                }
            )
            continue

        if budget and spent + len(wakes) >= budget:
            held.append(
                {
                    "wake_class": wake_class,
                    "wake_model": wake_model,
                    "events": fresh,
                    "reason": f"daily wake budget spent ({spent}/{budget}) — degraded to digest",
                }
            )
            continue

        wakes.append(
            {
                "wake_class": wake_class,
                "wake_model": wake_model,
                "events": fresh,
                "oldest_observed_at": oldest,
                "high_water": max(e["event_id"] for e in fresh),
                "consumer_key": consumer_key,
                "reason": reason,
            }
        )

    return {
        "mode": mode,
        "wakes": wakes,
        "held": held,
        "pending": len(events),
        "budget_remaining": max(0, budget - spent - len(wakes)) if budget else 0,
    }


@managed_connection
def mark_fired(consumer_key: str, high_water: int, *, conn: sqlite3.Connection = None) -> None:
    """Record that ``consumer_key`` fired through ``high_water``.

    Called after the wake has been consumed by delivery or explicit successful
    silence. A failed or merely empty responder turn leaves the cursor in place
    for the daily deliberation backstop.
    """
    conn.execute(
        "INSERT INTO stream_cursors "
        "(consumer_key, scope_key, cursor_int, updated_at) VALUES (?, '', ?, ?) "
        "ON CONFLICT(consumer_key, scope_key) DO UPDATE SET "
        "cursor_int = MAX(COALESCE(stream_cursors.cursor_int, 0), excluded.cursor_int), "
        "updated_at = excluded.updated_at",
        (consumer_key, max(0, int(high_water or 0)), _utcnow()),
    )


def observe(decision: dict) -> int:
    """Record a decision to ``wake_observations`` and advance its marks.

    Returns the number of wakes recorded as fired. This is the whole of Phase 0:
    measurement plus mark movement, no composition and no posting.
    """
    from storage import telemetry

    mode = decision.get("mode") or "shadow"
    recorded = 0
    for wake in decision.get("wakes") or []:
        telemetry.record_wake_observation(
            mode=mode,
            wake_class=wake["wake_class"],
            wake_model=wake["wake_model"],
            signal_keys=[e["signal_key"] for e in wake["events"]],
            event_types=sorted({e["event_type"] for e in wake["events"]}),
            oldest_observed_at=wake.get("oldest_observed_at"),
            reason=wake.get("reason", ""),
            fired=True,
        )
        try:
            mark_fired(wake["consumer_key"], wake["high_water"])
        except Exception:
            log.exception("wake: could not mark high-water for %s", wake["consumer_key"])
        # Shadow spends nothing, but it keeps its own counter so the shadow
        # budget behaves like the live one and the two never share a number.
        try:
            record_spend(mode)
        except Exception:
            log.exception("wake: could not charge the %s wake budget", mode)
        recorded += 1

    for hold in decision.get("held") or []:
        # Held decisions are NOT marked — they must re-evaluate next tick.
        telemetry.record_wake_observation(
            mode=mode,
            wake_class=hold["wake_class"],
            wake_model=hold["wake_model"],
            signal_keys=[e["signal_key"] for e in hold["events"]],
            event_types=sorted({e["event_type"] for e in hold["events"]}),
            reason=hold.get("reason", ""),
            fired=False,
        )
    return recorded


def evaluate_and_observe(*, next_scheduled_at: datetime | None = None) -> dict:
    """The engine tick's entry point. Never raises: measurement must not be able
    to fail a tick."""
    try:
        decision = evaluate(next_scheduled_at=next_scheduled_at)
    except Exception:
        log.exception("wake: evaluation failed")
        return {}
    if not decision.get("wakes") and not decision.get("held"):
        return {}
    try:
        observe(decision)
    except Exception:
        log.exception("wake: observation failed")
    summary = {
        "mode": decision.get("mode"),
        "fired": [
            f"{w['wake_class']}/{w['wake_model']}×{len(w['events'])}"
            for w in decision.get("wakes") or []
        ],
        "held": len(decision.get("held") or []),
    }
    if summary["fired"]:
        log.info(
            "wake (%s): would fire %s — %s",
            summary["mode"],
            ", ".join(summary["fired"]),
            "; ".join(w.get("reason", "") for w in decision["wakes"]),
        )
    return summary


def wake_policy_table() -> list[dict]:
    """The current registry, for the shadow report and for `/clanops`."""
    from engine.event_contracts import EVENT_CONTRACTS

    return [
        {
            "event_type": event_type,
            "wake": contract.wake,
            "wake_model": contract.wake_model,
            "hard_post": contract.hard_post,
            "lane": contract.lane,
        }
        for event_type, contract in sorted(EVENT_CONTRACTS.items())
    ]


__all__ = [
    "BATCH_MAX_EVENTS",
    "LIVE_CURSOR_PREFIX",
    "SHADOW_CURSOR_PREFIX",
    "batch_window_minutes",
    "daily_wake_budget",
    "evaluate",
    "evaluate_and_observe",
    "mark_fired",
    "min_lead_minutes",
    "observe",
    "pending_events",
    "shadow_only",
    "wake_enabled",
    "wake_policy_table",
]
