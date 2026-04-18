"""Regression tests for the war_surprise_participant signal:

1. The "first war ever" classification must take in-progress race
   participation into account, not only closed races. Otherwise a brand-new
   member who plays every battle day of their first race week gets flagged
   as a surprise participant on every battle day, not just the first.

2. Per-member signal_log_type entries inside a group signal must be logged
   when the signal is marked delivered, so the detector's per-member
   dedup actually works.
"""

import db
from runtime.jobs._signals import _mark_delivered_signals
from storage.war_analytics import has_played_earlier_this_week


def _seed_member(conn, tag, name="Player"):
    return db.snapshot_members(
        [{"tag": tag, "name": name, "role": "member", "expLevel": 60, "trophies": 6000, "clanRank": 1}],
        conn=conn,
    )


def _seed_war_day_status(conn, tag, season_id, section_index, period_index, decks_used_total):
    member_id = conn.execute(
        "SELECT member_id FROM members WHERE player_tag = ?", (tag,)
    ).fetchone()["member_id"]
    conn.execute(
        "INSERT INTO war_day_status (member_id, battle_date, observed_at, fame, repair_points, "
        " boat_attacks, decks_used_total, decks_used_today, season_id, section_index, "
        " period_index, phase, phase_day_number, raw_json) "
        "VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?, 'battle', ?, '{}')",
        (member_id, f"s{season_id}-w{section_index}-p{period_index}",
         "2026-04-18T10:00:00", decks_used_total, decks_used_total,
         season_id, section_index, period_index, period_index - 9),
    )
    conn.commit()


def test_has_played_earlier_this_week_false_when_no_prior_days():
    conn = db.get_connection(":memory:")
    try:
        _seed_member(conn, "#PLCCYUQL", "TDuck")
        # Period 12 is current, no rows seeded for periods 10/11.
        assert not has_played_earlier_this_week(conn, "#PLCCYUQL", 131, 1, 12)
    finally:
        conn.close()


def test_has_played_earlier_this_week_true_when_prior_day_has_decks():
    conn = db.get_connection(":memory:")
    try:
        _seed_member(conn, "#PLCCYUQL", "TDuck")
        _seed_war_day_status(conn, "#PLCCYUQL", 131, 1, 10, decks_used_total=4)
        _seed_war_day_status(conn, "#PLCCYUQL", 131, 1, 11, decks_used_total=8)
        # On period 12, prior days 10 and 11 had plays — not a surprise.
        assert has_played_earlier_this_week(conn, "#PLCCYUQL", 131, 1, 12)
    finally:
        conn.close()


def test_has_played_earlier_this_week_ignores_other_weeks():
    """A play from a *different* race week must not count."""
    conn = db.get_connection(":memory:")
    try:
        _seed_member(conn, "#PLCCYUQL", "TDuck")
        # Last week (section 0) — still counts as "never" played THIS week.
        _seed_war_day_status(conn, "#PLCCYUQL", 131, 0, 6, decks_used_total=4)
        assert not has_played_earlier_this_week(conn, "#PLCCYUQL", 131, 1, 12)
    finally:
        conn.close()


def test_mark_delivered_signals_records_per_member_log_type():
    """Group signals like war_surprise_participant carry one signal_log_type
    per member. _mark_delivered_signals must log each so the detector's
    per-member dedup check can actually find them next tick."""
    from unittest.mock import patch

    calls = []
    with patch.object(db, "mark_signal_sent", side_effect=lambda t, d: calls.append((t, d))):
        signal = {
            "type": "war_surprise_participant",
            "signal_date": "2026-04-18",
            "members": [
                {"tag": "#AAA", "signal_log_type": "war_surprise_participant:#AAA:s131:w2"},
                {"tag": "#BBB", "signal_log_type": "war_surprise_participant:#BBB:s131:w2"},
            ],
        }
        _mark_delivered_signals([signal])

    logged = {t for t, _ in calls}
    # Outer type still logged
    assert "war_surprise_participant" in logged
    # Per-member keys now logged too — detector dedup will work next tick
    assert "war_surprise_participant:#AAA:s131:w2" in logged
    assert "war_surprise_participant:#BBB:s131:w2" in logged
    # Every call carries the signal_date from the signal envelope
    assert all(d == "2026-04-18" for _, d in calls)
