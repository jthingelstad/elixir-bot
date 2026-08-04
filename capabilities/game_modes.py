"""Canonical clan game-mode intelligence capability.

This is the shared semantic boundary between the battle/event store and every
consumer that needs to understand how the clan is playing. It returns facts,
not prose or Discord-specific presentation.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from capabilities.contracts import ClanGameModesResult, ClanGameModeWindowsResult
from db import get_connection
from engine.normalize import humanize_game_mode, ranked_league_name
from engine.readiness import generation_snapshot
from storage import player as player_storage

CAPABILITY_ID = "clan_game_modes"
CONTRACT_VERSION = 1


def _ranked_standings(profiles: list[dict]) -> list[dict]:
    standings = []
    for profile in profiles:
        league = profile.get("league_number")
        if league is None:
            continue
        standings.append(
            {
                "member_ref": profile.get("member_ref")
                or profile.get("name")
                or profile.get("tag"),
                "player_tag": profile.get("player_tag") or profile.get("tag"),
                "league": league,
                "league_name": ranked_league_name(league),
                "rating": profile.get("ranked_trophies"),
            }
        )
    return standings


def _duo_pairs(conn: sqlite3.Connection | None, *, days: int, limit: int) -> list[dict]:
    if conn is None:
        return []
    rows = conn.execute(
        """SELECT COALESCE(p1.display_name, p1.current_name) AS player,
                  COALESCE(p2.display_name, p2.current_name) AS teammate,
                  COUNT(*) AS battles, SUM(b.outcome = 'W') AS wins
           FROM battle_events b
           JOIN players p1 ON p1.player_tag = b.player_tag
           JOIN players p2 ON p2.player_tag = b.teammate_tag
           WHERE b.teammate_tag IS NOT NULL
             AND b.battle_time >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
           GROUP BY b.player_tag, b.teammate_tag
           HAVING battles >= 2
           ORDER BY battles DESC LIMIT ?""",
        (f"-{days} days", limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _get_clan_game_modes(
    *,
    days: int = 30,
    mode_group: str | None = None,
    limit: int = 10,
    top_members: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> ClanGameModesResult:
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
        "data_generation": generation_snapshot(conn),
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


def get_clan_game_modes(
    *,
    days: int = 30,
    mode_group: str | None = None,
    limit: int = 10,
    top_members: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> ClanGameModesResult:
    """Read one game-mode view from a single materialized DB snapshot."""
    if conn is not None:
        return _get_clan_game_modes(
            days=days,
            mode_group=mode_group,
            limit=limit,
            top_members=top_members,
            conn=conn,
        )
    active = get_connection()
    try:
        active.execute("BEGIN")
        return _get_clan_game_modes(
            days=days,
            mode_group=mode_group,
            limit=limit,
            top_members=top_members,
            conn=active,
        )
    finally:
        active.close()


def _get_clan_game_mode_windows(
    *,
    windows: tuple[int, ...] = (7, 28),
    limit: int = 10,
    top_members: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> ClanGameModeWindowsResult:
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
        "data_generation": generation_snapshot(conn),
        "windows": snapshots,
    }


def get_clan_game_mode_windows(
    *,
    windows: tuple[int, ...] = (7, 28),
    limit: int = 10,
    top_members: int = 3,
    conn: Optional[sqlite3.Connection] = None,
) -> ClanGameModeWindowsResult:
    """Read comparable windows from one materialized DB snapshot."""
    if conn is not None:
        return _get_clan_game_mode_windows(
            windows=windows,
            limit=limit,
            top_members=top_members,
            conn=conn,
        )
    active = get_connection()
    try:
        active.execute("BEGIN")
        return _get_clan_game_mode_windows(
            windows=windows,
            limit=limit,
            top_members=top_members,
            conn=active,
        )
    finally:
        active.close()


__all__ = [
    "CAPABILITY_ID",
    "CONTRACT_VERSION",
    "get_clan_game_mode_windows",
    "get_clan_game_modes",
]


# --------------------------------------------------------------- named modes
#
# The clan's most-played special event was invisible until 2026-08-04. Everything
# here grouped by `mode_group`, so Chaos_1v1_Draft (557 clan battles),
# Crazy_Arena, Showdown_Friendly and three others collapsed into one bucket
# called "special_event". A member asked three times in four minutes how he was
# doing in Ken's C.H.A.O.S Draft League and Elixir correctly answered that its
# tools could not tell him — the data had 134 of his battles at 57%.
#
# Members name the mode the way the GAME names it ("chaos", "C.H.A.O.S Draft
# League"); the API names it `Chaos_1v1_Draft`. Resolution has to bridge that,
# because nobody is going to type `Challenge_AllCards_EventDeck_NoSet`.

_MODE_MIN_BATTLES = 3


def _mode_words(text: str) -> set[str]:
    """Comparable words: "C.H.A.O.S Draft League" -> {chaos, draft, league}.

    Word sets, not one squashed string. A substring test looked simpler and was
    wrong: "chaosdraftleague" does not contain "chaos1v1draft", but it DOES
    contain "draft", so `Draft_Competitive` won and the member's own phrasing
    resolved to the wrong mode.
    """
    text = str(text or "").lower()
    # Strip possessives FIRST. "Ken's C.H.A.O.S" otherwise tokenizes to
    # ken | s | c | h | a | o | s and the trailing 's' from "Ken's" joins the
    # acronym run as "schaos", so the member's own phrasing missed the mode.
    for suffix in ("'s", "\u2019s"):
        text = text.replace(suffix, " ")
    raw = "".join(ch if ch.isalnum() else " " for ch in text)
    # "C.H.A.O.S" becomes "c h a o s" — rejoin runs of single letters.
    words, run = [], []
    for token in raw.split():
        if len(token) == 1:
            run.append(token)
            continue
        if run:
            words.append("".join(run))
            run = []
        words.append(token)
    if run:
        words.append("".join(run))
    return {w for w in words if w and w not in _MODE_STOPWORDS}


_MODE_STOPWORDS = {"the", "a", "s", "mode", "league", "challenge", "event", "1v1", "2v2"}


def resolve_game_mode(
    query: str,
    *,
    days: int = 90,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    """Member wording -> the `game_mode_name` values actually in the data.

    Matches against both the raw API key and its humanized label, so "chaos",
    "Chaos Draft" and the game's own "Ken's C.H.A.O.S Draft League" all land on
    `Chaos_1v1_Draft`. Returns every match, busiest first — an ambiguous query
    should show the caller the choices rather than silently pick one.
    """
    active = conn or get_connection()
    try:
        rows = active.execute(
            "SELECT game_mode_name, COUNT(*) n FROM battle_events "
            "WHERE game_mode_name IS NOT NULL "
            "AND battle_time >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
            "GROUP BY game_mode_name ORDER BY n DESC",
            (f"-{max(1, int(days))} days",),
        ).fetchall()
    finally:
        if conn is None:
            active.close()

    wanted = _mode_words(query)
    if not wanted:
        return []
    scored = []
    for row in rows:
        raw = str(row[0])
        candidate = _mode_words(raw) | _mode_words(humanize_game_mode(raw) or "")
        overlap = wanted & candidate
        if not overlap:
            continue
        # Rank by how much of the QUERY is accounted for, then by how little
        # noise the candidate adds, then by how busy the mode is (rows are
        # already busiest-first).
        scored.append((len(overlap) / len(wanted), -len(candidate - wanted), raw))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [raw for _, _, raw in scored]


def _mode_record(rows) -> dict:
    wins = sum(1 for r in rows if r["outcome"] == "W")
    losses = sum(1 for r in rows if r["outcome"] == "L")
    played = len(rows)
    return {
        "battles": played,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / played * 100, 1) if played else None,
    }


def list_game_modes(
    *,
    days: int = 90,
    limit: int = 25,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Every mode the clan has actually played, busiest first.

    Discovery matters as much as lookup: a member cannot ask about a mode whose
    name they do not know, and the API names are unguessable
    (`Challenge_AllCards_EventDeck_NoSet`). This answers "what do you track?"
    with the modes there is real data for, never a hardcoded list.
    """
    active = conn or get_connection()
    try:
        rows = active.execute(
            "SELECT game_mode_name, mode_group, COUNT(*) n, "
            "COUNT(DISTINCT player_tag) players, "
            "SUM(CASE WHEN outcome = 'W' THEN 1 ELSE 0 END) wins, "
            "MAX(battle_time) last_played "
            "FROM battle_events WHERE game_mode_name IS NOT NULL "
            "AND battle_time >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
            "GROUP BY game_mode_name, mode_group ORDER BY n DESC LIMIT ?",
            (f"-{max(1, int(days))} days", max(1, int(limit))),
        ).fetchall()
        modes = [
            {
                "mode": str(r["game_mode_name"]),
                "label": humanize_game_mode(str(r["game_mode_name"])),
                "group": r["mode_group"],
                "battles": int(r["n"]),
                "players": int(r["players"]),
                "clan_win_rate": round(int(r["wins"]) / int(r["n"]) * 100, 1) if r["n"] else None,
                "last_played": r["last_played"],
            }
            for r in rows
        ]
        return {
            "capability": CAPABILITY_ID,
            "contract_version": CONTRACT_VERSION,
            "data_generation": generation_snapshot(active),
            "listing": True,
            "window_days": int(days),
            "modes": modes,
        }
    finally:
        if conn is None:
            active.close()


