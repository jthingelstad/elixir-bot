"""Adaptive budget poll scheduler (runtime.md §4; architecture §15).

Temperature per player (heat counter: battles → 3/hot, roster delta →
max(heat,2)/warm, decay −1 per tick, floor 0/cold). The per-tick budget covers
per-player endpoints only; clan/riverrace calls are fixed overhead outside it.
Fairness floors guarantee every member is polled within a bounded window
regardless of temperature.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine.db import canon_tag, utcnow

POLL_BUDGET_PER_TICK = 40  # runtime.md §8, ratified

# Cadence minutes per (endpoint, temperature); floor = fairness floor.
CADENCE = {
    "battlelog": {"hot": 0, "warm": 30, "cold": 120, "floor": 360},
    "profile": {"hot": 60, "warm": 240, "cold": 720, "floor": 1440},
}
_LAST_COL = {"battlelog": "last_battlelog_poll", "profile": "last_profile_poll"}


def _temp(heat: int) -> str:
    if heat >= 3:
        return "hot"
    if heat >= 1:
        return "warm"
    return "cold"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:  # engine convention: suffixless timestamps are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def update_heat(conn, player_tag, *, new_battles=False, roster_delta=False, now=None):
    """Heat rules (runtime.md §4): battles → 3; roster delta → max(heat, 2)."""
    tag = canon_tag(player_tag)
    now = now or utcnow()
    row = conn.execute(
        "SELECT heat FROM poll_state WHERE player_tag = ?", (tag,)
    ).fetchone()
    heat = int(row["heat"]) if row else 0
    if new_battles:
        heat = 3
    elif roster_delta:
        heat = max(heat, 2)
    conn.execute(
        """INSERT INTO poll_state (player_tag, temperature, heat, last_battle_seen,
                                   last_roster_delta, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(player_tag) DO UPDATE SET
               temperature = excluded.temperature, heat = excluded.heat,
               last_battle_seen = COALESCE(excluded.last_battle_seen,
                                           poll_state.last_battle_seen),
               last_roster_delta = COALESCE(excluded.last_roster_delta,
                                            poll_state.last_roster_delta),
               updated_at = excluded.updated_at""",
        (
            tag, _temp(heat), heat,
            now if new_battles else None,
            now if roster_delta else None,
            now,
        ),
    )


def decay_all(conn, now=None) -> None:
    """Each tick without evidence of activity: heat −1, floor 0. Called once
    per tick BEFORE update_heat re-heats the players whose polls found news."""
    now = now or utcnow()
    conn.execute(
        """UPDATE poll_state SET
               heat = MAX(0, heat - 1),
               temperature = CASE WHEN heat - 1 >= 3 THEN 'hot'
                                  WHEN heat - 1 >= 1 THEN 'warm'
                                  ELSE 'cold' END,
               updated_at = ?""",
        (now,),
    )


def seed_new_members(conn, roster_tags, now=None) -> int:
    """New members (no poll_state row) seed warm so first baselines land
    quickly — first-sight emits nothing, so the seed poll is silent (§8)."""
    now = now or utcnow()
    seeded = 0
    for raw in roster_tags:
        tag = canon_tag(raw)
        cur = conn.execute(
            """INSERT OR IGNORE INTO poll_state (player_tag, temperature, heat, updated_at)
               VALUES (?, 'warm', 2, ?)""",
            (tag, now),
        )
        seeded += cur.rowcount
    return seeded


def note_polled(conn, player_tag, endpoint: str, now=None) -> None:
    col = _LAST_COL[endpoint]
    conn.execute(
        f"UPDATE poll_state SET {col} = ?, updated_at = ? WHERE player_tag = ?",
        (now or utcnow(), now or utcnow(), canon_tag(player_tag)),
    )


def plan(conn, now=None, budget: int = POLL_BUDGET_PER_TICK) -> list[tuple[str, str]]:
    """Spend the per-player budget hottest-first with fairness floors
    (runtime.md §4): priority = floor-starved first (most overdue), then heat,
    then longest-overdue. Returns ordered [(endpoint, player_tag)]."""
    now = now or utcnow()
    now_dt = _parse(now)
    rows = conn.execute(
        """SELECT ps.* FROM poll_state ps
           WHERE EXISTS (SELECT 1 FROM clan_memberships cm
                         WHERE cm.player_tag = ps.player_tag AND cm.left_at IS NULL)"""
    ).fetchall()
    candidates: list[tuple[int, int, float, str, str]] = []
    for r in rows:
        temp = _temp(int(r["heat"]))
        for endpoint, cad in CADENCE.items():
            last = _parse(r[_LAST_COL[endpoint]])
            overdue_min = (
                (now_dt - last).total_seconds() / 60.0 if last else float("inf")
            )
            due = overdue_min >= cad[temp]
            starved = overdue_min >= cad["floor"]
            if due or starved:
                candidates.append(
                    (1 if starved else 0, int(r["heat"]), overdue_min,
                     endpoint, r["player_tag"])
                )
    candidates.sort(key=lambda c: (-c[0], -c[1], -c[2], c[3], c[4]))
    return [(c[3], c[4]) for c in candidates[: max(0, budget)]]
