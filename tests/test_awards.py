"""Tests for the awards feature — storage grants, detectors, site payloads."""

from __future__ import annotations

import db
from heartbeat import _awards
from modules.poap_kings import site as poap_site


# -- seed helpers -----------------------------------------------------------

def _seed_member(conn, tag: str, name: str = "Tester") -> int:
    canon = tag if tag.startswith("#") else f"#{tag}"
    conn.execute(
        "INSERT INTO members (player_tag, current_name, status, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 'active', '2026-01-01T00:00:00', '2026-04-01T00:00:00')",
        (canon, name),
    )
    return conn.execute("SELECT member_id FROM members WHERE player_tag = ?", (canon,)).fetchone()["member_id"]


def _seed_war_race(conn, season_id: int, section_index: int, our_fame: int = 10000) -> int:
    created = f"2026010{section_index + 1}T100000.000Z"
    conn.execute(
        "INSERT INTO war_races (season_id, section_index, created_date, our_rank, our_fame, total_clans, finish_time) "
        "VALUES (?, ?, ?, 1, ?, 5, ?)",
        (season_id, section_index, created, our_fame, created),
    )
    return conn.execute(
        "SELECT war_race_id FROM war_races WHERE season_id = ? AND section_index = ?",
        (season_id, section_index),
    ).fetchone()["war_race_id"]


def _seed_participation(conn, war_race_id, member_id, tag, fame=3000, decks=16):
    canon = tag if tag.startswith("#") else f"#{tag}"
    conn.execute(
        "INSERT INTO war_participation (war_race_id, member_id, player_tag, player_name, fame, decks_used) "
        "VALUES (?, ?, ?, 'T', ?, ?)",
        (war_race_id, member_id, canon, fame, decks),
    )


def _seed_snapshot(conn, *, season_id, section_index, period_index, member_id, tag, decks_used_today, observed_at="2026-01-01T20:00:00"):
    canon = tag if tag.startswith("#") else f"#{tag}"
    war_day_key = f"s{season_id:05d}-w{section_index:02d}-p{period_index:03d}"
    conn.execute(
        "INSERT INTO war_participant_snapshots "
        "(observed_at, war_day_key, season_id, section_index, period_index, phase, phase_day_number, "
        " member_id, player_tag, player_name, fame, decks_used_today) "
        "VALUES (?, ?, ?, ?, ?, 'battle', ?, ?, ?, 'T', 1000, ?)",
        (observed_at, war_day_key, season_id, section_index, period_index,
         period_index - 2, member_id, canon, decks_used_today),
    )


def _seed_current_war(conn, season_id: int = 131, section_index: int = 0):
    db.upsert_war_current_state(
        {
            "state": "full",
            "seasonId": season_id,
            "sectionIndex": section_index,
            "periodIndex": 3,
            "periodType": "warDay",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 0,
                "repairPoints": 0,
                "periodPoints": 0,
                "clanScore": 100,
                "participants": [],
            },
        },
        conn=conn,
    )


# -- insert_award idempotency -----------------------------------------------

def test_insert_award_is_idempotent():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ABC")
        first = db.insert_award(
            "war_champ", 131, member_id, "#ABC",
            rank=1, metric_value=14230, metric_unit="fame", conn=conn,
        )
        second = db.insert_award(
            "war_champ", 131, member_id, "#ABC",
            rank=1, metric_value=14230, metric_unit="fame", conn=conn,
        )
        assert first is True
        assert second is False
        rows = conn.execute("SELECT COUNT(*) AS c FROM awards").fetchone()
        assert rows["c"] == 1
    finally:
        conn.close()


def test_insert_award_allows_multiple_weekly_rows_per_member():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ABC")
        db.insert_award("perfect_week", 131, member_id, "#ABC",
                        section_index=0, rank=1, conn=conn)
        db.insert_award("perfect_week", 131, member_id, "#ABC",
                        section_index=1, rank=1, conn=conn)
        case = db.get_member_trophy_case(member_id, conn=conn)
        assert len(case) == 2
        assert {c["section_index"] for c in case} == {0, 1}
    finally:
        conn.close()


# -- war_participant detector ------------------------------------------------

def test_detect_war_participant_awards_grants_once_per_member():
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA", "Alice")
        m2 = _seed_member(conn, "#BBB", "Bob")
        # Only Alice has fame this season
        race_id = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race_id, m1, "#AAA", fame=3000)
        _seed_participation(conn, race_id, m2, "#BBB", fame=0)
        # Seed live war state so get_current_season_id returns 131
        _seed_current_war(conn, season_id=131)

        first_pass = _awards.detect_war_participant_awards(conn=conn)
        assert len(first_pass) == 1
        assert first_pass[0]["tag"] == "#AAA"
        assert first_pass[0]["award_type"] == "war_participant"

        # Running again should not double-grant
        second_pass = _awards.detect_war_participant_awards(conn=conn)
        assert second_pass == []
    finally:
        conn.close()


