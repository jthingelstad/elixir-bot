"""Posting policy for arena-relay leader actions.

Clash runs 24 hours and so does the action board — there are no quiet
hours and no posts-per-day quota. The board self-regulates on two
responsiveness signals instead:

- **Open-card backlog**: while leadership has LEADER_ACTION_OPEN_CARD_CAP
  undecided cards in front of them, Elixir stops adding more. Leaders in
  any timezone re-open the budget the moment they decide cards.
- **Earned frequency**: an action type the leaders keep declining
  self-throttles to one card per cooldown (see can_post_leader_action).

Critical war moments bypass both gates.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import db

# How many undecided (proposed) cards may sit on the board before Elixir
# stops posting non-critical ones.
LEADER_ACTION_OPEN_CARD_CAP = int(os.getenv("LEADER_ACTION_OPEN_CARD_CAP", "5"))
# Open cards older than this stop counting against the backlog — a stale
# nudge from a finished war shouldn't deadlock the board forever.
LEADER_ACTION_BACKLOG_WINDOW_DAYS = int(os.getenv("LEADER_ACTION_BACKLOG_WINDOW_DAYS", "7"))
# Earned frequency: an action type the leader keeps declining self-throttles.
# Once at least MIN_DECIDED decisions exist in the trailing window and the
# decline rate crosses the threshold, that type is limited to one card per
# cooldown instead of posting freely. Critical war actions bypass this like
# they bypass every other gate.
LEADER_ACTION_DECLINE_RATE_THRESHOLD = float(os.getenv("LEADER_ACTION_DECLINE_RATE_THRESHOLD", "0.6"))
LEADER_ACTION_MIN_DECIDED_FOR_THROTTLE = int(os.getenv("LEADER_ACTION_MIN_DECIDED_FOR_THROTTLE", "5"))
LEADER_ACTION_THROTTLED_COOLDOWN_HOURS = int(os.getenv("LEADER_ACTION_THROTTLED_COOLDOWN_HOURS", "72"))


def count_open_leader_actions(*, conn=None, now: datetime | None = None) -> int:
    """Undecided, unsuppressed cards proposed within the backlog window."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    now_text = current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff = (current - timedelta(days=max(1, LEADER_ACTION_BACKLOG_WINDOW_DAYS))).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    close = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM leader_action_recommendations "
            "WHERE status = 'proposed' AND COALESCE(is_test, 0) = 0 "
            "AND proposed_at >= ? "
            "AND (expires_at IS NULL OR expires_at <= ?)",
            (cutoff, now_text),
        ).fetchone()
        return int(row["cnt"] if row else 0)
    finally:
        if close:
            conn.close()


def can_post_leader_action(*, critical: bool = False, action_type: str | None = None, conn=None, now: datetime | None = None) -> tuple[bool, str | None]:
    if critical:
        return True, None
    backlog = count_open_leader_actions(conn=conn, now=now)
    if backlog >= LEADER_ACTION_OPEN_CARD_CAP:
        return False, f"open_card_backlog:{backlog}/{LEADER_ACTION_OPEN_CARD_CAP}"
    if action_type:
        stats = db.leader_action_decision_stats(action_type=action_type, conn=conn)
        decided = int(stats.get("decided") or 0)
        rate = stats.get("decline_rate")
        if (
            decided >= LEADER_ACTION_MIN_DECIDED_FOR_THROTTLE
            and rate is not None
            and rate >= LEADER_ACTION_DECLINE_RATE_THRESHOLD
            and db.has_recent_leader_action(
                action_type=action_type,
                within_hours=LEADER_ACTION_THROTTLED_COOLDOWN_HOURS,
                conn=conn,
            )
        ):
            return False, f"earned_frequency:{action_type}:decline_rate={rate:.2f}"
    return True, None


__all__ = [
    "LEADER_ACTION_BACKLOG_WINDOW_DAYS",
    "LEADER_ACTION_DECLINE_RATE_THRESHOLD",
    "LEADER_ACTION_MIN_DECIDED_FOR_THROTTLE",
    "LEADER_ACTION_OPEN_CARD_CAP",
    "LEADER_ACTION_THROTTLED_COOLDOWN_HOURS",
    "can_post_leader_action",
    "count_open_leader_actions",
]
