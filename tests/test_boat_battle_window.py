"""get_clan_boat_battle_record must actually scope to the last N war sections
(QA H9) — it used to count ALL boat battles ever while labelling them "last N
wars".
"""

from __future__ import annotations

import db


def _boat(conn, season, section, n, outcome, start_idx=0):
    for i in range(n):
        conn.execute(
            "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, "
            "is_war, battle_type, outcome, season_id, section_index) "
            "VALUES (?, '#B1', ?, '2026-07-11T00:00:00Z', 1, 'boatBattle', ?, ?, ?)",
            (
                f"{season}-{section}-{outcome}-{start_idx + i}",
                f"2026070{section}T1{i:02d}000.000Z",
                outcome,
                season,
                section,
            ),
        )


def test_boat_record_scopes_to_recent_wars():
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) VALUES ('#B1','B','2026-06-01','2026-07-11')"
        )
        # older war section: 5 wins
        _boat(conn, 133, 1, 5, "W")
        # most recent war section: 2 wins, 1 loss
        _boat(conn, 134, 2, 2, "W")
        _boat(conn, 134, 2, 1, "L", start_idx=100)
        conn.commit()

        one = db.get_clan_boat_battle_record(weeks=1, conn=conn)
        assert one["boat_battles"] == 3 and one["wins"] == 2 and one["losses"] == 1
        # unit is a war WEEK (section within a season), reported as season+week
        assert one["weeks_covered"] == [
            {"season_id": 134, "week": 3, "section_index": 2}
        ]

        both = db.get_clan_boat_battle_record(weeks=5, conn=conn)
        assert both["boat_battles"] == 8  # both weeks now in window
        assert len(both["weeks_covered"]) == 2
    finally:
        conn.close()
