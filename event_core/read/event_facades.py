"""v5-native replacements for the legacy ``game_event_stream`` high-level reads.

These facades answer the old prompt-context questions — recent observations,
windowed counts, per-player history, and game-mode pulse — directly from the v5
projection DB (``elixir-v5.db``: the ``detections`` and ``battle_telemetry``
projections), so callers can migrate off ``storage.event_stream`` /
``game_event_stream`` (which lived in the soon-to-be-retired ``elixir.db``) one
site at a time.

Design notes:
- Detections are signal-grain. The old ``event_class`` discriminator (signal vs
  battle) does not apply to ``detections``; battle activity is served separately
  by :func:`summarize_battle_modes` over ``battle_telemetry``.
- ``detections.occurred_at`` and ``battle_telemetry.battle_time`` are both stored
  in Clash-Royale time format (``YYYYMMDDThhmmss.000Z``), which is lexically
  ordered, so window cutoffs are compared as CR-format strings.
- Reads are best-effort: a missing projection table (e.g. before first ingest)
  yields an empty result rather than raising, matching the old prompt-context
  contract where absent observations simply meant "nothing to report".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from event_core import config
from event_core import db as ec_db
from event_core.domain.player import canon_tag
from storage.game_modes import mode_group_label

# Mirror the legacy EVENT_STREAM_WINDOWS so windowed-summary callers are unchanged.
DETECTION_WINDOWS = (7, 28, 56, 90)
DETECTION_RETENTION_DAYS = 90


def _now_dt(now: str | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    text = str(now).strip()
    try:
        if "-" not in text and "T" in text:  # CR format YYYYMMDDThhmmss...
            return datetime.strptime(text[:15], "%Y%m%dT%H%M%S")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _cr(dt: datetime) -> str:
    """Render a datetime as a CR-comparable cutoff string (YYYYMMDDThhmmss)."""
    return dt.strftime("%Y%m%dT%H%M%S")


def _cutoff_cr(now_dt: datetime, days: int) -> str:
    return _cr(now_dt - timedelta(days=int(days)))


def _run(conn: Optional[sqlite3.Connection], fn, default):
    """Run ``fn(conn)`` against the projections DB, best-effort.

    Uses the supplied connection, or opens (and closes) a short-lived one. A
    missing projection table returns ``default`` instead of raising.
    """
    own = conn is None
    c = conn
    try:
        if own:
            c = ec_db.connect(config.PROJECTIONS_DB)
        return fn(c)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return default
        raise
    finally:
        if own and c is not None:
            c.close()


def _detection_filters(
    *,
    cutoff: str | None = None,
    scope: str | None = None,
    detection_type: str | None = None,
    subject_key: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if cutoff:
        clauses.append("occurred_at >= ?")
        args.append(cutoff)
    if scope:
        clauses.append("scope = ?")
        args.append(scope)
    if detection_type:
        clauses.append("detection_type = ?")
        args.append(detection_type)
    if subject_key:
        clauses.append("subject_tag = ?")
        args.append(canon_tag(subject_key))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, args


def _row_to_event(row: sqlite3.Row) -> dict:
    item = dict(row)
    payload = item.get("payload_json")
    try:
        parsed = json.loads(payload) if payload else {}
    except (TypeError, ValueError):
        parsed = {}
    subject_tag = item.get("subject_tag")
    return {
        "event_key": item.get("dedup_key"),
        "event_type": item.get("detection_type"),
        "source_detector": item.get("detector"),
        "scope": item.get("scope"),
        "subject_type": "member" if subject_tag else None,
        "subject_key": subject_tag,
        "observed_at": item.get("occurred_at"),
        "occurred_at": item.get("occurred_at"),
        "payload_json": parsed,
    }


def summarize_event_windows(
    *,
    windows: tuple[int, ...] = DETECTION_WINDOWS,
    scope: str | None = None,
    subject_type: str | None = None,  # accepted for call-site compatibility; unused
    subject_key: str | None = None,
    event_class: str | None = None,  # accepted for call-site compatibility; unused
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Compact detection counts by type/scope for standard lookback windows.

    The v5-native replacement for ``summarize_events_by_window``. ``subject_type``
    and ``event_class`` are accepted so reader call sites migrate unchanged, but
    detections are signal-grain and keyed by ``subject_tag``, so only
    ``subject_key`` (a player tag) narrows the result.
    """
    now_dt = _now_dt(now)

    def _query(c: sqlite3.Connection) -> dict:
        summary: dict[str, dict] = {}
        for days in windows:
            where, args = _detection_filters(
                cutoff=_cutoff_cr(now_dt, days), scope=scope, subject_key=subject_key
            )
            total = c.execute(
                f"SELECT COUNT(*) AS count FROM detections {where}", args
            ).fetchone()["count"]
            by_type_rows = c.execute(
                f"SELECT detection_type, COUNT(*) AS count FROM detections {where} "
                "GROUP BY detection_type ORDER BY count DESC, detection_type ASC",
                args,
            ).fetchall()
            by_scope_rows = c.execute(
                f"SELECT scope, COUNT(*) AS count FROM detections {where} "
                "GROUP BY scope ORDER BY count DESC, scope ASC",
                args,
            ).fetchall()
            summary[f"{int(days)}d"] = {
                "days": int(days),
                "total": int(total or 0),
                "by_type": {row["detection_type"]: int(row["count"] or 0) for row in by_type_rows},
                "by_scope": {row["scope"]: int(row["count"] or 0) for row in by_scope_rows},
            }
        return summary

    return _run(conn, _query, {f"{int(d)}d": {"days": int(d), "total": 0, "by_type": {}, "by_scope": {}} for d in windows})


