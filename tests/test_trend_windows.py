"""Trend windows: the failure mode is ZEROS, and zeros look like a quiet week.

`storage/trends.py` had five of its nine public functions untested as of
2026-08-06, in a module with a documented history of failing silently.

The incident these guard against is worth stating exactly, because the shape
recurs. `battle_time` became ISO-Z at schema v25, so the day key is
`substr(battle_time, 1, 10)` — but the window BOUNDS were left as CR-compact
`YYYYMMDD`. Since '-' (0x2D) sorts below '0' (0x30), both comparisons failed for
every row, and the window returned zeros for a member with **523 battles in it**.

That bug has no symptom. A member with no battles and a member whose window is
broken produce the same output, and the honest-looking answer ("quiet week") is
the wrong one. The function's own docstring describes the failure in detail —
and until now nothing asserted it could not come back.

So these tests deliberately seed data on BOTH sides of a window boundary and
assert non-zero. A format regression fails here instead of silently reading as
inactivity.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from engine.db import chicago_today
from storage import trends

TAG = "#TREND1"


def _today():
    return datetime.fromisoformat(chicago_today()).date()


def _seed_player(conn, tag=TAG, name="Trendy"):
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?)",
        (tag, name, name, "2026-01-01", "2026-08-06"),
    )


def _seed_battle(conn, *, days_ago: int, outcome="W", trophy_change=30, tag=TAG, n=1):
    """A battle on a given day, stamped the way the engine stamps them: ISO-Z."""
    day = _today() - timedelta(days=days_ago)
    for i in range(n):
        stamp = f"{day.isoformat()}T{10 + i:02d}:00:00Z"
        conn.execute(
            "INSERT INTO battle_events (dedup_key, player_tag, battle_time, observed_at, "
            "mode_group, battle_type, outcome, trophy_change) VALUES (?,?,?,?,?,?,?,?)",
            (f"{tag}:{stamp}:{i}", tag, stamp, stamp, "ranked", "PvP", outcome, trophy_change),
        )


def _seed_daily(conn, *, days_ago: int, trophies: int, tag=TAG):
    day = (_today() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO player_daily_metrics (player_tag, metric_date, trophies, "
        "best_trophies, clan_rank) VALUES (?,?,?,?,?)",
        (tag, day, trophies, trophies, 1),
    )


# ------------------------------------------------------- the zeros regression


def test_a_member_with_battles_this_week_does_not_read_as_zero(engine_conn):
    """The v25 regression, pinned. Both window bounds and the day key must be in
    the SAME format, or every row falls outside the range and a busy week reads
    as an empty one."""
    _seed_player(engine_conn)
    _seed_battle(engine_conn, days_ago=1, n=5)
    _seed_battle(engine_conn, days_ago=3, outcome="L", trophy_change=-20, n=2)
    engine_conn.commit()

    result = trends.compare_member_trend_windows(TAG, window_days=7, conn=engine_conn)
    current = result["current"]["battle_activity"]
    assert current["battles"] == 7, "seven seeded battles must be counted, not zero"
    assert current["wins"] == 5 and current["losses"] == 2
    assert current["days"] == 2, "distinct battle days, not battle count"


def test_the_previous_window_is_a_different_window(engine_conn):
    """Current and previous must not overlap or the comparison is meaningless —
    a member would be compared against themselves and every week would look
    flat."""
    _seed_player(engine_conn)
    _seed_battle(engine_conn, days_ago=2, n=3)  # inside current 7-day window
    _seed_battle(engine_conn, days_ago=10, n=4)  # inside previous 7-day window
    engine_conn.commit()

    result = trends.compare_member_trend_windows(TAG, window_days=7, conn=engine_conn)
    assert result["current"]["battle_activity"]["battles"] == 3
    assert result["previous"]["battle_activity"]["battles"] == 4


@pytest.mark.parametrize("days_ago,in_current", [(0, True), (6, True), (7, False), (8, False)])
def test_the_window_boundary_is_inclusive_on_both_ends(engine_conn, days_ago, in_current):
    """A 7-day window covers today and the six days before it. Off-by-one here
    silently moves a whole day of battles between windows, which is how a flat
    week starts looking like a surge."""
    _seed_player(engine_conn)
    _seed_battle(engine_conn, days_ago=days_ago, n=1)
    engine_conn.commit()

    result = trends.compare_member_trend_windows(TAG, window_days=7, conn=engine_conn)
    assert (result["current"]["battle_activity"]["battles"] == 1) is in_current


def test_a_genuinely_quiet_week_still_reads_as_zero(engine_conn):
    """The counterpart to the regression: zeros must remain POSSIBLE. A test
    that only proved non-zero could be satisfied by a function that always
    returned a number."""
    _seed_player(engine_conn)
    _seed_battle(engine_conn, days_ago=30, n=9)  # far outside both windows
    engine_conn.commit()

    result = trends.compare_member_trend_windows(TAG, window_days=7, conn=engine_conn)
    assert result["current"]["battle_activity"]["battles"] == 0
    assert result["previous"]["battle_activity"]["battles"] == 0


# ------------------------------------------------------------- the histories


def test_trophy_history_respects_its_day_window(engine_conn):
    """`get_member_trophy_history` is the input to the trophy half of every
    trend comparison; a broken cutoff silently truncates or over-reports."""
    _seed_player(engine_conn)
    for days_ago, trophies in ((1, 7100), (5, 7000), (40, 6000)):
        _seed_daily(engine_conn, days_ago=days_ago, trophies=trophies)
    engine_conn.commit()

    recent = trends.get_member_trophy_history(TAG, days=30, conn=engine_conn)
    assert [r["trophies"] for r in recent] == [7000, 7100], "ascending, and 40 days ago excluded"

    wide = trends.get_member_trophy_history(TAG, days=90, conn=engine_conn)
    assert len(wide) == 3


def test_trophy_history_is_scoped_to_one_member(engine_conn):
    """Cross-member bleed would attribute another player's climb to this one."""
    _seed_player(engine_conn)
    _seed_player(engine_conn, tag="#OTHER", name="Someone Else")
    _seed_daily(engine_conn, days_ago=1, trophies=7100)
    _seed_daily(engine_conn, days_ago=1, trophies=99999, tag="#OTHER")
    engine_conn.commit()

    rows = trends.get_member_trophy_history(TAG, days=30, conn=engine_conn)
    assert [r["trophies"] for r in rows] == [7100]


