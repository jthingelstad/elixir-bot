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


def test_insert_award_allows_multiple_podium_ranks_per_season():
    conn = db.get_connection(":memory:")
    try:
        a = _seed_member(conn, "#AAA")
        b = _seed_member(conn, "#BBB")
        db.insert_award("war_champ", 131, a, "#AAA", rank=1, conn=conn)
        db.insert_award("war_champ", 131, b, "#BBB", rank=2, conn=conn)
        all_members = conn.execute(
            "SELECT COUNT(*) AS c FROM awards WHERE award_type='war_champ'"
        ).fetchone()["c"]
        assert all_members == 2
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


def test_iron_king_falls_back_for_section_without_snapshots():
    """A season with snapshots in some weeks uses legacy data for the rest."""
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

        tags = {c["tag"] for c in db.get_iron_king_candidates(season_id=131, conn=conn)}
        assert tags == {"#AAA"}
    finally:
        conn.close()


def test_iron_king_boundary_snapshot_does_not_fail_otherwise_perfect_day():
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

        tags = {c["tag"] for c in db.get_iron_king_candidates(season_id=131, conn=conn)}
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


def test_iron_king_empty_when_no_snapshots():
    conn = db.get_connection(":memory:")
    try:
        _seed_member(conn, "#AAA")
        candidates = db.get_iron_king_candidates(season_id=131, conn=conn)
        assert candidates == []
    finally:
        conn.close()


# -- backfill deprecated sweep ----------------------------------------------

def test_backfill_season_purges_deprecated_award_types():
    """Leftover perfect_week / victory_lap / donation_champ_weekly rows are deleted."""
    conn = db.get_connection(":memory:")
    try:
        m1 = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_war_race(conn, season_id=132, section_index=0)  # closes 131
        # Pre-seed legacy rows that should be swept.
        db.insert_award("perfect_week", 131, m1, "#AAA", section_index=0, conn=conn)
        db.insert_award("victory_lap", 131, m1, "#AAA", section_index=0, conn=conn)
        db.insert_award("donation_champ_weekly", 131, m1, "#AAA", section_index=0, conn=conn)

        summary = _awards.backfill_season(131, conn=conn)
        revoked = summary["_revoked"]
        assert {"perfect_week", "victory_lap", "donation_champ_weekly"} <= set(revoked.keys())
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM awards "
            "WHERE award_type IN ('perfect_week', 'victory_lap', 'donation_champ_weekly')"
        ).fetchone()["c"]
        assert remaining == 0
    finally:
        conn.close()


# -- site trophy case -------------------------------------------------------

def test_build_trophy_case_returns_member_awards():
    conn = db.get_connection(":memory:")
    try:
        member_id = _seed_member(conn, "#ABC", "Alice")
        db.insert_award("war_champ", 131, member_id, "#ABC",
                        rank=1, metric_value=14230, metric_unit="fame", conn=conn)
        db.insert_award("rookie_mvp", 131, member_id, "#ABC",
                        rank=2, conn=conn)

        case = poap_site.build_trophy_case("ABC", conn=conn)
        assert len(case) == 2
        types = sorted(c["award_type"] for c in case)
        assert types == ["rookie_mvp", "war_champ"]
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