def get_game_mode_performance(
    mode_query: Optional[str] = None,
    *,
    player_tag: Optional[str] = None,
    days: int = 90,
    limit: int = 15,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """One named mode: a member's record in it, and the clan leaderboard.

    `unresolved` with `available_modes` when the query matches nothing — the
    caller should offer the real names rather than assert the mode does not
    exist. Returns facts only.
    """
    if not (mode_query or "").strip():
        # No mode named: the question is "what is there?", not "how did I do".
        return list_game_modes(days=days, conn=conn)
    query = str(mode_query).strip()
    active = conn or get_connection()
    try:
        matches = resolve_game_mode(query, days=days, conn=active)
        if not matches:
            known = [
                {"mode": str(r[0]), "label": humanize_game_mode(str(r[0])), "battles": int(r[1])}
                for r in active.execute(
                    "SELECT game_mode_name, COUNT(*) n FROM battle_events "
                    "WHERE game_mode_name IS NOT NULL "
                    "AND battle_time >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
                    "GROUP BY game_mode_name ORDER BY n DESC LIMIT 20",
                    (f"-{max(1, int(days))} days",),
                ).fetchall()
            ]
            return {
                "capability": CAPABILITY_ID,
                "contract_version": CONTRACT_VERSION,
                "query": mode_query,
                "resolved": False,
                "available_modes": known,
            }

        mode = matches[0]
        window = (f"-{max(1, int(days))} days",)
        rows = active.execute(
            "SELECT b.player_tag, b.outcome, b.battle_time, p.display_name "
            "FROM battle_events b LEFT JOIN players p ON p.player_tag = b.player_tag "
            "WHERE b.game_mode_name = ? "
            "AND b.battle_time >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            (mode, window[0]),
        ).fetchall()

        by_member: dict[str, list] = {}
        names: dict[str, str] = {}
        for row in rows:
            by_member.setdefault(row["player_tag"], []).append(row)
            names[row["player_tag"]] = row["display_name"] or row["player_tag"]

        board = [
            {
                "player_tag": tag,
                "name": names.get(tag, tag),
                **_mode_record(member_rows),
            }
            for tag, member_rows in by_member.items()
            if len(member_rows) >= _MODE_MIN_BATTLES
        ]
        # Win rate ranks the board, but volume breaks ties — a 100% on 3 battles
        # should not outrank a 68% on 31.
        board.sort(key=lambda m: (m["win_rate"] or 0, m["battles"]), reverse=True)
        for index, entry in enumerate(board[:limit], start=1):
            entry["rank"] = index

        result = {
            "capability": CAPABILITY_ID,
            "contract_version": CONTRACT_VERSION,
            "data_generation": generation_snapshot(active),
            "resolved": True,
            "query": query,
            "mode": mode,
            "label": humanize_game_mode(mode),
            "also_matched": matches[1:],
            "window_days": int(days),
            "min_battles_for_board": _MODE_MIN_BATTLES,
            "clan": _mode_record(rows),
            "leaderboard": board[:limit],
        }
        if player_tag:
            tag = str(player_tag).upper()
            if not tag.startswith("#"):
                tag = f"#{tag}"
            mine = by_member.get(tag, [])
            mine_entry = next((m for m in board if m["player_tag"] == tag), {})
            result["member"] = {
                "player_tag": tag,
                "name": names.get(tag),
                **_mode_record(mine),
                "rank": mine_entry.get("rank"),
                "ranked_of": len(board),
            }
        return result
    finally:
        if conn is None:
            active.close()
