"""On-demand awareness runs for joins — the "say hello now" trigger.

The awareness loop is the clan's proactive voice and it runs on a fixed cadence
(``3,9,15,21`` CT). That cadence was widened for cost, and the price was written
down at the time in ``runtime/activities.py``: *"Cost of this change: hard-posts
wait up to 6h."* For most hard-posts that price is fair — a ``week_finished``
narrated two hours late reads exactly the same. A join does not. Measured over
the seven joins since the cadence change, a newcomer waited a median of 2.0h to
be welcomed and up to 5.2h; one joined at 22:58 CT and was welcomed at 03:05 CT,
to an empty room. The newcomer is in the app *now*, and that is the only moment
the welcome is worth anything.

This module answers one question after each engine tick: **is there a newcomer
nobody has said hello to yet?** If so, the tick asks the scheduler to run the
awareness loop out of band, right now.

What this module deliberately does NOT do is compose or post anything. That
distinction is the whole design. The 2026-07-04 single-pipeline rule — *"the old
independent profile lookup here is why the in-game welcome and the #clan-events
welcome told different stories"* — is what a second author costs, and a join is
the worst possible post to pay it on, because it is the one hard-post that must
carry a paired in-game ``clan_chat`` sibling authored in the same grounded pass
(``runtime/awareness/policy.py`` fails the tick otherwise). The brain stays the
sole author of its posts and their siblings. All this changes is *when* the one
author is asked to think.

Two properties make running the brain more often safe rather than risky:

- The loop is **cursor-driven** (``stream_cursors``, advanced only on success),
  so an extra run consumes what is past the cursor and nothing else. Extra runs
  cannot double-post; that is a property of the design, not of this module.
- The trigger carries its own **high-water mark**. It fires for a join once and
  never again for that same join, even if the resulting awareness tick fails and
  leaves the event cursor unadvanced. Without that, a join that trips the copy
  policy would re-fire an expensive Sonnet run every ten minutes forever. A
  failed run falls back to the scheduled cadence, which is exactly where it was
  before this module existed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

from db import managed_connection
from runtime.awareness.store import ensure_event_cursors

log = logging.getLogger("elixir")

# The consumer key for this trigger's own high-water mark. It lives in
# stream_cursors beside the awareness event cursors (and the older recognize:*
# / emit:* consumers) because it is exactly that: a durable per-consumer
# position. It is NOT an awareness event cursor and must never be advanced by
# store.advance_event_cursors — that helper filters to AWARENESS_EVENT_STREAMS,
# which keeps the two apart.
TRIGGER_CURSOR_KEY = "awareness:join_trigger"

# Don't bother firing when the scheduled run is already imminent — the join
# would be covered within the lead time anyway and an out-of-band run here just
# buys a few minutes for a full Sonnet tick.
MIN_LEAD_MINUTES_DEFAULT = 20


def trigger_enabled() -> bool:
    return os.getenv("ELIXIR_JOIN_TRIGGER", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _min_lead_minutes() -> int:
    raw = os.getenv("ELIXIR_JOIN_TRIGGER_MIN_LEAD_MINUTES")
    if not raw:
        return MIN_LEAD_MINUTES_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "join trigger: bad ELIXIR_JOIN_TRIGGER_MIN_LEAD_MINUTES=%r, using %s",
            raw,
            MIN_LEAD_MINUTES_DEFAULT,
        )
        return MIN_LEAD_MINUTES_DEFAULT


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _high_water(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT cursor_int FROM stream_cursors WHERE consumer_key = ? AND scope_key = ''",
        (TRIGGER_CURSOR_KEY,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def pending_joins(conn: sqlite3.Connection) -> list[dict]:
    """`member_joined` rows the awareness brain has not consumed yet.

    "Not consumed" is measured against the awareness clan-stream cursor, not
    against this tick's emissions: a join emitted three ticks ago whose awareness
    run failed is still pending, and still deserves a welcome. That makes the
    trigger self-healing across restarts — it re-derives the situation from
    durable state every tick instead of trusting a one-shot in-memory signal.
    """
    positions, _ = ensure_event_cursors(conn)
    clan_cursor = int(positions.get("clan") or 0)
    rows = conn.execute(
        "SELECT event_id, subject_tag, payload_json, observed_at FROM clan_events "
        "WHERE event_type = 'member_joined' AND event_id > ? ORDER BY event_id",
        (clan_cursor,),
    ).fetchall()
    return [
        {
            "event_id": int(r["event_id"]),
            "subject_tag": r["subject_tag"],
            "observed_at": r["observed_at"],
            "payload_json": r["payload_json"],
        }
        for r in rows
    ]


@managed_connection
def evaluate(
    *,
    next_scheduled_at: datetime | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection = None,
) -> dict:
    """Decide whether an out-of-band awareness run is owed for a newcomer.

    Returns ``{"run": bool, "reason": str, "joins": [...], "high_water": int}``.
    Read-only: the high-water mark moves in :func:`mark_fired`, after the run has
    actually been attempted, so a decision that never turns into a run is not
    silently swallowed.
    """
    if not trigger_enabled():
        return {"run": False, "reason": "disabled", "joins": [], "high_water": 0}

    joins = pending_joins(conn)
    if not joins:
        return {"run": False, "reason": "no pending joins", "joins": [], "high_water": 0}

    high_water = _high_water(conn)
    fresh = [j for j in joins if j["event_id"] > high_water]
    top = max(j["event_id"] for j in joins)
    if not fresh:
        # Already fired for every pending join. The awareness run that followed
        # must have failed or chosen not to advance; the scheduled cadence is the
        # backstop. Firing again would just buy another failure at Sonnet prices.
        return {
            "run": False,
            "reason": f"already fired through event {high_water}",
            "joins": joins,
            "high_water": high_water,
        }

    if next_scheduled_at is not None:
        now = now or datetime.now(timezone.utc)
        if next_scheduled_at.tzinfo is None:
            next_scheduled_at = next_scheduled_at.replace(tzinfo=timezone.utc)
        lead_minutes = (next_scheduled_at - now).total_seconds() / 60.0
        min_lead = _min_lead_minutes()
        if 0 <= lead_minutes < min_lead:
            # Deliberately do NOT move the high-water mark here: the scheduled run
            # is about to cover this join, and if it somehow doesn't, the next
            # tick re-evaluates with a fresh lead time.
            return {
                "run": False,
                "reason": f"scheduled run in {lead_minutes:.0f}m (< {min_lead}m lead)",
                "joins": joins,
                "high_water": high_water,
            }

    return {
        "run": True,
        "reason": f"{len(fresh)} unwelcomed join(s)",
        "joins": fresh,
        "high_water": top,
    }


@managed_connection
def mark_fired(high_water: int, *, conn: sqlite3.Connection = None) -> None:
    """Record that the trigger fired through ``high_water``.

    Called after the out-of-band run has been *attempted*, success or not. A
    failed attempt still marks: the point of the mark is to bound cost to one
    extra run per join, and the scheduled cadence remains the backstop for a
    join whose run failed.
    """
    conn.execute(
        "INSERT INTO stream_cursors "
        "(consumer_key, scope_key, cursor_int, updated_at) VALUES (?, '', ?, ?) "
        "ON CONFLICT(consumer_key, scope_key) DO UPDATE SET "
        "cursor_int = MAX(COALESCE(stream_cursors.cursor_int, 0), excluded.cursor_int), "
        "updated_at = excluded.updated_at",
        (TRIGGER_CURSOR_KEY, max(0, int(high_water or 0)), _utcnow()),
    )


__all__ = [
    "TRIGGER_CURSOR_KEY",
    "evaluate",
    "mark_fired",
    "pending_joins",
    "trigger_enabled",
]
