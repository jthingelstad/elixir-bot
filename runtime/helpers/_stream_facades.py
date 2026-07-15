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

from capabilities import game_modes as game_mode_capability


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
                where = "observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"
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
            where = "observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)"
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
    """Compatibility view over the canonical game-mode capability."""
    result = game_mode_capability.get_clan_game_mode_windows(
        windows=windows,
        top_members=top_members,
    )
    out = {}
    for window, snapshot in (result.get("windows") or {}).items():
        modes = {}
        for mode_key, mode in (snapshot.get("modes") or {}).items():
            if int(mode.get("battles") or 0) < min_battles:
                continue
            modes[mode_key] = {
                "label": mode.get("label"),
                "battles": int(mode.get("battles") or 0),
                "wins": int(mode.get("wins") or 0),
                "losses": int(mode.get("losses") or 0),
                "win_rate": mode.get("win_rate"),
                "active_members": int(mode.get("members_active") or 0),
                "top_members": [
                    {
                        "tag": member.get("player_tag") or member.get("tag"),
                        "name": member.get("member_ref") or member.get("name"),
                        "battles": int(member.get("battles") or 0),
                    }
                    for member in (mode.get("top_members") or [])
                ],
            }
        out[window] = modes
    return out
