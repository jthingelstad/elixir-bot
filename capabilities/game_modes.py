"""Canonical clan game-mode intelligence capability.

This is the shared semantic boundary between the battle/event store and every
consumer that needs to understand how the clan is playing. It returns facts,
not prose or Discord-specific presentation.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from db import managed_connection
from engine.normalize import ranked_league_name
from storage import player as player_storage

CAPABILITY_ID = "clan_game_modes"
CONTRACT_VERSION = 1


def _ranked_standings(profiles: list[dict]) -> list[dict]:
    standings = []
    for profile in profiles:
        league = profile.get("league_number")
        if league is None:
            continue
        standings.append({
            "member_ref": profile.get("member_ref") or profile.get("name") or profile.get("tag"),
            "player_tag": profile.get("player_tag") or profile.get("tag"),
            "league": league,
            "league_name": ranked_league_name(league),
            "rating": profile.get("ranked_trophies"),
        })
    return standings


def _duo_pairs(conn: sqlite3.Connection, *, days: int, limit: int) -> list[dict]:
    rows = conn.execute(
        """SELECT COALESCE(p1.display_name, p1.current_name) AS player,
                  COALESCE(p2.display_name, p2.current_name) AS teammate,
                  COUNT(*) AS battles, SUM(b.outcome = 'W') AS wins
           FROM battle_events b
           JOIN players p1 ON p1.player_tag = b.player_tag
           JOIN players p2 ON p2.player_tag = b.teammate_tag
           WHERE b.teammate_tag IS NOT NULL
             AND b.battle_time >= strftime('%Y%m%dT%H%M%S', 'now', ?)
           GROUP BY b.player_tag, b.teammate_tag
           HAVING battles >= 2
           ORDER BY battles DESC LIMIT ?""",
        (f"-{days} days", limit),
    ).fetchall()
    return [dict(row) for row in rows]


@managed_connection
def get_clan_game_modes(
    *,
    days: int = 30,
    mode_group: str | None = None,
    limit: int = 10,
    top_members: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Return one stable, audience-neutral view of clan game-mode activity."""
    days = max(1, int(days or 30))
    limit = max(1, min(int(limit or 10), 50))
    top_members = max(1, min(int(top_members or 3), 10))

    summary = player_storage.get_clan_game_mode_summary(
        days=days,
        mode_group=mode_group,
        limit=limit,
        conn=conn,
    )
    leaders = player_storage.get_clan_mode_top_members(
        days=days,
        per_mode=top_members,
        conn=conn,
    )

    modes = {}
    for group in summary.get("by_group") or []:
        label = group.get("label") or group.get("mode_group") or "Other"
        mode = dict(group)
        mode["top_members"] = list(leaders.get(label) or [])
        modes[group.get("mode_group") or "other"] = mode

    ranked_profiles = list(summary.get("ranked_profiles") or [])
    side_progress = list(summary.get("side_mode_progress") or [])
    leaderboards = list(summary.get("leaderboards") or [])

    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "window_days": days,
        "mode_group": mode_group,
        "sources": ["battle_events", "player_current_state", "game_mode_contexts"],
        "modes": modes,
        "game_modes": list(summary.get("by_game_mode") or []),
        "ranked": {
            "activity": list(summary.get("ranked_activity") or []),
            "profiles": ranked_profiles,
            "standings": _ranked_standings(ranked_profiles),
        },
        "side_modes": {
            "progress": side_progress,
            "leaderboards": leaderboards,
            "progress_tracked": bool(side_progress),
            "leaderboards_tracked": bool(leaderboards),
        },
        "events": {
            "activity": list(summary.get("by_game_mode") or []),
            "participation": list(summary.get("event_participation") or []),
            "badge_completions": list(summary.get("event_badge_completions") or []),
            "active": list(summary.get("active_events") or []),
        },
        "duos": _duo_pairs(conn, days=days, limit=limit),
    }


@managed_connection
def get_clan_game_mode_windows(
    *,
    windows: tuple[int, ...] = (7, 28),
    limit: int = 10,
    top_members: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Return a compact activity view of the capability for several windows.

    Cross-window consumers need comparable mode totals and named leaders, not
    repeated copies of Ranked profiles, event detail, and duo pairs. Those
    remain available from :func:`get_clan_game_modes` for a single window.
    """
    snapshots = {}
    for days in dict.fromkeys(max(1, int(value)) for value in windows):
        snapshot = get_clan_game_modes(
            days=days,
            limit=limit,
            top_members=top_members,
            conn=conn,
        )
        snapshots[f"{days}d"] = {
            "window_days": snapshot["window_days"],
            "sources": snapshot["sources"],
            "modes": snapshot["modes"],
        }
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "windows": snapshots,
    }


__all__ = [
    "CAPABILITY_ID",
    "CONTRACT_VERSION",
    "get_clan_game_mode_windows",
    "get_clan_game_modes",
]
