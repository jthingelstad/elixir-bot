"""compare_member/clan_trend_windows must read battle activity from the
authoritative battle_events store, not the lossy daily rollups (QA H1/H2).

The rollups undercounted new/backfilled members (a real 31-battle week showed
as 2 tracked days / 10 battles) and went stale clan-wide (previous week read as
0 battles). battle_events is complete, so the windows are now exact.
"""

from __future__ import annotations

from datetime import date, timedelta

import db


def _seed_player(conn, tag, name="T"):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', '2026-07-01', '2026-07-11', 1)"
    )
    conn.execute(
        "INSERT INTO players (player_tag, current_name, display_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, '2026-07-01', '2026-07-11')",
        (tag, name, name),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) VALUES (?, '2026-07-01', 'test')",
        (tag,),
    )


def _seed_battles(conn, tag, day: date, wins, losses):
    stamp = day.strftime("%Y%m%d")
    for i in range(wins + losses):
        conn.execute(
            "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, outcome, trophy_change) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"{tag}-{stamp}-{i}",
                tag,
                f"{stamp}T12{i:02d}00.000Z",
                "2026-07-11T00:00:00Z",
                "W" if i < wins else "L",
                30 if i < wins else -30,
            ),
        )


def test_member_trend_window_reads_full_battle_events():
    conn = db.get_connection()
    try:
        today = date.fromisoformat(db.chicago_today())
        _seed_player(conn, "#TREND1", "Andy")
        # previous 7d window: a busy week the lossy rollup would have undercounted
        _seed_battles(conn, "#TREND1", today - timedelta(days=9), wins=9, losses=6)
        _seed_battles(conn, "#TREND1", today - timedelta(days=8), wins=6, losses=4)  # 25 total prev
        # current 7d window
        _seed_battles(conn, "#TREND1", today - timedelta(days=2), wins=5, losses=5)
        conn.commit()

        cmp = db.compare_member_trend_windows("#TREND1", conn=conn)
        prev = cmp["previous"]["battle_activity"]
        cur = cmp["current"]["battle_activity"]
        assert prev["battles"] == 25 and (prev["wins"], prev["losses"]) == (15, 10)
        assert prev["days"] == 2  # two distinct battle days, fully counted
        assert cur["battles"] == 10 and (cur["wins"], cur["losses"]) == (5, 5)
    finally:
        conn.close()


def test_clan_trend_window_counts_current_members_from_events():
    conn = db.get_connection()
    try:
        today = date.fromisoformat(db.chicago_today())
        _seed_player(conn, "#CMEM1", "A")
        _seed_player(conn, "#CMEM2", "B")
        _seed_battles(conn, "#CMEM1", today - timedelta(days=1), wins=3, losses=1)
        _seed_battles(conn, "#CMEM2", today - timedelta(days=1), wins=2, losses=2)
        # previous week has activity too — must NOT read as 0 (the dead-clan bug)
        _seed_battles(conn, "#CMEM1", today - timedelta(days=9), wins=4, losses=0)
        conn.commit()

        cmp = db.compare_clan_trend_windows(conn=conn)
        cur = cmp["current"]["battle_activity"]
        prev = cmp["previous"]["battle_activity"]
        assert cur["battles"] == 8 and cur["active_members"] == 2
        assert prev["battles"] == 4  # not 0 — the false dead-clan signal is gone
    finally:
        conn.close()