def test_an_unknown_member_returns_empty_not_an_error(engine_conn):
    """These feed member-facing answers; a raise would take down the reply."""
    assert trends.get_member_trophy_history("#NOBODY", conn=engine_conn) == []
    result = trends.compare_member_trend_windows("#NOBODY", conn=engine_conn)
    assert result["current"]["battle_activity"]["battles"] == 0
    assert result["member"]["tag"] == "#NOBODY"


def test_daily_battle_summary_reads_rollups_not_events(engine_conn):
    """These two functions read DIFFERENT sources on purpose, and confusing them
    is easy: the trend window reads `battle_events` because the rollups are lossy
    for new and backfilled members (a real 27-battle week showed as 2 tracked
    days / 10 battles), while the daily summary reads
    `player_daily_battle_rollups` for its per-mode breakdown.

    Seeding only battle_events must therefore produce NOTHING here — which is
    exactly the trap this test exists to document.
    """
    _seed_player(engine_conn)
    _seed_battle(engine_conn, days_ago=1, n=3)
    engine_conn.commit()
    assert trends.get_member_daily_battle_summary(TAG, days=30, conn=engine_conn) == [], (
        "the daily summary does not read battle_events"
    )

    day = (_today() - timedelta(days=1)).isoformat()
    engine_conn.execute(
        "INSERT INTO player_daily_battle_rollups (player_tag, battle_date, mode_group, "
        "battles, wins, losses, draws, trophy_change_total, last_aggregated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (TAG, day, "ranked", 3, 2, 1, 0, 45, f"{day}T12:00:00Z"),
    )
    engine_conn.commit()
    rows = trends.get_member_daily_battle_summary(TAG, days=30, conn=engine_conn)
    assert [r["battles"] for r in rows] == [3]
    assert rows[0]["mode_group"] == "ranked"

    # And the day window applies to rollups too.
    assert trends.get_member_daily_battle_summary(TAG, days=0, conn=engine_conn) == []
