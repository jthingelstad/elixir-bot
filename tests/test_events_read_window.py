"""battle_events window filters must honor the lookback window (C2 regression).

battle_time is ISO-Z (2026-05-07T14:46:43Z) since schema v25. Comparing it against a mismatched
cutoff matched all of history, so summarize_event_windows returned identical
inflated counts for every window. This seeds battles inside and outside a 7d
window and asserts 7d < 28d.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from storage import events_read


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(conn, n, when, *, start=0):
    for i in range(n):
        conn.execute(
            "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, "
            "mode_group, game_mode_name, outcome) VALUES (?,?,?,?,?,?,?)",
            (
                f"b{start + i}",
                "#T1",
                _iso(when),
                _iso(when),
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

        ew = events_read.summarize_event_windows(windows=(7, 28), conn=conn)
        assert ew["windows"]["7d"]["battles_mirrored"] == 4
        assert ew["windows"]["28d"]["battles_mirrored"] == 8
    finally:
        conn.close()
