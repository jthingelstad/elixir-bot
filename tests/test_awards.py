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

        # detect_war_participant_awards is silent in Discord (returns [])
        # but still writes grants to the awards table.
        first_pass = _awards.detect_war_participant_awards(conn=conn)
        assert first_pass == []
        row = conn.execute(
            "SELECT player_tag FROM awards WHERE award_type='war_participant' AND season_id=131"
        ).fetchone()
        assert row is not None
        assert row["player_tag"] == "#AAA"

        # Running again is idempotent — no duplicate rows.
        second_pass = _awards.detect_war_participant_awards(conn=conn)
        assert second_pass == []
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM awards WHERE award_type='war_participant' AND season_id=131"
        ).fetchone()["c"]
        assert count == 1
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
        # detect_season_awards collapses the per-award grants into a single
        # season_awards_granted signal per newly-closed season. The per-award
        # rows still land in the awards table (source of truth for tools).
        assert len(signals) == 1
        s = signals[0]
        assert s["type"] == "season_awards_granted"
        assert s["season_id"] == 131
        # Podium buckets carry War Champ ranks 1 and 2.
        champ_ranks = [e["rank"] for e in s["war_champ"]]
        assert 1 in champ_ranks
        assert 2 in champ_ranks
        # Iron King legacy fallback (decks_used / 4 across 4 sections) sees
        # Alice and Bob as qualifiers too; we just assert the bucket exists.
        assert isinstance(s["iron_kings"], list)
        assert s["donation_champs"] == []
        assert s["rookie_mvps"] == []
        # DB-level grants still happened.
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM awards WHERE award_type='war_champ' AND season_id=131"
        ).fetchone()["c"]
        assert count >= 2

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


# -- season_awards_granted consolidated signal -------------------------------

def test_season_awards_granted_signal_excludes_war_participant():
    """The consolidated podium signal omits war_participant entries."""
    from heartbeat._awards import _build_season_awards_signal
    per_award = [
        {"award_type": "war_champ", "rank": 1, "tag": "#A", "name": "Alice",
         "metric_value": 4500, "metric_unit": "fame", "metadata": {}},
        {"award_type": "war_participant", "rank": 1, "tag": "#C", "name": "Cam",
         "metric_value": 500, "metric_unit": "fame", "metadata": {}},
        {"award_type": "iron_king", "rank": 1, "tag": "#B", "name": "Bob",
         "metric_value": 12, "metric_unit": "battle_days", "metadata": {}},
    ]
    signal = _build_season_awards_signal(131, per_award, "2026-04-06")
    assert signal is not None
    assert signal["type"] == "season_awards_granted"
    assert signal["signal_log_type"] == "season_awards_granted::131"
    assert signal["season_id"] == 131
    assert [e["tag"] for e in signal["war_champ"]] == ["#A"]
    assert [e["tag"] for e in signal["iron_kings"]] == ["#B"]
    # war_participant should not appear anywhere in the payload buckets.
    all_tags = (
        [e["tag"] for e in signal["war_champ"]]
        + [e["tag"] for e in signal["iron_kings"]]
        + [e["tag"] for e in signal["donation_champs"]]
        + [e["tag"] for e in signal["rookie_mvps"]]
    )
    assert "#C" not in all_tags


def test_season_awards_granted_returns_none_when_only_participants():
    """If only war_participant grants are in the list, no signal fires."""
    from heartbeat._awards import _build_season_awards_signal
    per_award = [
        {"award_type": "war_participant", "rank": 1, "tag": "#A", "name": "Alice",
         "metric_value": 500, "metric_unit": "fame", "metadata": {}},
    ]
    assert _build_season_awards_signal(131, per_award, "2026-04-06") is None


def test_season_awards_signal_routes_to_clan_events():
    """plan_signal_outcomes sends season_awards_granted to #clan-events."""
    from runtime.channel_subagents import plan_signal_outcomes
    signal = {"type": "season_awards_granted", "season_id": 131}
    outcomes = plan_signal_outcomes([signal])
    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "clan-events"
    assert outcomes[0]["intent"] == "season_awards_post"
    assert outcomes[0]["required"] is True


# -- list_awards + award_leaderboard helpers ---------------------------------

