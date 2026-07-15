"""battle_events window filters must honor the lookback window (C2 regression).

battle_time is CR-compact (20260507T144643.000Z). Comparing it against an ISO
cutoff matched all of history, so summarize_battle_modes / summarize_event_windows
returned identical inflated counts for every window. This seeds battles inside
and outside a 7d window and asserts 7d < 28d.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from storage import events_read


def _cr(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S.000Z")


def _seed(conn, n, when, *, start=0):
    for i in range(n):
        conn.execute(
            "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, "
            "mode_group, game_mode_name, outcome) VALUES (?,?,?,?,?,?,?)",
            (
                f"b{start + i}",
                "#T1",
                _cr(when),
                when.strftime("%Y-%m-%dT%H:%M:%S"),
                "ladder",
                "Ladder",
                "W",
            ),
        )


def test_battle_windows_respect_lookback():
    conn = db.get_connection()
    try:
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#T1','T','x','x')"
        )
        _seed(conn, 4, now - timedelta(days=2), start=0)  # inside 7d and 28d
        _seed(conn, 4, now - timedelta(days=20), start=100)  # inside 28d only
        conn.commit()

        modes = events_read.summarize_battle_modes(windows=(7, 28), conn=conn)
        w7 = modes["windows"]["7d"]["modes"]
        w28 = modes["windows"]["28d"]["modes"]
        assert w7 and w28
        assert w7[0]["battles"] == 4  # only the recent 4
        assert w28[0]["battles"] == 8  # all 8 — window is wider, not identical

        ew = events_read.summarize_event_windows(windows=(7, 28), conn=conn)
        assert ew["windows"]["7d"]["battles_mirrored"] == 4
        assert ew["windows"]["28d"]["battles_mirrored"] == 8
    finally:
        conn.close()
