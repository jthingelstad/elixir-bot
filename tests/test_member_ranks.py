"""Tests for storage.member_ranks — comparative rank fields on member references."""

from datetime import date, datetime, timedelta, timezone

import db
from storage.member_ranks import (
    ELDER_ELIGIBILITY_DEFAULTS,
    RANK_FIELDS,
    compute_member_ranks,
    evaluate_elder_eligibility,
)


# ── seed helpers ───────────────────────────────────────────────────────────

def _seed_member(
    conn,
    tag,
    name="Tester",
    role="member",
    donations_week=200,
    trophies=8000,
    clan_rank=1,
    last_seen="20260420T120000.000Z",
):
    """Insert a member with a current_state row. Returns member_id."""
    db.snapshot_members(
        [{
            "tag": tag, "name": name, "role": role, "expLevel": 60,
            "trophies": trophies, "clanRank": clan_rank,
            "donations": donations_week, "lastSeen": last_seen,
        }],
        conn=conn,
    )
    return conn.execute(
        "SELECT member_id FROM members WHERE player_tag = ?", (tag,)
    ).fetchone()["member_id"]


def _seed_war_race(conn, season_id, section_index, war_race_id=None):
    if war_race_id is None:
        war_race_id = section_index + 1
    created = f"2026010{section_index + 1}T100000.000Z"
    conn.execute(
        "INSERT INTO war_races "
        "(war_race_id, season_id, section_index, created_date, our_rank, "
        "our_fame, total_clans, finish_time) "
        "VALUES (?, ?, ?, ?, 1, 10000, 5, ?)",
        (war_race_id, season_id, section_index, created, created),
    )
    # current_war_state is what get_current_season_id reads.
    db.upsert_war_current_state(
        {
            "state": "full",
            "seasonId": season_id,
            "sectionIndex": section_index,
            "periodIndex": 3,
            "periodType": "warDay",
            "clan": {
                "tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0,
                "repairPoints": 0, "periodPoints": 0, "clanScore": 100,
                "participants": [],
            },
        },
        conn=conn,
    )
    return war_race_id


def _seed_war_participation(conn, war_race_id, member_id, tag, fame, decks=16):
    conn.execute(
        "INSERT INTO war_participation "
        "(war_race_id, member_id, player_tag, player_name, fame, decks_used) "
        "VALUES (?, ?, ?, 'T', ?, ?)",
        (war_race_id, member_id, tag, fame, decks),
    )


def _seed_daily_metric(conn, member_id, days_back, donations_week, today=None):
    today = today or date.today()
    metric_date = (today - timedelta(days=days_back)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO member_daily_metrics "
        "(member_id, metric_date, donations_week) VALUES (?, ?, ?)",
        (member_id, metric_date, donations_week),
    )


# ── donation_rank_week ─────────────────────────────────────────────────────

def test_donation_rank_week_orders_by_donations_desc():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice", donations_week=500, clan_rank=2)
        b = _seed_member(conn, "#B", "Bob", donations_week=300, clan_rank=1)
        c = _seed_member(conn, "#C", "Cam", donations_week=100, clan_rank=3)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[a]["donation_rank_week"] == 1
        assert ranks[b]["donation_rank_week"] == 2
        assert ranks[c]["donation_rank_week"] == 3
    finally:
        conn.close()


def test_donation_rank_week_ties_break_on_clan_rank_then_name():
    conn = db.get_connection(":memory:")
    try:
        # Same donations: clan_rank ASC wins (lower rank = higher trophy position).
        a = _seed_member(conn, "#A", "Alice", donations_week=200, clan_rank=5)
        b = _seed_member(conn, "#B", "Bob", donations_week=200, clan_rank=2)
        c = _seed_member(conn, "#C", "Cam", donations_week=200, clan_rank=2)
        ranks = compute_member_ranks(conn=conn)
        # Bob and Cam tie on donations + clan_rank → name break.
        # Alice has worse clan_rank → ranked last.
        assert ranks[b]["donation_rank_week"] == 1  # 'Bob' before 'Cam'
        assert ranks[c]["donation_rank_week"] == 2
        assert ranks[a]["donation_rank_week"] == 3
    finally:
        conn.close()