def list_recent_events(
    *,
    days: int = DETECTION_RETENTION_DAYS,
    since: str | None = None,
    scope: str | None = None,
    event_type: str | None = None,
    subject_type: str | None = None,  # accepted for call-site compatibility; unused
    subject_key: str | None = None,
    event_class: str | None = None,  # accepted for call-site compatibility; unused
    limit: int = 100,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Recent detections, newest first, in the legacy event-row shape.

    The v5-native replacement for ``list_recent_events``. ``event_type`` filters
    on ``detection_type``; ``subject_key`` filters on ``subject_tag``.
    """
    cutoff = _cr(_now_dt(since)) if since else _cutoff_cr(_now_dt(now), days)

    def _query(c: sqlite3.Connection) -> list[dict]:
        where, args = _detection_filters(
            cutoff=cutoff, scope=scope, detection_type=event_type, subject_key=subject_key
        )
        rows = c.execute(
            f"SELECT * FROM detections {where} "
            "ORDER BY occurred_at DESC, dedup_key DESC LIMIT ?",
            (*args, max(1, int(limit or 100))),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    return _run(conn, _query, [])


def list_subject_events(
    subject_type: str,
    subject_key: str,
    *,
    days: int = DETECTION_RETENTION_DAYS,
    limit: int = 100,
    scope: str | None = None,
    event_class: str | None = None,
    now: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Per-subject detection history (the v5-native ``list_subject_events``)."""
    return list_recent_events(
        days=days,
        scope=scope,
        subject_key=subject_key,
        limit=limit,
        now=now,
        conn=conn,
    )


def _win_rate(wins: int, decided: int) -> float | None:
    return round(wins / decided, 3) if decided else None


def summarize_battle_modes(
    *,
    windows: tuple[int, ...] = (7, 28),
    now: str | None = None,
    top_members: int = 3,
    min_battles: int = 3,
    subject_key: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Per-mode battle activity from ``battle_telemetry`` (game-mode pulse).

    The v5-native replacement for ``summarize_battle_modes``. Member names come
    from the ``members`` table, which lives in the same unified store as the
    battle telemetry (db.DB_PATH == config.PROJECTIONS_DB).
    """
    now_dt = _now_dt(now)
    member_filter = canon_tag(subject_key) if subject_key else None

    def _query(c: sqlite3.Connection) -> dict:
        result: dict[str, dict] = {}
        for days in windows:
            cutoff = _cutoff_cr(now_dt, days)
            where = ["b.battle_time >= ?", "b.player_tag IS NOT NULL"]
            params: list = [cutoff]
            if member_filter:
                where.append("b.player_tag = ?")
                params.append(member_filter)
            rows = c.execute(
                f"""
                SELECT b.mode_group AS mode,
                       b.player_tag AS tag,
                       m.current_name AS name,
                       COUNT(*) AS battles,
                       SUM(CASE WHEN b.outcome = 'W' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN b.outcome = 'L' THEN 1 ELSE 0 END) AS losses
                FROM battle_telemetry b
                LEFT JOIN members m ON m.player_tag = b.player_tag
                WHERE {' AND '.join(where)}
                GROUP BY b.mode_group, b.player_tag
                """,
                tuple(params),
            ).fetchall()
            modes: dict[str, dict] = {}
            for row in rows:
                mode = row["mode"] or "other"
                bucket = modes.setdefault(mode, {"battles": 0, "wins": 0, "losses": 0, "members": []})
                wins = int(row["wins"] or 0)
                losses = int(row["losses"] or 0)
                battles = int(row["battles"] or 0)
                bucket["battles"] += battles
                bucket["wins"] += wins
                bucket["losses"] += losses
                bucket["members"].append({
                    "tag": row["tag"],
                    "name": row["name"],
                    "battles": battles,
                    "wins": wins,
                    "losses": losses,
                })
            summary_modes: dict[str, dict] = {}
            for mode, bucket in modes.items():
                if bucket["battles"] < max(1, int(min_battles)):
                    continue
                members = sorted(
                    bucket["members"], key=lambda member: (-member["battles"], -member["wins"])
                )[: max(0, int(top_members))]
                for member in members:
                    member["win_rate"] = _win_rate(member["wins"], member["wins"] + member["losses"])
                summary_modes[mode] = {
                    "label": mode_group_label(mode),
                    "battles": bucket["battles"],
                    "active_members": len(bucket["members"]),
                    "wins": bucket["wins"],
                    "losses": bucket["losses"],
                    "win_rate": _win_rate(bucket["wins"], bucket["wins"] + bucket["losses"]),
                    "top_members": members,
                }
            ordered = dict(sorted(summary_modes.items(), key=lambda kv: -kv[1]["battles"]))
            result[f"{int(days)}d"] = {"days": int(days), "modes": ordered}
        return result

    return _run(conn, _query, {f"{int(d)}d": {"days": int(d), "modes": {}} for d in windows})
