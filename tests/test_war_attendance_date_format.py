"""Compatibility reads must count legacy war weeks in either date format (QA H5).

New writes and repaired live data are canonical ISO. This fixture preserves the
read-side defense against a compact archive/import row: a raw string comparison
drops the ISO row because '-' sorts below digits, understating recent attendance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db


def test_recent_war_weeks_count_both_date_formats():
    # Dates must be RELATIVE to now. They were once hardcoded (2026-06-29 /
    # 2026-07-06, "within 28 days of 2026-07-11") and silently aged out of the
    # 4-week window on 2026-07-28, failing permanently and blocking every commit
    # — including a release cut. What this test pins is format normalization,
    # not any particular date, so anchor both rows safely inside the window.
    now = datetime.now(timezone.utc)
    compact_date = (now - timedelta(days=7)).strftime("%Y%m%dT%H%M%S.000Z")  # legacy archive row
    iso_date = (now - timedelta(days=14)).strftime("%Y-%m-%d")  # ISO — the buggy row
    canonical_date = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        # Two recent weeks, DIFFERENT date formats — both inside the 4-week window.
        conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, created_date) VALUES (140, 0, ?)",
            (compact_date,),
        )
        conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, created_date) VALUES (140, 1, ?)",
            (iso_date,),
        )  # ISO — the buggy row
        conn.commit()

        att = db.get_member_war_attendance("#WK1", season_id=140, conn=conn)
        # Both recent weeks must be counted (buggy raw compare would drop the ISO one).
        assert att["last_4_weeks"]["total_races"] == 2
        # This compatibility test deliberately introduced a legacy compact row;
        # leave the shared test DB in the canonical write domain.
        conn.execute(
            "UPDATE war_weeks SET created_date=? WHERE season_id=140 AND section_index=0",
            (canonical_date,),
        )
        conn.commit()
    finally:
        conn.close()
