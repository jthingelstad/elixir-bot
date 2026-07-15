"""battle_events retention must actually purge (C3 regression).

battle_events.battle_time is CR-compact (YYYYMMDDTHHMMSS.000Z). The purge
must compare it against a compact cutoff, not an ISO one — an ISO cutoff sorts
below every compact timestamp, so nothing is ever deleted and the table grows
unbounded. This test seeds one ancient and one recent battle and asserts only
the ancient one is purged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from storage import metadata


def _cr(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S.000Z")


def test_purge_removes_old_battle_events_keeps_recent():
    conn = db.get_connection()
    try:
        now = datetime.now(timezone.utc)
        ancient = now - timedelta(days=db.BATTLE_EVENT_RETENTION_DAYS + 10)
        recent = now - timedelta(days=1)
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#T1','T','x','x')"
        )
        for key, when in (("old", ancient), ("new", recent)):
            conn.execute(
                "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at) "
                "VALUES (?, '#T1', ?, ?)",
                (key, _cr(when), when.strftime("%Y-%m-%dT%H:%M:%S")),
            )
        conn.commit()

        stats = metadata.purge_old_data(conn=conn)
        conn.commit()

        assert stats["battle_events"] == 1  # exactly the ancient row
        remaining = {
            r["dedup_key"]
            for r in conn.execute("SELECT dedup_key FROM battle_events").fetchall()
        }
        assert remaining == {"new"}
    finally:
        conn.close()
