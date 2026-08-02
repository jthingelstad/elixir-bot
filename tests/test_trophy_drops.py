"""get_trophy_drops must report only genuine trophy DECLINES (QA H11).

The old MAX-MIN spread labelled climbers as droppers — a member who went
8700 -> 9004 showed 'drop: 304'. The fix takes the directional first->last net
and returns only real declines.
"""

from __future__ import annotations

import datetime as _dt

import db

# get_trophy_drops(days=30) is a rolling window, so literal dates are a fuse: once
# the oldest seeded day ages past 30 days the first and last reading collapse to
# the same row, the computed drop becomes 0, and the test fails forever with no
# code change. Anchor the series to today.
_D0, _D1, _D2, _D3 = ((_dt.date.today() - _dt.timedelta(days=d)).isoformat() for d in (3, 2, 1, 0))


def _seed(conn, tag, name, series):
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, '2026-07-01', '2026-07-11')",
        (tag, name),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES (?, '2026-07-01', 'test')",
        (tag,),
    )
    for date, trophies in series:
        conn.execute(
            "INSERT INTO player_daily_metrics (player_tag, metric_date, trophies) VALUES (?, ?, ?)",
            (tag, date, trophies),
        )
    conn.commit()


def test_trophy_drops_excludes_climbers_and_reports_declines():
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) "
            "VALUES ('#J2RGCRVG', '2026-07-01', '2026-07-11', 1)"
        )
        # Climber: dipped mid-window but net POSITIVE first->last (old spread bug
        # would have flagged the 8700->9100 range as a 400 "drop").
        _seed(
            conn,
            "#CLIMB",
            "Andy",
            [
                (_D0, 8700),
                (_D1, 9100),
                (_D2, 8900),
                (_D3, 9004),
            ],
        )
        # Real dropper: net decline first->last.
        _seed(conn, "#DROP", "Ditaka", [(_D0, 12230), (_D3, 12037)])

        drops = db.get_trophy_drops(days=30, min_drop=100, conn=conn)
        by_tag = {d["tag"]: d for d in drops}

        assert "#CLIMB" not in by_tag  # climber excluded (net +304)
        assert "#DROP" in by_tag
        d = by_tag["#DROP"]
        assert d["change"] == -193 and d["drop"] == 193
        assert d["from_trophies"] == 12230 and d["to_trophies"] == 12037
        # Never report a positive change as a drop.
        assert all(x["change"] < 0 for x in drops)
    finally:
        conn.close()