def test_list_awards_filters_by_season_and_type():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        _seed_war_race(conn, season_id=130, section_index=0)
        _seed_war_race(conn, season_id=131, section_index=0)
        db.insert_award("war_champ", 130, alice, "#AAA", rank=1, conn=conn)
        db.insert_award("war_champ", 130, bob, "#BBB", rank=2, conn=conn)
        db.insert_award("iron_king", 130, alice, "#AAA", rank=1, conn=conn)
        db.insert_award("war_champ", 131, alice, "#AAA", rank=1, conn=conn)

        s130 = db.list_awards(season_id=130, conn=conn)
        assert len(s130) == 3
        assert all(a["season_id"] == 130 for a in s130)

        alice_war_champs = db.list_awards(member_tag="#AAA", award_type="war_champ", conn=conn)
        assert len(alice_war_champs) == 2

        rank1 = db.list_awards(rank=1, conn=conn)
        assert all(a["rank"] == 1 for a in rank1)
    finally:
        conn.close()


def test_award_leaderboard_counts_per_member():
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        cam = _seed_member(conn, "#CCC", "Cam")
        for season in (128, 129, 130):
            _seed_war_race(conn, season_id=season, section_index=0)
            db.insert_award("war_champ", season, alice, "#AAA", rank=1, conn=conn)
        _seed_war_race(conn, season_id=131, section_index=0)
        db.insert_award("war_champ", 131, bob, "#BBB", rank=1, conn=conn)
        db.insert_award("war_champ", 131, cam, "#CCC", rank=2, conn=conn)  # rank-2, ignored

        board = db.award_leaderboard(award_type="war_champ", rank=1, conn=conn)
        assert board[0]["player_tag"] == "#AAA"
        assert board[0]["count"] == 3
        assert board[1]["player_tag"] == "#BBB"
        assert board[1]["count"] == 1
        tags = [r["player_tag"] for r in board]
        assert "#CCC" not in tags  # rank-2 win filtered out
    finally:
        conn.close()


# -- get_awards tool executor ------------------------------------------------

def test_get_awards_tool_list_mode():
    import agent.app  # warms up the full module graph, avoids circular init
    from agent.tool_exec import _execute_get_awards
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        _seed_war_race(conn, season_id=131, section_index=0)
        db.insert_award("war_champ", 131, alice, "#AAA", rank=1, conn=conn)

        # Force the helpers to use our in-memory conn by patching
        # managed_connection default — simplest: use a managed_connection
        # override via monkey-patching isn't available here, so just assert
        # shape via the real DB. The conn we seeded won't be visible to
        # managed_connection (which opens its own); instead we test the
        # shape of the tool's output via a minimal real call.
    finally:
        conn.close()

    out = _execute_get_awards({"mode": "list", "season_id": -1, "limit": 5})
    assert out["mode"] == "list"
    assert out["count"] == 0
    assert out["results"] == []
    assert out["filters"]["season_id"] == -1


def test_get_awards_tool_leaderboard_requires_award_type():
    import agent.app
    from agent.tool_exec import _execute_get_awards
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _execute_get_awards({"mode": "leaderboard"})


# -- get_season_awards_standings unified helper ------------------------------

_AWARDS_PAYLOAD_KEYS = ("war_champ", "iron_kings", "donation_champs", "rookie_mvps")


def test_season_awards_standings_empty_when_no_data():
    conn = db.get_connection(":memory:")
    try:
        result = db.get_season_awards_standings(season_id=999, conn=conn)
        assert result["season_id"] == 999
        for key in _AWARDS_PAYLOAD_KEYS:
            assert result[key] == []
    finally:
        conn.close()


