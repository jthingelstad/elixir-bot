"""Posting policy for arena-relay leader actions."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db

CHICAGO = ZoneInfo("America/Chicago")
LEADER_ACTION_DAILY_CAP = int(os.getenv("LEADER_ACTION_DAILY_CAP", "4"))
LEADER_ACTION_QUIET_START_HOUR = int(os.getenv("LEADER_ACTION_QUIET_START_HOUR", "22"))
LEADER_ACTION_QUIET_END_HOUR = int(os.getenv("LEADER_ACTION_QUIET_END_HOUR", "7"))


def _local_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(CHICAGO)


def is_quiet_time(now: datetime | None = None) -> bool:
    hour = _local_now(now).hour
    start = LEADER_ACTION_QUIET_START_HOUR
    end = LEADER_ACTION_QUIET_END_HOUR
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _today_bounds_utc(now: datetime | None = None) -> tuple[str, str]:
    day = _local_now(now).date().isoformat()
    return db.chicago_day_bounds_utc(day)


def count_actions_today(*, conn=None, now: datetime | None = None) -> int:
    start, end = _today_bounds_utc(now)
    close = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM leader_action_recommendations "
            "WHERE proposed_at >= ? AND proposed_at < ?",
            (start, end),
        ).fetchone()
        return int(row["cnt"] if row else 0)
    finally:
        if close:
            conn.close()


def can_post_leader_action(*, critical: bool = False, conn=None, now: datetime | None = None) -> tuple[bool, str | None]:
    if critical:
        return True, None
    if is_quiet_time(now):
        return False, "quiet_hours"
    count = count_actions_today(conn=conn, now=now)
    if count >= LEADER_ACTION_DAILY_CAP:
        return False, f"daily_cap:{LEADER_ACTION_DAILY_CAP}"
    return True, None


__all__ = [
    "LEADER_ACTION_DAILY_CAP",
    "LEADER_ACTION_QUIET_END_HOUR",
    "LEADER_ACTION_QUIET_START_HOUR",
    "can_post_leader_action",
    "count_actions_today",
    "is_quiet_time",
]
