"""Stream read facades — the v5.1 replacement for event_core.read.event_facades.

Same call shapes the content jobs already use (summarize_event_windows,
list_recent_events, summarize_battle_modes), reading the engine streams
(player_events / clan_events / war_events, battle_events) instead of the
retired detections/battle_telemetry tables. Extra legacy kwargs are accepted
and ignored for call-site compatibility, matching the old facade's stance.
"""

from __future__ import annotations

import json
import sqlite3


def _connect() -> sqlite3.Connection:
    from engine import db as engine_db

    return engine_db.connect()


_STREAM_TABLES = ("player_events", "clan_events", "war_events")


def summarize_event_windows(
    *, windows: tuple[int, ...] = (7, 28), scope: str | None = None, **_ignored
) -> dict:
    """Event counts by type per lookback window across the three emitted streams."""
    conn = _connect()
    try:
        out: dict = {}
        for days in windows:
            counts: dict[str, int] = {}
            for table in _STREAM_TABLES:
                where = "observed_at >= datetime('now', ?)"
                params: list = [f"-{days} days"]
                if scope:
                    where += " AND scope = ?"
                    params.append(scope)
                for row in conn.execute(
                    f"SELECT event_type, COUNT(*) AS n FROM {table} WHERE {where} GROUP BY event_type",
                    params,
                ):
                    counts[row["event_type"]] = counts.get(row["event_type"], 0) + int(row["n"])
            out[f"{days}d"] = {"total": sum(counts.values()), "by_type": counts}
        return out
    finally:
        conn.close()


def list_recent_events(
    *,
    days: int = 30,
    scope: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
    **_ignored,
) -> list[dict]:
    """Recent events across the emitted streams, newest first, payload inline."""
    conn = _connect()
    try:
        selects = []
        params: list = []
        for table in _STREAM_TABLES:
            where = "observed_at >= datetime('now', ?)"
            table_params = [f"-{days} days"]
            if scope:
                where += " AND scope = ?"
                table_params.append(scope)
            if event_type:
                where += " AND event_type = ?"
                table_params.append(event_type)
            selects.append(
                f"SELECT event_type, dedup_key, observed_at, timing, scope, payload_json "
                f"FROM {table} WHERE {where}"
            )
            params.extend(table_params)
        rows = conn.execute(
            " UNION ALL ".join(selects) + " ORDER BY observed_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append(
                {
                    "event_type": r["event_type"],
                    "dedup_key": r["dedup_key"],
                    "observed_at": r["observed_at"],
                    "timing": r["timing"],
                    "scope": r["scope"],
                    "payload": payload,
                }
            )
        return out
    finally:
        conn.close()


def summarize_battle_modes(
    *,
    windows: tuple[int, ...] = (7, 28),
    top_members: int = 3,
    min_battles: int = 3,
    **_ignored,
) -> dict:
    """Per-mode battle activity from battle_events (game-mode pulse)."""
    conn = _connect()
    try:
        out: dict = {}
        for days in windows:
            modes: dict[str, dict] = {}
            for row in conn.execute(
                """SELECT mode_group, COUNT(*) AS battles,
                          SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) AS wins,
                          COUNT(DISTINCT player_tag) AS players
                   FROM battle_events
                   WHERE battle_time >= strftime('%Y%m%dT%H%M%S', 'now', ?)
                     AND mode_group IS NOT NULL
                   GROUP BY mode_group ORDER BY battles DESC""",
                (f"-{days} days",),
            ):
                if int(row["battles"]) < min_battles:
                    continue
                top = conn.execute(
                    """SELECT b.player_tag, p.current_name, COUNT(*) AS battles
                       FROM battle_events b LEFT JOIN players p ON p.player_tag = b.player_tag
                       WHERE b.mode_group = ?
                         AND b.battle_time >= strftime('%Y%m%dT%H%M%S', 'now', ?)
                       GROUP BY b.player_tag ORDER BY battles DESC LIMIT ?""",
                    (row["mode_group"], f"-{days} days", top_members),
                ).fetchall()
                modes[row["mode_group"]] = {
                    "battles": int(row["battles"]),
                    "wins": int(row["wins"] or 0),
                    "players": int(row["players"]),
                    "top_members": [
                        {"tag": t["player_tag"], "name": t["current_name"], "battles": int(t["battles"])}
                        for t in top
                    ],
                }
            out[f"{days}d"] = modes
        return out
    finally:
        conn.close()