def test_season_awards_standings_returns_signal_shape_mid_season():
    """Mid-season helper returns the same per-entry shape the signal payload uses."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        bob = _seed_member(conn, "#BBB", "Bob")
        # One section logged → mid-season state.
        race_id = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race_id, alice, "#AAA", fame=4500, decks=16)
        _seed_participation(conn, race_id, bob, "#BBB", fame=3000, decks=16)
        # Snapshots so Alice and Bob both qualify for Iron King this section.
        for member, tag in ((alice, "#AAA"), (bob, "#BBB")):
            for period in (3, 4):
                _seed_snapshot(
                    conn, season_id=131, section_index=0, period_index=period,
                    member_id=member, tag=tag, decks_used_today=4,
                    observed_at="2026-01-01T05:00:00",
                )

        result = db.get_season_awards_standings(season_id=131, conn=conn)
        assert result["season_id"] == 131
        # War Champ — top entries in fame order with rank, fame metric, metadata
        assert [e["tag"] for e in result["war_champ"]] == ["#AAA", "#BBB"]
        assert result["war_champ"][0]["rank"] == 1
        assert result["war_champ"][0]["metric_value"] == 4500
        assert result["war_champ"][0]["metric_unit"] == "fame"
        assert "races_participated" in result["war_champ"][0]["metadata"]
        # Iron King — both qualify, rank=1 for everyone
        iron_tags = {e["tag"] for e in result["iron_kings"]}
        assert iron_tags == {"#AAA", "#BBB"}
        assert all(e["rank"] == 1 for e in result["iron_kings"])
        assert all(e["metric_unit"] == "battle_days" for e in result["iron_kings"])
        # Per-entry shape contract — every bucket entry has these keys
        for key in _AWARDS_PAYLOAD_KEYS:
            for entry in result[key]:
                assert set(entry.keys()) >= {"rank", "tag", "name", "metric_value", "metric_unit", "metadata"}
    finally:
        conn.close()


def test_season_awards_standings_uses_current_season_when_id_omitted():
    """Omitting season_id falls back to the current season."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        race_id = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race_id, alice, "#AAA", fame=2000, decks=16)
        _seed_current_war(conn, season_id=131)

        result = db.get_season_awards_standings(conn=conn)
        assert result["season_id"] == 131
        assert [e["tag"] for e in result["war_champ"]] == ["#AAA"]
    finally:
        conn.close()


def test_season_awards_standings_rookie_eligibility_filters_long_tenure():
    """Members joined before season start are excluded from rookie_mvps."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")  # legacy member
        rookie = _seed_member(conn, "#NEW", "Rookie")
        # Long-tenure: clan_membership joined a year before season
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source) "
            "VALUES (?, '2025-01-01T00:00:00.000Z', NULL, 'test')",
            (alice,),
        )
        # Rookie joined during S131 (season starts at the war race created_date,
        # 2026-01-01T10:00:00, so we pick a time after that)
        conn.execute(
            "INSERT INTO clan_memberships (member_id, joined_at, left_at, join_source) "
            "VALUES (?, '2026-01-01T11:00:00.000Z', NULL, 'test')",
            (rookie,),
        )
        race_id = _seed_war_race(conn, season_id=131, section_index=0)
        _seed_participation(conn, race_id, alice, "#AAA", fame=5000)
        _seed_participation(conn, race_id, rookie, "#NEW", fame=3500)

        result = db.get_season_awards_standings(season_id=131, conn=conn)
        rookie_tags = [e["tag"] for e in result["rookie_mvps"]]
        # Long-tenure Alice is excluded, rookie present
        assert "#AAA" not in rookie_tags
        assert "#NEW" in rookie_tags
    finally:
        conn.close()


def test_perfect_war_participants_honors_post_victory_exemption():
    """After reconciliation, perfect-attendance follows the Iron King rule:
    a player who skipped a post-10k-fame battle day still qualifies."""
    conn = db.get_connection(":memory:")
    try:
        alice = _seed_member(conn, "#AAA", "Alice")
        # Single section. _seed_war_race sets finish_time = 2026-01-01T10:00:00,
        # so a snapshot at 12:00 is post-victory and should not count.
        _seed_war_race(conn, season_id=131, section_index=0)
        _seed_snapshot(
            conn, season_id=131, section_index=0, period_index=3,
            member_id=alice, tag="#AAA", decks_used_today=4,
            observed_at="2026-01-01T05:00:00",
        )
        _seed_snapshot(
            conn, season_id=131, section_index=0, period_index=4,
            member_id=alice, tag="#AAA", decks_used_today=0,
            observed_at="2026-01-01T12:00:00",
        )

        result = db.get_perfect_war_participants(season_id=131, conn=conn)
        tags = [r["tag"] for r in result]
        assert tags == ["#AAA"], f"expected Alice to qualify post-victory, got {tags}"
    finally:
        conn.close()