# -- season awards detector --------------------------------------------------

def test_detect_season_awards_skips_in_progress_season():
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA")
        race_id = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race_id, m1, "#AAA", fame=3000)

        signals = _awards.detect_season_awards(conn=conn)
        # Only one season in war_races — it's the current one, not yet ended.
        assert signals == []
        assert conn.execute("SELECT COUNT(*) AS c FROM awards").fetchone()["c"] == 0
    finally:
        conn.close()


def test_detect_season_awards_grants_when_season_ended():
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA", "Alice")
        m2 = _seed_member(conn, "#BBB", "Bob")
        # Season 131 — past, with Alice leading
        for section in range(4):
            race_id = _seed_war_race(conn, season_id=131, section_index=section)
            _seed_participation(conn, race_id, m1, "#AAA", fame=3500, decks=16)
            _seed_participation(conn, race_id, m2, "#BBB", fame=2000, decks=12)
        # Season 132 — current (makes 131 "ended")
        _seed_war_race(conn, season_id=132, section_index=0)

        signals = _awards.detect_season_awards(conn=conn)
        types = [(s["award_type"], s["rank"]) for s in signals]
        # Expect: war_champ rank 1/2, donation_champ ranks would be empty (no
        # donations seeded), iron_king empty (no snapshots seeded), rookie_mvp
        # empty (no clan_memberships seeded). So only war_champ entries.
        assert ("war_champ", 1) in types
        assert ("war_champ", 2) in types

        # Re-running is idempotent
        again = _awards.detect_season_awards(conn=conn)
        assert again == []
    finally:
        conn.close()


# -- iron king query ---------------------------------------------------------

def test_iron_king_requires_all_battle_days_at_four_decks():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        # Two battle days observed for the season. Alice hits 4/4 on both,
        # Bob hits 4/4 only on one. Snapshots dated before the seeded
        # finish_time so both days count as required.
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T06:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=bob, tag="#BBB", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=bob, tag="#BBB", decks_used_today=3,
                       observed_at="2026-01-01T06:00:00")

        candidates = db.get_iron_king_candidates(season_id=131, conn=conn)
        tags = {c["tag"] for c in candidates}
        assert tags == {"#AAA"}
    finally:
        conn.close()


def test_iron_king_ignores_days_after_clan_finish():
    """Players who skipped post-victory battle days still qualify for Iron King."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        # Season 131 has finish_time set to 2026-01-01T10:00:00 (from _seed_war_race).
        # Day 3 has snapshots before finish (05:00) — required.
        # Day 4 has snapshots after finish (12:00) — post-victory, not required.
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=0,
                       observed_at="2026-01-01T12:00:00")

        candidates = db.get_iron_king_candidates(season_id=131, conn=conn)
        assert {c["tag"] for c in candidates} == {"#AAA"}
        assert candidates[0]["total_battle_days"] == 1
    finally:
        conn.close()


def test_iron_king_legacy_fallback_reconstructs_from_war_participation():
    """Seasons without snapshots fall back to war_participation.decks_used."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        cat = _seed_member(conn, "#CCC", "Cat")
        # Season 129 with 2 weeks, no snapshots at all.
        for section in (0, 1):
            race_id = _seed_war_race(conn, season_id=129, section_index=section)
            _seed_participation(conn, race_id, alice, "#AAA", fame=3000, decks=16)
            _seed_participation(conn, race_id, bob, "#BBB", fame=2800, decks=16)
            _seed_participation(conn, race_id, cat, "#CCC", fame=2000, decks=12)

        candidates = db.get_iron_king_candidates(season_id=129, conn=conn)
        tags = {c["tag"] for c in candidates}
        assert tags == {"#AAA", "#BBB"}  # Cat missed a deck somewhere
    finally:
        conn.close()


def test_perfect_week_legacy_fallback_reconstructs_from_war_participation():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        race_id = _seed_war_race(conn, season_id=129, section_index=1)
        _seed_participation(conn, race_id, alice, "#AAA", fame=3000, decks=16)
        _seed_participation(conn, race_id, bob, "#BBB", fame=2800, decks=12)

        candidates = db.get_perfect_week_candidates(
            season_id=129, section_index=1, conn=conn
        )
        assert {c["tag"] for c in candidates} == {"#AAA"}
    finally:
        conn.close()


