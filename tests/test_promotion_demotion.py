"""Promotion + demotion recommendation logic.

Rules under test:
- Promotion floor: 21 days in clan.
- Elder cap: ~3 per 10 active members; cap_reached flag.
- Donations must be meaningful — same threshold as promotion.
- Demotion when donations are below the threshold for two consecutive weeks.
"""

from datetime import datetime, timedelta, timezone

import db
from storage.war_analytics import (
    get_promotion_candidates,
    get_demotion_candidates,
    _donations_peaks_in_window,
)


def _seed_member(conn, tag, name, role, trophies=8000, donations_week=200, last_seen="20260418T120000.000Z"):
    db.snapshot_members(
        [{"tag": tag, "name": name, "role": role, "expLevel": 60,
          "trophies": trophies, "clanRank": 1, "donations": donations_week,
          "lastSeen": last_seen}],
        conn=conn,
    )
    return conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()["member_id"]


def _seed_daily_metrics(conn, member_id, *, today, points: list[tuple[int, int]]):
    """points: list of (days_back, donations_week_value)."""
    for days_back, donations in points:
        date_str = (today - timedelta(days=days_back)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO member_daily_metrics "
            "(member_id, metric_date, donations_week) VALUES (?, ?, ?)",
            (member_id, date_str, donations),
        )
    conn.commit()


def test_promotion_min_tenure_is_21_days():
    conn = db.get_connection(":memory:")
    try:
        # Two strong members, one over the new 21-day floor, one under.
        _seed_member(conn, "#OLD", "OldHand", "member", donations_week=300)
        _seed_member(conn, "#NEW", "Newcomer", "member", donations_week=300)
        db.set_member_join_date("#OLD", "OldHand", "2026-03-15", conn=conn)  # well over 21d
        db.set_member_join_date("#NEW", "Newcomer", "2026-04-10", conn=conn)  # ~8d
        result = get_promotion_candidates(min_donations_week=50, conn=conn)
        recommended_tags = {m["tag"] for m in result["recommended"]}
        assert "#OLD" in recommended_tags
        assert "#NEW" not in recommended_tags
    finally:
        conn.close()


def test_elder_cap_reached_flag_set_when_at_3_per_10():
    conn = db.get_connection(":memory:")
    try:
        # 10 active members, 3 elders → cap reached.
        for i in range(7):
            _seed_member(conn, f"#M{i:02d}", f"M{i}", "member", donations_week=10)
        for i in range(3):
            _seed_member(conn, f"#E{i:02d}", f"E{i}", "elder", donations_week=200)
        result = get_promotion_candidates(conn=conn)
        assert result["composition"]["active_members"] == 10
        assert result["composition"]["target_elder_max"] == 3
        assert result["composition"]["elder_capacity_remaining"] == 0
        assert result["composition"]["elder_cap_reached"] is True
    finally:
        conn.close()


def test_demotion_flagged_after_two_consecutive_weak_weeks():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ELDER1", "WeakElder", "elder", donations_week=10,
                                  last_seen="20260418T120000.000Z")
        today = datetime(2026, 4, 18, tzinfo=timezone.utc).date()
        # Two weeks of low donations: peak <50 in both windows.
        _seed_daily_metrics(conn, member_id, today=today, points=[
            (1, 10), (2, 8), (3, 6),    # this week
            (8, 12), (9, 10), (10, 8),  # last week
        ])
        result = get_demotion_candidates(min_donations_week=50, conn=conn)
        tags = {m["tag"] for m in result["members"]}
        assert "#ELDER1" in tags
    finally:
        conn.close()


def test_demotion_not_flagged_when_one_week_was_strong():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ELDER2", "GoodWeek", "elder", donations_week=20)
        today = datetime(2026, 4, 18, tzinfo=timezone.utc).date()
        _seed_daily_metrics(conn, member_id, today=today, points=[
            (1, 20),                # this week is weak
            (8, 200), (9, 150),     # last week was meaningful — not a sustained dip
        ])
        result = get_demotion_candidates(min_donations_week=50, conn=conn)
        tags = {m["tag"] for m in result["members"]}
        assert "#ELDER2" not in tags
    finally:
        conn.close()


def test_demotion_skipped_when_history_is_insufficient():
    """A brand-new elder with <14 days of snapshots must not be demoted on
    insufficient evidence."""
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#NEWELDER", "FreshElder", "elder", donations_week=10)
        today = datetime(2026, 4, 18, tzinfo=timezone.utc).date()
        # Only this week's snapshots — last week has nothing.
        _seed_daily_metrics(conn, member_id, today=today, points=[
            (1, 5), (2, 5), (3, 5),
        ])
        result = get_demotion_candidates(min_donations_week=50, conn=conn)
        tags = {m["tag"] for m in result["members"]}
        assert "#NEWELDER" not in tags
    finally:
        conn.close()


def test_promotion_response_includes_demotion_candidates():
    """get_promotion_candidates returns both lists in one response."""
    conn = db.get_connection(":memory:")
    try:
        # One promotable member, one demotable elder.
        m_id = _seed_member(conn, "#STRONG", "Strong", "member", donations_week=300)
        db.set_member_join_date("#STRONG", "Strong", "2026-01-01", conn=conn)
        e_id = _seed_member(conn, "#WEAK", "WeakElder", "elder", donations_week=5)
        today = datetime(2026, 4, 18, tzinfo=timezone.utc).date()
        _seed_daily_metrics(conn, e_id, today=today, points=[
            (1, 5), (2, 5), (8, 10), (10, 8),
        ])
        result = get_promotion_candidates(min_donations_week=50, conn=conn)
        assert any(m["tag"] == "#STRONG" for m in result["recommended"])
        assert any(m["tag"] == "#WEAK" for m in result["demotion_candidates"])
    finally:
        conn.close()