def test_donation_rank_week_only_active_members():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice", donations_week=500)
        b = _seed_member(conn, "#B", "Bob", donations_week=100)
        # Mark Bob inactive. He should be omitted from the rank table.
        conn.execute("UPDATE members SET status='left' WHERE member_id=?", (b,))
        ranks = compute_member_ranks(conn=conn)
        assert b not in ranks
        assert ranks[a]["donation_rank_week"] == 1
    finally:
        conn.close()


# ── donation_rank_season ───────────────────────────────────────────────────

def test_donation_rank_season_sums_weekly_peaks():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice", donations_week=100)
        b = _seed_member(conn, "#B", "Bob", donations_week=100)
        _seed_war_race(conn, season_id=131, section_index=0)
        # _seed_war_race sets created_date 2026-01-01T10:00, so season window starts then.
        # Daily metrics within window — Alice peaks higher per week.
        for member_id, peaks in [(a, [200, 300]), (b, [150, 100])]:
            for i, val in enumerate(peaks):
                conn.execute(
                    "INSERT INTO member_daily_metrics "
                    "(member_id, metric_date, donations_week) VALUES (?, ?, ?)",
                    (member_id, f"2026-01-{(i*7)+2:02d}", val),
                )
        ranks = compute_member_ranks(conn=conn)
        assert ranks[a]["donation_rank_season"] == 1
        assert ranks[b]["donation_rank_season"] == 2
    finally:
        conn.close()


def test_donation_rank_season_null_when_no_metrics():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice")
        # No war_races → no current season → no season ranking
        ranks = compute_member_ranks(conn=conn)
        assert ranks[a]["donation_rank_season"] is None
    finally:
        conn.close()


# ── war_fame_rank_current_race ─────────────────────────────────────────────

def test_war_fame_rank_current_race_orders_by_fame():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice")
        b = _seed_member(conn, "#B", "Bob")
        c = _seed_member(conn, "#C", "Cam")
        race = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_war_participation(conn, race, a, "#A", fame=4500)
        _seed_war_participation(conn, race, b, "#B", fame=3000)
        _seed_war_participation(conn, race, c, "#C", fame=1500)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[a]["war_fame_rank_current_race"] == 1
        assert ranks[b]["war_fame_rank_current_race"] == 2
        assert ranks[c]["war_fame_rank_current_race"] == 3
    finally:
        conn.close()


def test_war_fame_rank_current_race_uses_latest_section():
    """Multiple sections — 'current race' is the highest section_index."""
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice")
        b = _seed_member(conn, "#B", "Bob")
        # Alice dominated old section, Bob dominates current.
        old_race = _seed_war_race(conn, season_id=131, section_index=0, war_race_id=10)
        new_race = _seed_war_race(conn, season_id=131, section_index=1, war_race_id=11)
        _seed_war_participation(conn, old_race, a, "#A", fame=5000)
        _seed_war_participation(conn, old_race, b, "#B", fame=500)
        _seed_war_participation(conn, new_race, a, "#A", fame=500)
        _seed_war_participation(conn, new_race, b, "#B", fame=4000)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[b]["war_fame_rank_current_race"] == 1
        assert ranks[a]["war_fame_rank_current_race"] == 2
    finally:
        conn.close()


def test_war_fame_rank_null_when_no_participation_this_race():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice")
        b = _seed_member(conn, "#B", "Bob")
        race = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_war_participation(conn, race, a, "#A", fame=4500)
        # Bob did not play this race → null, not 0.
        ranks = compute_member_ranks(conn=conn)
        assert ranks[a]["war_fame_rank_current_race"] == 1
        assert ranks[b]["war_fame_rank_current_race"] is None
    finally:
        conn.close()


# ── war_fame_rank_season ───────────────────────────────────────────────────