def test_iron_king_excludes_members_who_missed_any_week():
    """Even a perfect record in weeks played fails if the member missed a week."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        # Two weeks. Alice plays both perfectly; Bob only plays the second week.
        for section in (0, 1):
            race_id = _seed_war_race(conn, season_id=131, section_index=section)
            _seed_participation(conn, race_id, alice, "#AAA", fame=3000, decks=16)
            if section == 1:
                _seed_participation(conn, race_id, bob, "#BBB", fame=3000, decks=16)
            pre_finish = f"2026-01-0{section + 1}T05:00:00"
            _seed_snapshot(conn, season_id=131, section_index=section,
                           period_index=3, member_id=alice, tag="#AAA",
                           decks_used_today=4, observed_at=pre_finish)
            if section == 1:
                _seed_snapshot(conn, season_id=131, section_index=section,
                               period_index=3, member_id=bob, tag="#BBB",
                               decks_used_today=4, observed_at=pre_finish)

        tags = {c["tag"] for c in db.get_iron_king_candidates(season_id=131, conn=conn)}
        assert tags == {"#AAA"}
    finally:
        conn.close()


def test_perfect_week_falls_back_for_section_without_snapshots():
    """A season with snapshots in some weeks still uses legacy data where snapshots are missing."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        # Week 0 has participation but no snapshots — legacy fallback.
        race0 = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race0, alice, "#AAA", fame=3000, decks=16)
        # Week 1 has a snapshot (pre-finish).
        _seed_war_race(conn, season_id=131, section_index=1)
        _seed_snapshot(conn, season_id=131, section_index=1, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-02T05:00:00")

        w0 = db.get_perfect_week_candidates(season_id=131, section_index=0, conn=conn)
        w1 = db.get_perfect_week_candidates(season_id=131, section_index=1, conn=conn)
        assert {c["tag"] for c in w0} == {"#AAA"}
        assert {c["tag"] for c in w1} == {"#AAA"}
    finally:
        conn.close()


def test_perfect_week_boundary_snapshot_does_not_fail_otherwise_perfect_day():
    """A reset-to-zero snapshot at the war-day boundary must not trump in-day peaks."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        # Two in-day snapshots showing 4 decks, plus a boundary snapshot at
        # the reset that landed at 0 — simulating the 10:00 UTC rollover bug.
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T04:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=0,
                       observed_at="2026-01-01T09:59:59")

        tags = {c["tag"] for c in db.get_perfect_week_candidates(
            season_id=131, section_index=0, conn=conn)}
        assert tags == {"#AAA"}
    finally:
        conn.close()


def test_backfill_season_revokes_stale_grants():
    """A hand-inserted award that no longer matches the candidate query is swept."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        # Two weeks; Alice played both perfectly, Bob only one.
        for section in (0, 1):
            race_id = _seed_war_race(conn, season_id=131, section_index=section)
            _seed_participation(conn, race_id, alice, "#AAA", fame=3000, decks=16)
            if section == 1:
                _seed_participation(conn, race_id, bob, "#BBB", fame=3000, decks=16)
            pre_finish = f"2026-01-0{section + 1}T05:00:00"
            _seed_snapshot(conn, season_id=131, section_index=section,
                           period_index=3, member_id=alice, tag="#AAA",
                           decks_used_today=4, observed_at=pre_finish)
            if section == 1:
                _seed_snapshot(conn, season_id=131, section_index=section,
                               period_index=3, member_id=bob, tag="#BBB",
                               decks_used_today=4, observed_at=pre_finish)
        # Next season present → 131 is closed.
        _seed_war_race(conn, season_id=132, section_index=0)

        # Manually insert a stale Iron King grant for Bob (who missed a week).
        db.insert_award("iron_king", 131, bob, "#BBB", conn=conn)

        summary = _awards.backfill_season(131, conn=conn)
        revoked = summary["_revoked"].get("iron_king") or []
        assert [r["player_tag"] for r in revoked] == ["#BBB"]
        remaining = conn.execute(
            "SELECT player_tag FROM awards WHERE award_type='iron_king' AND season_id=131"
        ).fetchall()
        assert [r["player_tag"] for r in remaining] == ["#AAA"]
    finally:
        conn.close()


def test_grant_week_awards_is_idempotent():
    """Re-running grant_week_awards on the same week produces zero new signals."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=2,
                       observed_at="2026-01-01T20:00:00")

        first = _awards.grant_week_awards(131, 0, conn)
        assert len(first) == 2  # perfect_week + victory_lap
        second = _awards.grant_week_awards(131, 0, conn)
        assert second == []
    finally:
        conn.close()


def test_iron_king_empty_when_no_snapshots():
    conn = db.get_connection(":memory:")
    try:
        _seed_member(conn, "#AAA")
        candidates = db.get_iron_king_candidates(season_id=131, conn=conn)
        assert candidates == []
    finally:
        conn.close()


# -- perfect week / victory lap ---------------------------------------------

def test_perfect_week_ignores_days_after_clan_finish():
    """A battle day that started after clan finish_time doesn't count against."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        # Required day: before clan finish at 10:00.
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        # Post-victory day: after finish. Alice played 0, but it shouldn't count.
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=0,
                       observed_at="2026-01-01T12:00:00")

        candidates = db.get_perfect_week_candidates(
            season_id=131, section_index=0, conn=conn
        )
        assert {c["tag"] for c in candidates} == {"#AAA"}
        assert candidates[0]["total_battle_days"] == 1
    finally:
        conn.close()


