"""Race-log helpers shared by the war read modules.

v5.1: the logged-race source is war_weeks (was war_races); season inference
matches engine.clock.infer_season_id — a live sectionIndex lower than the
last logged one means a new season started (§16.1: seasons are contiguous).
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def get_latest_logged_race(conn: sqlite3.Connection):
    """Return the most recent war_weeks row (summary column set), or None."""
    return conn.execute(
        "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, "
        "NULL AS total_clans, finish_time "
        "FROM war_weeks ORDER BY season_id DESC, section_index DESC LIMIT 1"
    ).fetchone()


def infer_current_season_id_from_live_state(
    payload, latest_logged_race
) -> Optional[int]:
    """Infer the current season id from a live race projection or payload.

    Accepts both CR-shaped payloads (seasonId/sectionIndex) and the engine's
    race-aspect projection (season_id/section_index).
    """
    payload = payload or {}
    live_season_id = payload.get("seasonId", payload.get("season_id"))
    if live_season_id is not None:
        return live_season_id
    if not latest_logged_race:
        return None
    live_section_index = payload.get("sectionIndex", payload.get("section_index"))
    logged_section_index = latest_logged_race["section_index"]
    if (
        live_section_index is not None
        and logged_section_index is not None
        and live_section_index < logged_section_index
    ):
        return latest_logged_race["season_id"] + 1
    return latest_logged_race["season_id"]
