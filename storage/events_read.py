"""Event-stream read facades — v5.1 (schema.md §9: get_elixir_state).

Replaces event_core.read.event_facades: the same three call shapes, reading
the four v5.1 event streams (player/clan/war events + battle_events) instead
of the retired detections table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import managed_connection

__all__ = [
    "summarize_event_windows",
    "list_recent_events",
    "summarize_battle_modes",
]

DETECTION_WINDOWS = (1, 7, 28)

# (stream, table, subject_expr, timing_expr, has_backfilled). game_events is a
# different shape — no `timing` column, but it carries `backfilled` — so its
# metadata differs. It is NOT in the default set: legacy callers (member_report,
# webapp history) don't expect game-level rows; the awareness brain opts in.
_ALL_STREAMS = (
    ("player", "player_events", "player_tag", "timing", False),
    ("clan", "clan_events", "COALESCE(subject_tag, clan_tag)", "timing", False),
    ("war", "war_events", "CAST(season_id AS TEXT)", "timing", False),
    ("game", "game_events", "subject_tag", "NULL", True),
)
_DEFAULT_STREAMS = ("player", "clan", "war")


def _anchor(now: Optional[str] = None) -> datetime:
    return (
        datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)
        if now
        else datetime.now(timezone.utc)
    )


def _cutoff(days: int, now: Optional[str] = None) -> str:
    """ISO cutoff — for the observed_at columns (player/clan/war events)."""
    return (_anchor(now) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _cutoff_compact(days: int, now: Optional[str] = None) -> str:
    """CR-compact cutoff (YYYYMMDDTHHMMSS) — for battle_events.battle_time, which
    is stored in Clash Royale's compact form (20260507T144643.000Z), NOT ISO.
    Comparing that column against the ISO _cutoff sorts below every real value,
    so the window filter matches ALL of history (7d and 28d return identical,
    inflated counts). Same fix as runtime/member_report._cutoff_compact."""
    return (_anchor(now) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S")


@managed_connection
def summarize_event_windows(
    *,
    windows: tuple[int, ...] = DETECTION_WINDOWS,
    scope: str | None = None,
    subject_type: str | None = None,  # call-site compatibility; unused
    subject_key: str | None = None,
    event_class: str | None = None,  # call-site compatibility; unused
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Event counts by type per lookback window, across all four streams."""
    del subject_type, event_class
    out: dict = {"windows": {}}
    for days in windows:
        cutoff = _cutoff(days, now)
        counts: dict[str, int] = {}
        total = 0
        for stream, table, subject_expr, _timing_expr, _has_bf in _ALL_STREAMS:
            if stream not in _DEFAULT_STREAMS:
                continue
            where = ["observed_at >= ?"]
            params: list = [cutoff]
            if scope:
                where.append("scope = ?")
                params.append(scope)
            if subject_key:
                where.append(f"{subject_expr} = ?")
                params.append(subject_key)
            for row in conn.execute(
                f"SELECT event_type, COUNT(*) AS cnt FROM {table} "
                f"WHERE {' AND '.join(where)} GROUP BY event_type",
                tuple(params),
            ).fetchall():
                key = row["event_type"]
                counts[key] = counts.get(key, 0) + row["cnt"]
                total += row["cnt"]
        battle_cutoff = _cutoff_compact(days, now)   # battle_time is CR-compact, not ISO
        battles = conn.execute(
            "SELECT COUNT(*) AS cnt FROM battle_events WHERE battle_time >= ?"
            + (" AND player_tag = ?" if subject_key else ""),
            (battle_cutoff, subject_key) if subject_key else (battle_cutoff,),
        ).fetchone()["cnt"]
        out["windows"][f"{days}d"] = {
            "total_events": total,
            "battles_mirrored": battles,
            "by_type": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        }
    return out


@managed_connection
def list_recent_events(
    *,
    days: int = 7,
    since: str | None = None,
    scope: str | None = None,
    event_type: str | None = None,
    subject_type: str | None = None,  # call-site compatibility; unused
    subject_key: str | None = None,
    event_class: str | None = None,  # call-site compatibility; unused
    streams: tuple[str, ...] | None = None,
    exclude_backfilled: bool = False,
    limit: int = 100,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    del subject_type, event_class
    cutoff = since or _cutoff(days, now)
    wanted = set(streams) if streams else set(_DEFAULT_STREAMS)
    events: list[dict] = []
    for stream, table, subject_expr, timing_expr, has_backfilled in _ALL_STREAMS:
        if stream not in wanted:
            continue
        where = ["observed_at >= ?"]
        params: list = [cutoff]
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if subject_key:
            where.append(f"{subject_expr} = ?")
            params.append(subject_key)
        if exclude_backfilled and has_backfilled:
            # Backfilled rows are seeded history, not "new" — never a live signal.
            where.append("COALESCE(backfilled, 0) = 0")
        for row in conn.execute(
            f"SELECT dedup_key, event_type, {subject_expr} AS subject_tag, observed_at, "
            f"{timing_expr} AS timing, scope, payload_json FROM {table} "
            f"WHERE {' AND '.join(where)} ORDER BY observed_at DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall():
            item = dict(row)
            item["stream"] = stream
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except (TypeError, ValueError):
                item["payload"] = {}
            events.append(item)
    events.sort(key=lambda e: str(e.get("observed_at") or ""), reverse=True)
    return events[: max(1, int(limit))]


@managed_connection
def summarize_battle_modes(
    *,
    windows: tuple[int, ...] = (7, 28),
    now: str | None = None,
    top_members: int = 3,
    min_battles: int = 3,
    subject_key: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Per-mode battle activity from battle_events."""
    out: dict = {"windows": {}}
    for days in windows:
        cutoff = _cutoff_compact(days, now)   # battle_time is CR-compact, not ISO
        where = ["b.battle_time >= ?"]
        params: list = [cutoff]
        if subject_key:
            where.append("b.player_tag = ?")
            params.append(subject_key)
        mode_rows = conn.execute(
            "SELECT b.mode_group, b.game_mode_name, COUNT(*) AS battles, "
            "SUM(CASE WHEN b.outcome = 'W' THEN 1 ELSE 0 END) AS wins, "
            "COUNT(DISTINCT b.player_tag) AS members_active "
            "FROM battle_events b "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY b.mode_group, b.game_mode_name "
            "ORDER BY battles DESC",
            tuple(params),
        ).fetchall()
        modes = []
        for row in mode_rows:
            if (row["battles"] or 0) < min_battles:
                continue
            top = conn.execute(
                "SELECT b.player_tag AS tag, COALESCE(p.display_name, p.current_name) AS name, COUNT(*) AS battles "
                "FROM battle_events b LEFT JOIN players p ON p.player_tag = b.player_tag "
                f"WHERE {' AND '.join(where)} AND b.mode_group IS ? AND b.game_mode_name IS ? "
                "GROUP BY b.player_tag ORDER BY battles DESC LIMIT ?",
                (*params, row["mode_group"], row["game_mode_name"], max(1, int(top_members))),
            ).fetchall()
            modes.append({
                "mode_group": row["mode_group"],
                "game_mode_name": row["game_mode_name"],
                "battles": row["battles"],
                "wins": row["wins"],
                "members_active": row["members_active"],
                "top_members": [dict(t) for t in top],
            })
        out["windows"][f"{days}d"] = {"modes": modes}
    return out