def test_perfect_week_still_requires_4_on_pre_finish_days():
    """Missing a deck on a required day still disqualifies."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=3,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T12:00:00")

        candidates = db.get_perfect_week_candidates(
            season_id=131, section_index=0, conn=conn
        )
        assert candidates == []
    finally:
        conn.close()


def test_victory_lap_granted_for_post_victory_decks():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        _seed_war_race(conn, season_id=131, section_index=0)
        # Pre-finish day: both play.
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=bob, tag="#BBB", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        # Post-finish day: Alice keeps running laps, Bob stops.
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=3,
                       observed_at="2026-01-01T20:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=bob, tag="#BBB", decks_used_today=0,
                       observed_at="2026-01-01T20:00:00")

        candidates = db.get_victory_lap_candidates(
            season_id=131, section_index=0, conn=conn
        )
        assert {c["tag"] for c in candidates} == {"#AAA"}
        assert candidates[0]["peak_decks"] == 3
    finally:
        conn.close()


def test_victory_lap_empty_when_clan_never_finished():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        # Race with sentinel finish_time — clan never won.
        conn.execute(
            "INSERT INTO war_races (season_id, section_index, created_date, "
            "our_rank, our_fame, total_clans, finish_time) "
            "VALUES (131, 0, '20260101T100000.000Z', 5, 5000, 5, "
            "'19691231T235959.000Z')"
        )
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")

        candidates = db.get_victory_lap_candidates(
            season_id=131, section_index=0, conn=conn
        )
        assert candidates == []
    finally:
        conn.close()


def test_grant_week_awards_emits_victory_lap_signals():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=3,
                       member_id=alice, tag="#AAA", decks_used_today=4,
                       observed_at="2026-01-01T05:00:00")
        _seed_snapshot(conn, season_id=131, section_index=0, period_index=4,
                       member_id=alice, tag="#AAA", decks_used_today=2,
                       observed_at="2026-01-01T20:00:00")

        signals = _awards.grant_week_awards(131, 0, conn)
        types = sorted(s["award_type"] for s in signals)
        assert types == ["perfect_week", "victory_lap"]
        vl = next(s for s in signals if s["award_type"] == "victory_lap")
        assert vl["metric_value"] == 2
        assert vl["metric_unit"] == "decks"
        assert vl["award_display_name"] == "Victory Lap"
    finally:
        conn.close()


# -- weekly donation awards detector ----------------------------------------

def test_detect_weekly_donation_awards_persists_top3_from_signal():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        cat = _seed_member(conn, "#CCC", "Cat")
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_current_war(conn, season_id=131)

        leader_signal = {
            "type": "weekly_donation_leader",
            "week_key": "2026W02",
            "week_ending": "2026-01-11",
            "leaders": [
                {"tag": "#AAA", "name": "Alice", "donations": 1200, "rank": 1},
                {"tag": "#BBB", "name": "Bob", "donations": 900, "rank": 2},
                {"tag": "#CCC", "name": "Cat", "donations": 700, "rank": 3},
            ],
        }
        signals = _awards.detect_weekly_donation_awards([leader_signal], conn=conn)
        assert len(signals) == 3
        ranks = sorted(s["rank"] for s in signals)
        assert ranks == [1, 2, 3]

        # Re-running is idempotent
        again = _awards.detect_weekly_donation_awards([leader_signal], conn=conn)
        assert again == []
    finally:
        conn.close()


# -- site trophy case -------------------------------------------------------

def test_build_trophy_case_returns_member_awards():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ABC", "Alice")
        db.insert_award("war_champ", 131, member_id, "#ABC",
                        rank=1, metric_value=14230, metric_unit="fame", conn=conn)
        db.insert_award("perfect_week", 131, member_id, "#ABC",
                        section_index=2, rank=1, conn=conn)

        case = poap_site.build_trophy_case("ABC", conn=conn)
        assert len(case) == 2
        types = sorted(c["award_type"] for c in case)
        assert types == ["perfect_week", "war_champ"]
    finally:
        conn.close()


def test_backfill_season_closed_grants_all_award_types():
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA", "Alice")
        m2 = _seed_member(conn, "#BBB", "Bob")
        # Closed season 131 with fame + perfect deck usage.
        # Snapshots dated before the seeded finish_time so each battle day
        # counts as required.
        for section in range(3):
            race_id = _seed_war_race(conn, season_id=131, section_index=section)
            _seed_participation(conn, race_id, m1, "#AAA", fame=3500)
            _seed_participation(conn, race_id, m2, "#BBB", fame=2000)
            pre_finish = f"2026-01-0{section + 1}T05:00:00"
            for period in (3, 4):
                _seed_snapshot(conn, season_id=131, section_index=section,
                               period_index=period, member_id=m1, tag="#AAA",
                               decks_used_today=4, observed_at=pre_finish)
                _seed_snapshot(conn, season_id=131, section_index=section,
                               period_index=period, member_id=m2, tag="#BBB",
                               decks_used_today=3, observed_at=pre_finish)
        # Next season present → 131 is closed
        _seed_war_race(conn, season_id=132, section_index=0)

        summary = _awards.backfill_season(131, conn=conn)
        assert len(summary["war_champ"]) == 2
        # Alice hit 4/4 on every battle day — qualifies for Iron King
        iron_tags = [s["tag"] for s in summary["iron_king"]]
        assert iron_tags == ["#AAA"]
        # Re-run is idempotent (ignoring the revoked report key)
        again = _awards.backfill_season(131, conn=conn)
        assert all(not v for k, v in again.items() if k != "_revoked")
        assert not again["_revoked"]
    finally:
        conn.close()


def test_backfill_season_in_progress_skips_season_wide_awards():
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA", "Alice")
        race_id = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race_id, m1, "#AAA", fame=3500)
        _seed_current_war(conn, season_id=131)

        summary = _awards.backfill_season(131, conn=conn)
        # In-progress season: season-wide awards held back, but participant
        # and weekly paths still run.
        assert summary["war_champ"] == []
        assert summary["iron_king"] == []
        assert summary["donation_champ"] == []
        assert summary["rookie_mvp"] == []
        assert len(summary["war_participant"]) == 1
    finally:
        conn.close()


def test_grant_weekly_donation_for_season_reconstructs_from_metrics():
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA", "Alice")
        m2 = _seed_member(conn, "#BBB", "Bob")
        m3 = _seed_member(conn, "#CCC", "Cat")
        # Season race created 2026-01-01; _season_bounds returns [01-01, 01-08].
        # 2026-01-04 is a Sunday inside that window.
        _seed_war_race(conn, season_id=131, section_index=0)
        conn.execute(
            "INSERT INTO member_daily_metrics (member_id, metric_date, donations_week) VALUES (?, ?, ?)",
            (m1, "2026-01-04", 1200),
        )
        conn.execute(
            "INSERT INTO member_daily_metrics (member_id, metric_date, donations_week) VALUES (?, ?, ?)",
            (m2, "2026-01-04", 900),
        )
        conn.execute(
            "INSERT INTO member_daily_metrics (member_id, metric_date, donations_week) VALUES (?, ?, ?)",
            (m3, "2026-01-04", 700),
        )

        signals = _awards.grant_weekly_donation_for_season(131, conn)
        assert len(signals) == 3
        assert [s["rank"] for s in signals] == [1, 2, 3]
        assert signals[0]["tag"] == "#AAA"
    finally:
        conn.close()


def test_build_awards_data_groups_by_season():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ABC", "Alice")
        _seed_war_race(conn, season_id=130, section_index=0)
        _seed_war_race(conn, season_id=131, section_index=0)
        db.insert_award("war_champ", 130, member_id, "#ABC", rank=1, conn=conn)
        db.insert_award("war_champ", 131, member_id, "#ABC", rank=1, conn=conn)

        data = poap_site.build_awards_data(conn=conn)
        assert "generated_at" in data
        season_ids = [s["season_id"] for s in data["seasons"]]
        assert season_ids == [131, 130]  # newest first
        assert all(s["awards"] for s in data["seasons"])
    finally:
        conn.close()
