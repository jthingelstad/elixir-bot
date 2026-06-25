"""Promotion + demotion recommendation logic.

Elder is selected by a smoothed, relative donation leaderboard:
- active member/elder only
- recent battle activity required
- recent war participation required
- no absolute donation floor
- the Elder cap is a maximum, not a target
"""

from datetime import datetime, timedelta, timezone

import db
from storage.war_analytics import get_demotion_candidates, get_promotion_candidates


ANCHOR = datetime(2026, 4, 18, tzinfo=timezone.utc).date()


def _seed_member(
    conn,
    tag,
    name,
    role,
    trophies=8000,
    donations_week=20,
    last_seen="20260418T120000.000Z",
    joined_date="2026-01-01",
):
    db.snapshot_members(
        [{"tag": tag, "name": name, "role": role, "expLevel": 60,
          "trophies": trophies, "clanRank": 1, "donations": donations_week,
          "lastSeen": last_seen}],
        conn=conn,
    )
    db.set_member_join_date(tag, name, joined_date, conn=conn)
    return conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (tag,)).fetchone()["member_id"]


def _seed_war_race(conn, season_id=131, section_index=0):
    conn.execute(
        "INSERT INTO war_races "
        "(season_id, section_index, created_date, our_rank, our_fame, total_clans, finish_time) "
        "VALUES (?, ?, '20260418T100000.000Z', 1, 10000, 5, '20260418T120000.000Z')",
        (season_id, section_index),
    )
    war_race_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
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


def _seed_war_participation(conn, war_race_id, member_id, tag, decks=4):
    conn.execute(
        "INSERT INTO war_participation "
        "(war_race_id, member_id, player_tag, player_name, fame, decks_used) "
        "VALUES (?, ?, ?, 'T', 1000, ?)",
        (war_race_id, member_id, tag, decks),
    )


def _seed_battle(conn, member_id, days_back=0):
    battle_date = ANCHOR - timedelta(days=days_back)
    conn.execute(
        "INSERT INTO member_battle_facts (member_id, battle_time, battle_type, outcome) "
        "VALUES (?, ?, 'PvP', 'win')",
        (member_id, f"{battle_date:%Y%m%d}T120000.000Z"),
    )


def _seed_daily_metrics(conn, member_id, *, peaks: list[int]):
    for week_index, peak in enumerate(peaks):
        metric_date = ANCHOR - timedelta(days=week_index * 7)
        conn.execute(
            "INSERT OR REPLACE INTO member_daily_metrics "
            "(member_id, metric_date, donations_week) VALUES (?, ?, ?)",
            (member_id, metric_date.isoformat(), peak),
        )
    conn.commit()


def _make_eligible(
    conn,
    war_race_id,
    tag,
    name,
    role="member",
    peaks=None,
    donations_week=None,
    joined_date="2026-01-01",
):
    peaks = peaks or [20]
    member_id = _seed_member(
        conn,
        tag,
        name,
        role,
        donations_week=donations_week if donations_week is not None else peaks[0],
        joined_date=joined_date,
    )
    _seed_daily_metrics(conn, member_id, peaks=peaks)
    _seed_battle(conn, member_id)
    _seed_war_participation(conn, war_race_id, member_id, tag)
    return member_id


