"""get_member_war_attendance's last_4_weeks window must count war weeks whose
created_date is stored in EITHER format (QA H5).

war_weeks.created_date is a mix of compact '20260629T093706.000Z' and ISO
'2026-07-06'. A raw string compare against a compact cutoff drops the ISO rows
(the two newest weeks) because '-' sorts below digits — understating recent
attendance that feeds inactivity/kick reads.
"""
from __future__ import annotations

import db


def test_recent_war_weeks_count_both_date_formats():
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', '2026-06-01', '2026-07-11', 1)"
        )
        conn.execute(
            "INSERT INTO players (player_tag, current_name, display_name, first_seen_at, last_seen_at) "
            "VALUES ('#WK1', 'War Guy', 'War Guy', '2026-06-01', '2026-07-11')"
        )
        conn.execute("INSERT INTO war_seasons (season_id, started_at) VALUES (140, '2026-06-01')")
        # Two recent weeks, DIFFERENT date formats — both within 28 days of 2026-07-11.
        conn.execute("INSERT INTO war_weeks (season_id, section_index, created_date) VALUES (140, 0, '20260629T093706.000Z')")
        conn.execute("INSERT INTO war_weeks (season_id, section_index, created_date) VALUES (140, 1, '2026-07-06')")  # ISO — the buggy row
        conn.commit()

        att = db.get_member_war_attendance("#WK1", season_id=140, conn=conn)
        # Both recent weeks must be counted (buggy raw compare would drop the ISO one).
        assert att["last_4_weeks"]["total_races"] == 2
    finally:
        conn.close()
