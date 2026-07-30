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
    "list_events_after_cursors",
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


def _decode_event(row, stream: str) -> dict:
    item = dict(row)
    item["stream"] = stream
    item["stream_position"] = int(item.pop("event_id"))
    try:
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
    except TypeError, ValueError:
        item["payload"] = {}
    return item


@managed_connection
def list_events_after_cursors(
    cursors: dict[str, int],
    *,
    streams: tuple[str, ...] = ("player", "clan", "war", "game"),
    limit: int = 80,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Return the oldest bounded batch after durable per-stream positions.

    Positions are SQLite ``event_id`` values, not timestamps. Each stream is
    read independently and the results are merged by insertion time; advancing
    the returned checkpoints can therefore never skip an unseen row in that
    stream. Backfilled game rows remain in the batch so their positions can be
    acknowledged, but callers decide whether they are member-facing context.
    """
    wanted = set(streams)
    cap = max(1, int(limit))
    candidates: list[dict] = []
    heads: dict[str, int] = {}

    for stream, table, subject_expr, timing_expr, has_backfilled in _ALL_STREAMS:
        if stream not in wanted:
            continue
        start = max(0, int(cursors.get(stream, 0) or 0))
        heads[stream] = int(
            conn.execute(f"SELECT COALESCE(MAX(event_id), 0) FROM {table}").fetchone()[0]
        )
        backfilled_expr = "backfilled" if has_backfilled else "0"
        rows = conn.execute(
            f"SELECT event_id, dedup_key, event_type, {subject_expr} AS subject_tag, "
            f"observed_at, created_at, {timing_expr} AS timing, scope, payload_json, "
            f"{backfilled_expr} AS backfilled FROM {table} "
            "WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
            (start, cap + 1),
        ).fetchall()
        candidates.extend(_decode_event(row, stream) for row in rows)

    # created_at is the insertion clock shared by all four streams. event_id and
    # stream make ties deterministic without pretending ids are cross-stream.
    candidates.sort(
        key=lambda e: (
            str(e.get("created_at") or e.get("observed_at") or ""),
            str(e.get("stream") or ""),
            int(e.get("stream_position") or 0),
        )
    )
    batch = candidates[:cap]
    checkpoints = {stream: max(0, int(cursors.get(stream, 0) or 0)) for stream in wanted}
    for event in batch:
        stream = event["stream"]
        checkpoints[stream] = max(checkpoints.get(stream, 0), event["stream_position"])

    remaining = {
        stream: max(0, heads.get(stream, 0) - checkpoints.get(stream, 0)) for stream in wanted
    }
    return {
        "events": batch,
        "checkpoints": checkpoints,
        "heads": heads,
        "remaining_by_stream": remaining,
        "has_more": any(heads.get(s, 0) > checkpoints.get(s, 0) for s in wanted),
    }


def _anchor(now: Optional[str] = None) -> datetime:
    return (
        datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)
        if now
        else datetime.now(timezone.utc)
    )


def _cutoff(days: int, now: Optional[str] = None) -> str:
    """ISO-Z cutoff. One helper for every timestamp column here: since schema
    v25 battle_time carries the same ISO-Z shape as the observed_at columns, so
    the separate CR-compact cutoff this module used to keep is gone."""
    return (_anchor(now) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@managed_connection
def summarize_event_windows(
    *,
    windows: tuple[int, ...] = DETECTION_WINDOWS,
    scope: str | None = None,
    subject_key: str | None = None,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Event counts by type per lookback window, across all four streams."""
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
        battle_cutoff = _cutoff(days, now)
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
    subject_key: str | None = None,
    streams: tuple[str, ...] | None = None,
    exclude_backfilled: bool = False,
    limit: int = 100,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
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
            except TypeError, ValueError:
                item["payload"] = {}
            events.append(item)
    events.sort(key=lambda e: str(e.get("observed_at") or ""), reverse=True)
    return events[: max(1, int(limit))]