def test_promotion_has_no_absolute_donation_floor():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        for idx, peak in enumerate([40, 30, 20, 10, 8, 6, 4, 3, 2, 1]):
            _make_eligible(conn, race, f"#M{idx}", f"M{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(min_donations_week=50, conn=conn)

        assert result["criteria"]["donation_rule"].startswith("relative rank")
        assert result["composition"]["target_elder_max"] == 3
        assert result["composition"]["elder_selection_count"] == 1
        assert [m["tag"] for m in result["recommended"]] == ["#M0"]
        assert all(m["rolling_donations_avg"] < 50 for m in result["recommended"])
    finally:
        conn.close()


def test_role_review_recommends_promotions_and_demotions_from_same_board():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        _make_eligible(conn, race, "#E1", "Top Elder", role="elder", peaks=[100, 100])
        _make_eligible(conn, race, "#E2", "Middle Elder", role="elder", peaks=[90, 90])
        _make_eligible(conn, race, "#E3", "Low Elder", role="elder", peaks=[5, 5])
        _make_eligible(conn, race, "#M1", "Top Member", peaks=[95, 95])
        for idx, peak in enumerate([30, 25, 20, 15, 10, 1]):
            _make_eligible(conn, race, f"#F{idx}", f"F{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(conn=conn)
        demotions = get_demotion_candidates(conn=conn)

        assert result["composition"]["target_elder_max"] == 3
        assert [m["tag"] for m in result["recommended"]] == ["#M1"]
        assert [m["tag"] for m in result["demotion_candidates"]] == ["#E3"]
        assert [m["tag"] for m in demotions["members"]] == ["#E3"]
        assert "outside Elder group" in result["demotion_candidates"][0]["reason"]
    finally:
        conn.close()


def test_elder_cap_is_not_filled_when_current_elder_group_is_stable():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        _make_eligible(conn, race, "#E1", "Top Elder", role="elder", peaks=[100, 100])
        for idx, peak in enumerate([90, 80, 70, 60, 50, 40, 30, 20, 10]):
            _make_eligible(conn, race, f"#M{idx}", f"M{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(conn=conn)

        assert result["composition"]["target_elder_max"] == 3
        assert result["composition"]["elder_selection_count"] == 1
        assert result["recommended"] == []
        assert result["demotion_candidates"] == []
        assert {m["tag"] for m in result["borderline"]} >= {"#M0", "#M1"}
    finally:
        conn.close()


def test_elder_demotion_has_rank_hysteresis_to_avoid_flapping():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        _make_eligible(conn, race, "#E1", "Top Elder", role="elder", peaks=[100, 100])
        _make_eligible(conn, race, "#E2", "Second Elder", role="elder", peaks=[90, 90])
        _make_eligible(conn, race, "#M1", "Rising Member", peaks=[85, 85])
        _make_eligible(conn, race, "#E3", "Near Elder", role="elder", peaks=[80, 80])
        for idx, peak in enumerate([30, 25, 20, 15, 10, 5]):
            _make_eligible(conn, race, f"#F{idx}", f"F{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(conn=conn)

        assert result["composition"]["elder_selection_count"] == 3
        assert result["recommended"] == []
        assert result["demotion_candidates"] == []
        protected = next(m for m in result["borderline"] if m["tag"] == "#E3")
        held = next(m for m in result["borderline"] if m["tag"] == "#M1")
        assert "protected by hysteresis" in protected["reason"]
        assert "until an Elder slot opens" in held["reason"]
    finally:
        conn.close()


def test_recent_battle_activity_is_required_for_elder_board():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        stale_id = _seed_member(conn, "#STALE", "Stale", "member", donations_week=500)
        _seed_daily_metrics(conn, stale_id, peaks=[500, 500])
        _seed_battle(conn, stale_id, days_back=12)
        _seed_war_participation(conn, race, stale_id, "#STALE")
        for idx, peak in enumerate([40, 30, 20, 10, 5, 4, 3, 2, 1]):
            _make_eligible(conn, race, f"#M{idx}", f"M{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(conn=conn)
        stale = next(m for m in result["borderline"] if m["tag"] == "#STALE")

        assert "#STALE" not in {m["tag"] for m in result["recommended"]}
        assert "activity" in stale["missing"]
    finally:
        conn.close()


def test_under_tenure_high_donor_is_not_immediate_promotion_candidate():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        _make_eligible(conn, race, "#E1", "Top Elder", role="elder", peaks=[100, 100])
        _make_eligible(conn, race, "#E2", "Middle Elder", role="elder", peaks=[90, 90])
        _make_eligible(conn, race, "#E3", "Low Elder", role="elder", peaks=[5, 5])
        _make_eligible(
            conn,
            race,
            "#NEW",
            "New Donor",
            peaks=[500, 500],
            joined_date="2026-04-11",
        )
        for idx, peak in enumerate([30, 25, 20, 15, 10, 1]):
            _make_eligible(conn, race, f"#F{idx}", f"F{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(conn=conn)
        fresh = next(m for m in result["borderline"] if m["tag"] == "#NEW")

        assert result["criteria"]["min_tenure_days"] == 21
        assert "#NEW" not in {m["tag"] for m in result["recommended"]}
        assert "tenure" in fresh["missing"]
        assert fresh["tenure_days"] == 7
    finally:
        conn.close()


def test_war_activity_is_required_for_elder_board():
    conn = db.get_connection(":memory:")
    try:
        race = _seed_war_race(conn)
        no_war_id = _seed_member(conn, "#NOWAR", "NoWar", "member", donations_week=500)
        _seed_daily_metrics(conn, no_war_id, peaks=[500, 500])
        _seed_battle(conn, no_war_id)
        for idx, peak in enumerate([40, 30, 20, 10, 5, 4, 3, 2, 1]):
            _make_eligible(conn, race, f"#M{idx}", f"M{idx}", peaks=[peak, peak])

        result = get_promotion_candidates(conn=conn)
        no_war = next(m for m in result["borderline"] if m["tag"] == "#NOWAR")

        assert "#NOWAR" not in {m["tag"] for m in result["recommended"]}
        assert "war" in no_war["missing"]
    finally:
        conn.close()
