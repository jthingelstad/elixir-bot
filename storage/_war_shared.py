"""Race-log helpers shared by war_status and war_ingest.

Both modules need the most recent logged river race and the same
season-id inference from a live war payload. These used to exist as
same-named private copies in each module — with different SELECT
column lists — which made them an easy refactoring trap.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def get_latest_logged_race(conn: sqlite3.Connection):
    """Return the most recent war_races row, or None.

    Selects the full summary column set; callers that only need
    season_id/section_index just read those fields.
    """
    return conn.execute(
        "SELECT season_id, section_index, created_date, our_rank, trophy_change, our_fame, total_clans, finish_time "
        "FROM war_races ORDER BY season_id DESC, section_index DESC, war_race_id DESC LIMIT 1"
    ).fetchone()


def infer_current_season_id_from_live_state(payload, latest_logged_race) -> Optional[int]:
    """Infer the current season id from a live war payload.

    Prefers the payload's own seasonId; otherwise reasons from the last
    logged race — a live sectionIndex lower than the logged one means a
    new season has started.
    """
    live_season_id = (payload or {}).get("seasonId")
    if live_season_id is not None:
        return live_season_id
    if not latest_logged_race:
        return None
    live_section_index = (payload or {}).get("sectionIndex")
    logged_section_index = latest_logged_race["section_index"]
    if (
        live_section_index is not None
        and logged_section_index is not None
        and live_section_index < logged_section_index
    ):
        return latest_logged_race["season_id"] + 1
    return latest_logged_race["season_id"]