def test_war_fame_rank_season_sums_across_races():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice")
        b = _seed_member(conn, "#B", "Bob")
        race1 = _seed_war_race(conn, season_id=131, section_index=0, war_race_id=10)
        race2 = _seed_war_race(conn, season_id=131, section_index=1, war_race_id=11)
        # Alice: 3000 + 3000 = 6000. Bob: 4500 + 1000 = 5500.
        _seed_war_participation(conn, race1, a, "#A", fame=3000)
        _seed_war_participation(conn, race2, a, "#A", fame=3000)
        _seed_war_participation(conn, race1, b, "#B", fame=4500)
        _seed_war_participation(conn, race2, b, "#B", fame=1000)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[a]["war_fame_rank_season"] == 1
        assert ranks[b]["war_fame_rank_season"] == 2
    finally:
        conn.close()


# ── elder_eligible ─────────────────────────────────────────────────────────

def test_elder_eligible_only_for_role_member():
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=100)
        e = _seed_member(conn, "#ELD", "E", role="elder", donations_week=100)
        l = _seed_member(conn, "#LEAD", "L", role="leader", donations_week=100)
        db.set_member_join_date("#MEM", "M", "2026-01-01", conn=conn)
        db.set_member_join_date("#ELD", "E", "2026-01-01", conn=conn)
        db.set_member_join_date("#LEAD", "L", "2026-01-01", conn=conn)
        ranks = compute_member_ranks(conn=conn)
        # Member is the only one whose eligibility predicate runs.
        assert ranks[m]["elder_eligible"] is not None
        assert ranks[e]["elder_eligible"] is None
        assert ranks[l]["elder_eligible"] is None
    finally:
        conn.close()


def test_elder_eligible_true_when_all_criteria_pass():
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(
            conn, "#MEM", "M", role="member",
            donations_week=200, last_seen="20260420T120000.000Z",
        )
        # Tenure: joined 60 days ago.
        db.set_member_join_date("#MEM", "M", "2026-02-19", conn=conn)
        # War participation in current season.
        race = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_war_participation(conn, race, m, "#MEM", fame=2000)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible"] is True
    finally:
        conn.close()


def test_elder_eligible_false_when_donations_below_threshold():
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=10)
        db.set_member_join_date("#MEM", "M", "2026-02-19", conn=conn)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible"] is False
    finally:
        conn.close()


# ── elder_eligible_crossed_this_week ───────────────────────────────────────

def test_crossed_this_week_true_when_tenure_just_crossed_21():
    """A member at tenure 24 days was at tenure 17 seven days ago — under 21."""
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=200)
        # Tenure 24 days from current_state's last_seen anchor.
        # _member_activity_anchor uses MAX(last_seen_api). seed sets it to
        # 2026-04-20T12:00:00.000Z, so anchor.date() = 2026-04-20.
        # 24 days back → 2026-03-27.
        db.set_member_join_date("#MEM", "M", "2026-03-27", conn=conn)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible"] is True
        assert ranks[m]["elder_eligible_crossed_this_week"] is True
    finally:
        conn.close()


def test_crossed_this_week_true_when_donations_just_crossed():
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=200)
        # Long tenure — tenure can't be the trigger.
        db.set_member_join_date("#MEM", "M", "2026-01-01", conn=conn)
        # Daily metric 7 days ago shows donations were 10 (below threshold).
        # Anchor = 2026-04-20 → 7 days ago = 2026-04-13.
        conn.execute(
            "INSERT INTO member_daily_metrics "
            "(member_id, metric_date, donations_week) VALUES (?, '2026-04-13', 10)",
            (m,),
        )
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible"] is True
        assert ranks[m]["elder_eligible_crossed_this_week"] is True
    finally:
        conn.close()


def test_crossed_this_week_false_when_no_historical_data():
    """Conservative — no 7-day-ago snapshot returns False."""
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=200)
        # Long tenure (not the trigger), no daily_metrics seeded.
        db.set_member_join_date("#MEM", "M", "2026-01-01", conn=conn)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible"] is True
        assert ranks[m]["elder_eligible_crossed_this_week"] is False
    finally:
        conn.close()


