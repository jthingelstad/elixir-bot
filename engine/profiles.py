"""Playstyle profiles — ranked-and-profiles.md §2.3 (D2/D4 ratified).

A computed READ over player_daily_battle_rollups (no new pipeline, no cache):
per-mode mix over a trailing window plus deterministic identity labels — the
same label for the same numbers, testable, and trustworthy as *facts* for the
Editor-era composer (grounded specifics, never invented color).

Friendlies count toward totals but never drive a primary identity (sparring
is context, not a playstyle); a friendly-dominated mix reads `all-rounder`.
Display naming: "Ranked" (the mid-2025 rename), never Path of Legends.
"""

from __future__ import annotations

from datetime import date, timedelta

PRIMARY_SHARE = 0.35      # spec §2.3: largest mode share must reach 35%…
PRIMARY_MIN_BATTLES = 12  # …with at least 12 battles in the window
QUIET_BELOW = 8           # under 8 total battles → 'quiet'

MODE_LABELS = {
    "ladder": "ladder regular",
    "war": "war-first",
    "ranked": "Ranked grinder",
    "two_v_two": "2v2 duoist",
    "special_event": "event explorer",
}
MODE_DISPLAY = {
    "ladder": "Trophy Road", "war": "war", "ranked": "Ranked",
    "two_v_two": "2v2", "special_event": "events", "friendly": "friendlies",
    "tournament": "tournaments",
}


def player_mode_profile(conn, tag: str, days: int = 28, today: str | None = None) -> dict:
    """Mode mix + identity over the trailing `days` Chicago days.

    Returns {total_battles, window_days, modes: {mode: {battles, wins, share}},
    identity, secondary, duo_partners} — every number derived from rollup rows.
    `today` (ISO date) pins the window end for determinism in tests."""
    end = date.fromisoformat(today) if today else date.today()
    start = (end - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT mode_group, SUM(battles) AS battles, SUM(wins) AS wins
           FROM player_daily_battle_rollups
           WHERE player_tag = ? AND battle_date >= ? AND battle_date <= ?
           GROUP BY mode_group ORDER BY battles DESC""",
        (tag, start, end.isoformat()),
    ).fetchall()
    total = sum(r["battles"] or 0 for r in rows)
    modes = {
        r["mode_group"]: {
            "battles": r["battles"] or 0,
            "wins": r["wins"] or 0,
            "share": round((r["battles"] or 0) / total, 3) if total else 0.0,
        }
        for r in rows
        if r["mode_group"]
    }
    identity, secondary = _identity(modes, total)
    return {
        "total_battles": total,
        "window_days": days,
        "modes": modes,
        "identity": identity,
        "secondary": secondary,
        "duo_partners": duo_partners(conn, tag, start, end.isoformat()),
    }


def _identity(modes: dict, total: int) -> tuple[str, list[str]]:
    if total < QUIET_BELOW:
        return "quiet", []
    labeled = [
        (m, v) for m, v in modes.items()
        if m in MODE_LABELS  # friendlies/tournaments never drive identity
    ]
    labeled.sort(key=lambda kv: -kv[1]["battles"])
    primary = None
    if labeled:  # only the largest identity-eligible mode can be primary
        mode, v = labeled[0]
        if v["share"] >= PRIMARY_SHARE and v["battles"] >= PRIMARY_MIN_BATTLES:
            primary = mode
    if primary is None:
        return "all-rounder", []
    secondary = [
        MODE_LABELS[m] for m, v in labeled
        if m != primary and v["share"] >= 0.20 and v["battles"] >= QUIET_BELOW
    ]
    return MODE_LABELS[primary], secondary


def duo_partners(conn, tag: str, start: str, end: str, limit: int = 3) -> list[dict]:
    """Top 2v2 teammates over the window (D3 — needs teammate_tag rows; empty
    until the ingest column has data). Dates compare against battle_time's
    CR-compact form via the date prefix."""
    rows = conn.execute(
        """SELECT b.teammate_tag, COALESCE(p.display_name, p.current_name) AS name, COUNT(*) AS games,
                  SUM(b.outcome = 'W') AS wins
           FROM battle_events b
           LEFT JOIN players p ON p.player_tag = b.teammate_tag
           WHERE b.player_tag = ? AND b.mode_group = 'two_v_two'
             AND b.teammate_tag IS NOT NULL
             AND substr(b.battle_time, 1, 8) >= ?
           GROUP BY b.teammate_tag ORDER BY games DESC LIMIT ?""",
        (tag, start.replace("-", ""), limit),
    ).fetchall()
    return [
        {"tag": r["teammate_tag"], "name": r["name"],
         "games": r["games"], "wins": r["wins"] or 0}
        for r in rows
    ]


def playstyle_line(profile: dict) -> str | None:
    """One grounded sentence for compose context ('EddiePlayz has played 31 of
    his last 40 battles in 2v2'). None when there's nothing worth saying."""
    total = profile.get("total_battles") or 0
    if total < QUIET_BELOW:
        return None
    modes = profile.get("modes") or {}
    top_mode = max(modes, key=lambda m: modes[m]["battles"], default=None)
    if not top_mode:
        return None
    v = modes[top_mode]
    disp = MODE_DISPLAY.get(top_mode, top_mode)
    line = (f"{v['battles']} of their last {total} battles "
            f"(past {profile.get('window_days', 28)} days) were {disp}")
    if v["battles"]:
        line += f", winning {v['wins']}"
    duos = profile.get("duo_partners") or []
    if top_mode == "two_v_two" and duos and duos[0].get("name"):
        line += f"; most often teaming with {duos[0]['name']}"
    return line + "."
