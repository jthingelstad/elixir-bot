"""Long-term rollups for the normalized event stream."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import db as _db
from db import EVENT_STREAM_RETENTION_DAYS, managed_connection

ROLLUP_MEMBER_90D = "member_90d"
ROLLUP_WAR_CYCLE = "war_cycle"
ROLLUP_CASE_HISTORY = "case_history"

ROLLUP_TYPES = {
    ROLLUP_MEMBER_90D,
    ROLLUP_WAR_CYCLE,
    ROLLUP_CASE_HISTORY,
}

__all__ = [
    "ROLLUP_CASE_HISTORY",
    "ROLLUP_MEMBER_90D",
    "ROLLUP_TYPES",
    "ROLLUP_WAR_CYCLE",
    "get_event_rollup",
    "list_event_rollups",
    "prune_event_stream_with_rollups",
    "upsert_event_rollup",
    "write_event_rollups_for_retention",
]


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str, ensure_ascii=False)


def _json_loads(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _format_time(value: datetime) -> str:
    return value.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def _retention_cutoff(*, retention_days: int = EVENT_STREAM_RETENTION_DAYS, now: str | None = None) -> str:
    base = _parse_time(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    return _format_time(base - timedelta(days=int(retention_days or EVENT_STREAM_RETENTION_DAYS)))


def _row_to_rollup(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["summary"] = _json_loads(item.pop("summary_json", "{}"))
    return item


def _normalize_rollup_type(value: str) -> str:
    clean = _clean_text(value)
    if clean not in ROLLUP_TYPES:
        raise ValueError(f"invalid rollup_type: {value}")
    return clean


@managed_connection
def upsert_event_rollup(
    *,
    rollup_key: str,
    rollup_type: str,
    period_start: str,
    period_end: str,
    summary: dict,
    scope: str = "public",
    subject_type: str | None = None,
    subject_key: str | None = None,
    project_key: str | None = None,
    season_id: str | int | None = None,
    source_event_count: int = 0,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    clean_key = _clean_text(rollup_key)
    if not clean_key:
        raise ValueError("rollup_key is required")
    clean_type = _normalize_rollup_type(rollup_type)
    clean_start = _clean_text(period_start)
    clean_end = _clean_text(period_end)
    if not clean_start or not clean_end:
        raise ValueError("period_start and period_end are required")
    now = _db._utcnow()
    conn.execute(
        """
        INSERT INTO event_rollups (
            rollup_key, rollup_type, scope, subject_type, subject_key,
            project_key, season_id, period_start, period_end,
            source_event_count, summary_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rollup_key) DO UPDATE SET
            rollup_type = excluded.rollup_type,
            scope = excluded.scope,
            subject_type = excluded.subject_type,
            subject_key = excluded.subject_key,
            project_key = excluded.project_key,
            season_id = excluded.season_id,
            period_start = excluded.period_start,
            period_end = excluded.period_end,
            source_event_count = excluded.source_event_count,
            summary_json = excluded.summary_json,
            updated_at = excluded.updated_at
        """,
        (
            clean_key,
            clean_type,
            _clean_text(scope) or "public",
            _clean_text(subject_type),
            _clean_text(subject_key),
            _clean_text(project_key),
            _clean_text(season_id),
            clean_start,
            clean_end,
            max(0, int(source_event_count or 0)),
            _json_dumps(summary or {}),
            now,
            now,
        ),
    )
    conn.commit()
    return get_event_rollup(clean_key, conn=conn) or {}


@managed_connection
def get_event_rollup(rollup_key: str, conn: Optional[sqlite3.Connection] = None) -> dict | None:
    row = conn.execute(
        "SELECT * FROM event_rollups WHERE rollup_key = ?",
        (_clean_text(rollup_key),),
    ).fetchone()
    return _row_to_rollup(row)


@managed_connection
def list_event_rollups(
    *,
    rollup_type: str | None = None,
    scope: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    project_key: str | None = None,
    season_id: str | int | None = None,
    limit: int = 25,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    where = []
    params: list = []
    if rollup_type:
        where.append("rollup_type = ?")
        params.append(_normalize_rollup_type(rollup_type))
    if scope:
        where.append("scope = ?")
        params.append(_clean_text(scope))
    if subject_type:
        where.append("subject_type = ?")
        params.append(_clean_text(subject_type))
    if subject_key:
        where.append("subject_key = ?")
        params.append(_clean_text(subject_key))
    if project_key:
        where.append("project_key = ?")
        params.append(_clean_text(project_key))
    if season_id is not None:
        where.append("season_id = ?")
        params.append(_clean_text(season_id))
    sql_where = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM event_rollups {sql_where} "
        "ORDER BY period_end DESC, updated_at DESC, rollup_id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 25), 200))),
    ).fetchall()
    return [_row_to_rollup(row) for row in rows]


def _event_rows(
    conn: sqlite3.Connection,
    *,
    where: str,
    params: tuple | list,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_key, event_type, source_system, source_detector,
               source_signal_type, observed_at, occurred_at, scope,
               subject_type, subject_key, season_id, war_week
        FROM game_event_stream
        """
        f"WHERE {where} "
        "ORDER BY observed_at ASC, event_id ASC",
        tuple(params),
    ).fetchall()