def test_crossed_this_week_false_when_donations_already_strong_last_week():
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=200)
        db.set_member_join_date("#MEM", "M", "2026-01-01", conn=conn)
        # 7 days ago they were already above threshold.
        conn.execute(
            "INSERT INTO member_daily_metrics "
            "(member_id, metric_date, donations_week) VALUES (?, '2026-04-13', 150)",
            (m,),
        )
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible_crossed_this_week"] is False
    finally:
        conn.close()


def test_crossed_this_week_false_when_not_currently_eligible():
    conn = db.get_connection(":memory:")
    try:
        m = _seed_member(conn, "#MEM", "M", role="member", donations_week=10)
        db.set_member_join_date("#MEM", "M", "2026-01-01", conn=conn)
        ranks = compute_member_ranks(conn=conn)
        assert ranks[m]["elder_eligible"] is False
        assert ranks[m]["elder_eligible_crossed_this_week"] is False
    finally:
        conn.close()


# ── enrichment via _member_reference_fields ────────────────────────────────

def test_member_reference_fields_includes_rank_fields():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#A", "Alice", donations_week=500)
        b = _seed_member(conn, "#B", "Bob", donations_week=100)
        # Drive a real consumer that uses _member_reference_fields.
        result = db.get_promotion_candidates(conn=conn)
        items = result["recommended"] + result["borderline"]
        for item in items:
            for field in RANK_FIELDS:
                assert field in item, f"missing {field} on {item.get('tag')}"
    finally:
        conn.close()


def test_conn_cache_avoids_recomputation():
    """A single conn computes ranks once. Subsequent enrichments hit cache."""
    from db import _MEMBER_RANKS_CACHE, _clear_member_ranks_cache, _member_reference_fields
    _clear_member_ranks_cache()
    conn = db.get_connection(":memory:")
    try:
        _seed_member(conn, "#A", "Alice", donations_week=500)
        _member_reference_fields(conn, 1, {"tag": "#A"})
        cache = _MEMBER_RANKS_CACHE.get(id(conn))
        assert cache is not None
        # Mutate the cache — second call should reflect the mutated value,
        # proving the path doesn't recompute.
        cache[1]["donation_rank_week"] = 999
        item = _member_reference_fields(conn, 1, {"tag": "#A"})
        assert item["donation_rank_week"] == 999
    finally:
        conn.close()
        _clear_member_ranks_cache()


# ── pure eligibility predicate ─────────────────────────────────────────────

def test_evaluate_elder_eligibility_all_pass():
    out = evaluate_elder_eligibility(
        donations_week=100, tenure_days=30, days_inactive=2,
        war_races_played=1, season_id=131,
    )
    assert out["all_passed"] is True
    assert all(out["checks"].values())


def test_evaluate_elder_eligibility_war_skipped_without_season():
    out = evaluate_elder_eligibility(
        donations_week=100, tenure_days=30, days_inactive=2,
        war_races_played=0, season_id=None,
    )
    assert out["checks"]["war"] is True
    assert out["all_passed"] is True


def test_evaluate_elder_eligibility_thresholds_match_defaults():
    # 50 donations and 21 days are the documented bars.
    on_bar = evaluate_elder_eligibility(
        donations_week=ELDER_ELIGIBILITY_DEFAULTS["min_donations_week"],
        tenure_days=ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"],
        days_inactive=ELDER_ELIGIBILITY_DEFAULTS["active_within_days"],
        war_races_played=1, season_id=131,
    )
    assert on_bar["all_passed"] is True
    just_below = evaluate_elder_eligibility(
        donations_week=ELDER_ELIGIBILITY_DEFAULTS["min_donations_week"] - 1,
        tenure_days=ELDER_ELIGIBILITY_DEFAULTS["min_tenure_days"],
        days_inactive=ELDER_ELIGIBILITY_DEFAULTS["active_within_days"],
        war_races_played=1, season_id=131,
    )
    assert just_below["all_passed"] is False
    assert just_below["checks"]["donations"] is False