def _summarize_event_rows(rows: list[sqlite3.Row]) -> dict:
    by_type = Counter()
    by_scope = Counter()
    by_source = Counter()
    by_subject_type = Counter()
    event_keys = []
    first_observed = None
    last_observed = None
    for row in rows:
        by_type[row["event_type"] or "unknown"] += 1
        by_scope[row["scope"] or "unknown"] += 1
        by_source[row["source_system"] or "unknown"] += 1
        by_subject_type[row["subject_type"] or "none"] += 1
        if row["event_key"] and len(event_keys) < 25:
            event_keys.append(row["event_key"])
        observed = row["observed_at"]
        first_observed = observed if first_observed is None or observed < first_observed else first_observed
        last_observed = observed if last_observed is None or observed > last_observed else last_observed
    return {
        "total": len(rows),
        "by_type": dict(sorted(by_type.items())),
        "by_scope": dict(sorted(by_scope.items())),
        "by_source_system": dict(sorted(by_source.items())),
        "by_subject_type": dict(sorted(by_subject_type.items())),
        "first_observed_at": first_observed,
        "last_observed_at": last_observed,
        "sample_event_keys": event_keys,
    }


def _dominant_scope(rows: list[sqlite3.Row], *, default: str = "public") -> str:
    counts = Counter(row["scope"] or default for row in rows)
    if not counts:
        return default
    if any(scope in counts for scope in ("leadership", "system_internal")):
        return "leadership" if counts.get("leadership", 0) >= counts.get("system_internal", 0) else "system_internal"
    return counts.most_common(1)[0][0]


def _event_bounds(conn: sqlite3.Connection, *, cutoff: str) -> tuple[str | None, str | None, int]:
    row = conn.execute(
        """
        SELECT MIN(observed_at) AS min_observed,
               MAX(observed_at) AS max_observed,
               COUNT(*) AS count
        FROM game_event_stream
        WHERE observed_at < ?
        """,
        (cutoff,),
    ).fetchone()
    return row["min_observed"], row["max_observed"], int(row["count"] or 0)


def _write_member_90d_rollups(conn: sqlite3.Connection, *, cutoff: str) -> int:
    min_observed, _max_observed, count = _event_bounds(conn, cutoff=cutoff)
    if count == 0 or not min_observed:
        return 0
    start_dt = _parse_time(min_observed)
    cutoff_dt = _parse_time(cutoff)
    if start_dt is None or cutoff_dt is None:
        return 0
    written = 0
    window_start = start_dt
    while window_start < cutoff_dt:
        window_end = min(window_start + timedelta(days=90), cutoff_dt)
        start = _format_time(window_start)
        end = _format_time(window_end)
        subject_rows = conn.execute(
            """
            SELECT DISTINCT subject_key
            FROM game_event_stream
            WHERE observed_at >= ? AND observed_at < ?
              AND subject_type = 'member'
              AND subject_key IS NOT NULL
            ORDER BY subject_key
            """,
            (start, end),
        ).fetchall()
        for subject in subject_rows:
            rows = _event_rows(
                conn,
                where="observed_at >= ? AND observed_at < ? AND subject_type = 'member' AND subject_key = ?",
                params=(start, end, subject["subject_key"]),
            )
            if not rows:
                continue
            summary = _summarize_event_rows(rows)
            summary["rollup_window_days"] = 90
            upsert_event_rollup(
                rollup_key=f"member_90d:{subject['subject_key']}:{start[:10]}:{end[:10]}",
                rollup_type=ROLLUP_MEMBER_90D,
                scope=_dominant_scope(rows),
                subject_type="member",
                subject_key=subject["subject_key"],
                period_start=start,
                period_end=end,
                source_event_count=len(rows),
                summary=summary,
                conn=conn,
            )
            written += 1
        window_start = window_end
    return written


def _write_war_cycle_rollups(conn: sqlite3.Connection, *, cutoff: str) -> int:
    seasons = conn.execute(
        """
        SELECT DISTINCT season_id
        FROM game_event_stream
        WHERE observed_at < ? AND season_id IS NOT NULL
        ORDER BY season_id
        """,
        (cutoff,),
    ).fetchall()
    written = 0
    for season in seasons:
        season_id = season["season_id"]
        rows = _event_rows(
            conn,
            where="observed_at < ? AND season_id = ?",
            params=(cutoff, season_id),
        )
        if not rows:
            continue
        summary = _summarize_event_rows(rows)
        summary["war_weeks"] = sorted({row["war_week"] for row in rows if row["war_week"] is not None})
        upsert_event_rollup(
            rollup_key=f"war_cycle:{season_id}",
            rollup_type=ROLLUP_WAR_CYCLE,
            scope=_dominant_scope(rows),
            subject_type="war",
            subject_key=f"season:{season_id}",
            season_id=season_id,
            period_start=summary["first_observed_at"] or rows[0]["observed_at"],
            period_end=summary["last_observed_at"] or rows[-1]["observed_at"],
            source_event_count=len(rows),
            summary=summary,
            conn=conn,
        )
        written += 1
    return written


def _write_case_history_rollups(conn: sqlite3.Connection, *, cutoff: str) -> int:
    rows = conn.execute(
        """
        SELECT *
        FROM decision_cases
        WHERE status IN ('resolved', 'dismissed')
           OR updated_at < ?
        ORDER BY updated_at DESC, case_id DESC
        """,
        (cutoff,),
    ).fetchall()
    written = 0
    for row in rows:
        state = _json_loads(row["state_json"])
        summary = {
            "case_key": row["case_key"],
            "case_type": row["case_type"],
            "status": row["status"],
            "title": row["title"],
            "recommendation": row["recommendation"],
            "rationale": row["rationale"],
            "resolution": row["resolution"],
            "target_player_tag": row["target_player_tag"],
            "target_player_name": row["target_player_name"],
            "source_signal_key": row["source_signal_key"],
            "source_event_key": row["source_event_key"],
            "state_keys": sorted(state.keys()),
        }
        upsert_event_rollup(
            rollup_key=f"case_history:{row['case_id']}",
            rollup_type=ROLLUP_CASE_HISTORY,
            scope="leadership",
            subject_type=row["subject_type"],
            subject_key=row["subject_key"],
            season_id=None,
            period_start=row["opened_at"],
            period_end=row["resolved_at"] or row["updated_at"],
            source_event_count=1 if row["source_event_key"] else 0,
            summary=summary,
            conn=conn,
        )
        written += 1
    return written


@managed_connection
def write_event_rollups_for_retention(
    *,
    cutoff: str | None = None,
    now: str | None = None,
    retention_days: int = EVENT_STREAM_RETENTION_DAYS,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    clean_cutoff = _clean_text(cutoff) or _retention_cutoff(retention_days=retention_days, now=now)
    stats = {
        ROLLUP_MEMBER_90D: _write_member_90d_rollups(conn, cutoff=clean_cutoff),
        ROLLUP_WAR_CYCLE: _write_war_cycle_rollups(conn, cutoff=clean_cutoff),
        ROLLUP_CASE_HISTORY: _write_case_history_rollups(conn, cutoff=clean_cutoff),
    }
    conn.commit()
    return {
        "cutoff": clean_cutoff,
        "written": stats,
        "total_written": sum(stats.values()),
    }


@managed_connection
def prune_event_stream_with_rollups(
    *,
    retention_days: int = EVENT_STREAM_RETENTION_DAYS,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    cutoff = _retention_cutoff(retention_days=retention_days, now=now)
    old_count = conn.execute(
        "SELECT COUNT(*) AS count FROM game_event_stream WHERE observed_at < ?",
        (cutoff,),
    ).fetchone()["count"]
    if not old_count:
        return {
            "cutoff": cutoff,
            "old_event_count": 0,
            "pruned": 0,
            "rollups": {"total_written": 0, "written": {}},
        }
    rollups = write_event_rollups_for_retention(
        cutoff=cutoff,
        retention_days=retention_days,
        conn=conn,
    )
    cursor = conn.execute(
        "DELETE FROM game_event_stream WHERE observed_at < ?",
        (cutoff,),
    )
    conn.commit()
    return {
        "cutoff": cutoff,
        "old_event_count": int(old_count or 0),
        "pruned": cursor.rowcount,
        "rollups": rollups,
    }
