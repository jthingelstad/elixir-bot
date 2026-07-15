"""Season awards — the Q5 consumer (engine/awards.py): podium, free-pass
rotation metadata, iron-king insufficient-data guard, silent participants,
idempotency. Plus the projected-roster-key regression on
projections.refresh_player_state (live incident 2026-07-04)."""
from __future__ import annotations

import db
import pytest

from engine import awards as engine_awards
from engine import projections
from engine.change_sets import ChangeSetInvariantError

AT = "2026-07-06T10:00:00Z"
SEASON = 133


def _seed_season(conn, *, full_attendance: bool = False):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-06', 1)"
    )
    for tag, name in (("#A", "Alpha"), ("#B", "Bravo"), ("#C", "Carol"),
                      ("#D", "Dora"), ("#R", "Rook")):
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '2026-02-01', '2026-07-06')", (tag, name))
        joined = "2026-06-20" if tag == "#R" else "2026-03-01"  # Rook = mid-season
        conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES (?, ?, 'test')", (tag, joined))
    conn.execute(
        "INSERT INTO war_seasons (season_id, started_at, ended_at, final_rank, weeks, "
        "war_champ_tag, free_pass_tag) VALUES (?, '2026-06-10', ?, 1, 2, '#A', '#B')",
        (SEASON, AT))  # rotation applied: champ #A, pass falls to #B
    # Prior-season war history for the veterans (#A/#B/#C) so they are NOT
    # first-war-season rookies. #R has no prior war → the only Rookie MVP.
    conn.execute(
        "INSERT OR IGNORE INTO war_seasons (season_id, started_at, free_pass_tag) "
        "VALUES (132, '2026-05-10', '#A')"
    )
    for tag in ("#A", "#B", "#C"):
        conn.execute(
            "INSERT INTO war_participation (season_id, section_index, player_tag, "
            "fame, decks_used, observed_at) VALUES (132, 0, ?, 1000, 16, ?)", (tag, AT))
    for section in (3, 4):
        conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, created_date, finish_time) "
            "VALUES (?, ?, ?, ?)",
            (SEASON, section, f"2026-06-{10 + section}", f"2026-06-{17 + section}"))
        for tag, fame in (("#A", 3000), ("#B", 2500), ("#C", 2000), ("#R", 900)):
            conn.execute(
                "INSERT INTO war_participation (season_id, section_index, player_tag, "
                "fame, decks_used, observed_at) VALUES (?, ?, ?, ?, 16, ?)",
                (SEASON, section, tag, fame, AT))
    # attendance: full coverage only when asked (else section 4 only — the
    # real s133 situation)
    sections = (3, 4) if full_attendance else (4,)
    for section in sections:
        for day in (0, 1):
            for tag, used in (("#A", 4), ("#B", 4), ("#C", 3)):
                conn.execute(
                    "INSERT INTO war_attendance_days (season_id, section_index, "
                    "war_day_index, player_tag, decks_used, decks_available, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, 4, ?)",
                    (SEASON, section, day, tag, used, AT))
    # donations: Carol leads
    for d, (tag, don) in enumerate((("#C", 400), ("#A", 250), ("#B", 100))):
        conn.execute(
            "INSERT INTO player_daily_metrics (player_tag, metric_date, donations_week) "
            "VALUES (?, ?, ?)", (tag, f"2026-06-1{5 + d}", don))
    conn.commit()


def test_grant_season_awards_full_slate():
    conn = db.get_connection()
    try:
        _seed_season(conn)
        grants = engine_awards.grant_season_awards(conn, SEASON, AT)

        podium = grants["war_champ"]
        assert [(e["rank"], e["tag"]) for e in podium] == [(1, "#A"), (2, "#B"), (3, "#C")]
        assert podium[0]["metric_value"] == 6000  # 3000 × 2 sections

        fp = grants["free_pass"]
        assert len(fp) == 1 and fp[0]["tag"] == "#B" and fp[0]["rotation_applied"] is True
        fp_rows = conn.execute(
            "SELECT COUNT(*) FROM awards WHERE award_type='free_pass' AND season_id=?",
            (SEASON,)).fetchone()[0]
        assert fp_rows == 1

        # s133 reality: attendance only covers one of two sections → guarded skip
        assert grants["iron_kings"] == []
        assert grants["iron_king_skipped"] == "insufficient attendance data"

        assert [e["tag"] for e in grants["donation_champs"]] == ["#C", "#A", "#B"]
        assert [e["tag"] for e in grants["rookie_mvps"]] == ["#R"]  # first-war-season only
        assert grants["war_participants"] == 4  # A, B, C, R (fame > 0)

        # ledger keys claimed per recognition.md §5
        claims = conn.execute(
            "SELECT COUNT(*) FROM recognition_ledger WHERE recognition_key LIKE 'award:%'"
        ).fetchone()[0]
        assert claims == grants["granted"]

        # idempotent: re-run grants nothing
        again = engine_awards.grant_season_awards(conn, SEASON, AT)
        assert again["granted"] == 0
    finally:
        conn.close()


def test_season_close_award_failure_rolls_back_as_one_transition(
    engine_conn, monkeypatch,
):
    from engine.emitters.war import close_season

    _seed_season(engine_conn)
    engine_conn.execute(
        "UPDATE war_seasons SET ended_at = NULL WHERE season_id = ?", (SEASON,)
    )
    engine_conn.commit()

    def fail_awards(*args, **kwargs):
        raise RuntimeError("injected award failure")

    monkeypatch.setattr(engine_awards, "grant_season_awards", fail_awards)
    with pytest.raises(RuntimeError, match="injected award failure"):
        close_season(engine_conn, SEASON, {}, AT)
    engine_conn.rollback()

    ended_at = engine_conn.execute(
        "SELECT ended_at FROM war_seasons WHERE season_id = ?", (SEASON,)
    ).fetchone()[0]
    event_count = engine_conn.execute(
        "SELECT COUNT(*) FROM war_events WHERE dedup_key = ?",
        (f"season_closed:{SEASON}",),
    ).fetchone()[0]
    assert ended_at is None
    assert event_count == 0


def test_season_close_invariant_catches_silent_award_omission(
    engine_conn, monkeypatch,
):
    from engine.emitters.war import close_season

    _seed_season(engine_conn)
    engine_conn.execute(
        "UPDATE war_seasons SET ended_at = NULL WHERE season_id = ?", (SEASON,)
    )
    engine_conn.commit()
    monkeypatch.setattr(engine_awards, "grant_season_awards", lambda *args, **kwargs: {})

    with pytest.raises(ChangeSetInvariantError, match="awards missing"):
        close_season(engine_conn, SEASON, {}, AT)
    engine_conn.rollback()
    assert engine_conn.execute(
        "SELECT ended_at FROM war_seasons WHERE season_id = ?", (SEASON,)
    ).fetchone()[0] is None


def test_replayed_season_close_preserves_recorded_outcome(engine_conn):
    from engine.emitters.war import close_season

    _seed_season(engine_conn)
    engine_conn.execute(
        "UPDATE war_seasons SET ended_at = NULL WHERE season_id = ?", (SEASON,)
    )
    close_season(engine_conn, SEASON, {}, AT)
    recorded = engine_conn.execute(
        "SELECT player_tag, rank FROM awards "
        "WHERE season_id = ? AND award_type = 'donation_champ' ORDER BY rank",
        (SEASON,),
    ).fetchall()

    # Later state can change every input used by the live preview. Replaying
    # the same boundary still treats the recorded close as historical fact.
    engine_conn.execute(
        "UPDATE clan_memberships SET left_at = '2026-07-07' WHERE player_tag = '#C'"
    )
    engine_conn.execute(
        "UPDATE player_daily_metrics SET donations_week = 9999 WHERE player_tag = '#A'"
    )
    assert close_season(engine_conn, SEASON, {}, "2026-07-08T00:00:00Z") == 0
    assert engine_conn.execute(
        "SELECT player_tag, rank FROM awards "
        "WHERE season_id = ? AND award_type = 'donation_champ' ORDER BY rank",
        (SEASON,),
    ).fetchall() == recorded


def test_tie_aware_ranks_share_a_rank_and_flag_ties():
    """Competition ranking (1,2,2,4) with tie flags — so Elixir can say
    'three tied for 2nd' instead of inventing an order between equal points."""
    from storage.awards import _apply_tie_aware_ranks
    rows = [{"points": 2700}, {"points": 2400}, {"points": 2400},
            {"points": 2400}, {"points": 2150}]
    _apply_tie_aware_ranks(rows, "points")
    assert [r["rank"] for r in rows] == [1, 2, 2, 2, 5]
    assert [r["tied"] for r in rows] == [False, True, True, True, False]
    assert rows[1]["tie_count"] == 3


def test_award_races_tie_aware_and_iron_king_unranked():
    """get_award_races: War Champ top-N tie-aware with points; Iron King is a
    participation list (no ranks); Rookie MVP is first-war-season only."""
    conn = db.get_connection()
    try:
        _seed_season(conn, full_attendance=True)
        races = db.get_award_races(season_id=SEASON, conn=conn)
        champ = races["war_champ"]
        # #A 6000, #B 5000, #C 4000, #R 1800 — all distinct here, but points present
        assert champ[0]["name"] == "Alpha" and champ[0]["points"] == 6000
        assert races["war_champ_leader"]["tag"] == "#A"
        # Iron King = participation list (full-attendance seed → #A/#B perfect),
        # every entry on_track, no rank/podium field.
        assert races["iron_king"] and all(e["on_track"] for e in races["iron_king"])
        assert all("rank" not in e for e in races["iron_king"])
        # Rookie MVP: only the first-war-season member (#R).
        assert [r["tag"] for r in races["rookie_mvp"]] == ["#R"]
    finally:
        conn.close()


def test_war_season_history_is_the_free_pass_lineage(engine_conn):
    """The deep history Elixir reflects on a season recap / free-pass
    designation: season-by-season champ + free-pass winner, newest first,
    rotation flagged, repeat holders surfaced, capped to the rolling window —
    every name resolved from stored war_seasons tags, never invented."""
    from storage.war_analytics import get_war_season_history
    c = engine_conn
    for tag, name in (("#LEVY", "King Levy"), ("#RAQ", "raquaza"),
                      ("#28", "28"), ("#ATT", "Atternam"), ("#NEW", "Newbie")):
        c.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
                  "VALUES (?, ?, '2026-01-01', '2026-07-06')", (tag, name))
    # champ==free_pass except S133 where it ROTATED off the champ (#28 held S131)
    seasons = [
        (129, 1, "#LEVY", "#LEVY"),
        (130, 1, "#RAQ", "#RAQ"),
        (131, 1, "#28", "#28"),
        (132, 1, "#ATT", "#ATT"),
        (133, 1, "#28", "#ATT"),   # champ #28 held it last month → rotates to #ATT
    ]
    for sid, rank, champ, fp in seasons:
        c.execute("INSERT INTO war_seasons (season_id, started_at, final_rank, war_champ_tag, free_pass_tag) "
                  "VALUES (?, '2026-01-01', ?, ?, ?)", (sid, rank, champ, fp))
    c.commit()

    hist = get_war_season_history(conn=c)
    assert [s["season_id"] for s in hist["seasons"]] == [133, 132, 131, 130, 129]  # newest first
    assert hist["seasons"][0]["war_champ"]["name"] == "28"       # tag resolved to name
    assert hist["seasons"][0]["free_pass"]["name"] == "Atternam"
    assert hist["seasons"][0]["rotation_applied"] is True        # champ != free pass
    assert hist["seasons"][1]["rotation_applied"] is False       # S132 champ took it
    # Atternam held the free pass in 132 and 133 → repeat holder.
    assert hist["repeat_free_pass_holders"] == [
        {"tag": "#ATT", "name": "Atternam", "season_ids": [132, 133]}
    ]

    # rolling window caps the lineage
    assert get_war_season_history(limit=2, conn=c)["seasons_shown"] == 2
    assert [s["season_id"] for s in get_war_season_history(limit=2, conn=c)["seasons"]] == [133, 132]


def test_war_champ_tie_breaks_on_cards_donated(engine_conn):
    """Equal War Champ points are a genuine tie (both flagged, same rank), but the
    leader/#1 is the higher season donor (Jamie's tiebreak)."""
    c = engine_conn
    c.execute("INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
              "VALUES ('#J2RGCRVG','POAP KINGS','2026-02-04','2026-07-06',1)")
    c.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (150, '2026-06-10')"
    )
    c.execute("INSERT INTO war_weeks (season_id, section_index, created_date, finish_time) "
              "VALUES (150, 0, '2026-06-11', '2026-06-18')")
    for tag, name, don in (("#HI", "HiDonor", 500), ("#LO", "LoDonor", 100)):
        c.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
                  "VALUES (?, ?, '2026-02-01', '2026-07-06')", (tag, name))
        c.execute("INSERT INTO clan_memberships (player_tag, joined_at, join_source) VALUES (?, '2026-03-01', 'test')", (tag,))
        c.execute("INSERT INTO war_participation (season_id, section_index, player_tag, fame, decks_used, observed_at) "
                  "VALUES (150, 0, ?, 2400, 16, ?)", (tag, AT))  # EQUAL points
        c.execute("INSERT INTO player_daily_metrics (player_tag, metric_date, donations_week) VALUES (?, '2026-06-15', ?)", (tag, don))
    c.commit()
    champ = db.get_award_races(season_id=150, conn=c)["war_champ"]
    assert [e["name"] for e in champ] == ["HiDonor", "LoDonor"]   # higher donor first
    assert champ[0]["donations"] == 500 and champ[1]["donations"] == 100
    assert champ[0]["rank"] == champ[1]["rank"] and champ[0]["tied"] and champ[1]["tied"]  # still a points tie
    assert db.get_award_races(season_id=150, conn=c)["war_champ_leader"]["name"] == "HiDonor"

    # Season close consumes the exact same outcome: no second implementation
    # can silently choose the low donor after the live race showed HiDonor.
    from engine.emitters.war import close_season

    close_season(c, 150, {}, AT)
    season = c.execute(
        "SELECT war_champ_tag, free_pass_tag FROM war_seasons WHERE season_id=150"
    ).fetchone()
    recorded = c.execute(
        "SELECT player_tag FROM awards WHERE season_id=150 AND award_type='war_champ' "
        "ORDER BY rank LIMIT 1"
    ).fetchone()
    assert season["war_champ_tag"] == "#HI"
    assert season["free_pass_tag"] == "#HI"
    assert recorded["player_tag"] == "#HI"


def test_award_outcome_excludes_departed_members_everywhere(engine_conn):
    """The preview and official close share the active-member eligibility rule."""
    from engine.award_outcomes import compute_season_award_outcome
    from engine.emitters.war import close_season

    c = engine_conn
    c.execute(
        "INSERT OR IGNORE INTO clans "
        "(clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-01-01', '2026-07-06', 1)"
    )
    c.execute("INSERT INTO war_seasons (season_id, started_at) VALUES (151, '2026-07-01')")
    c.execute("INSERT INTO war_weeks (season_id, section_index, created_date) VALUES (151, 0, '2026-07-01')")
    for tag, name, points, left_at in (
        ("#ACTIVE", "Active", 2000, None),
        ("#LEFT", "Departed", 9000, "2026-07-05"),
    ):
        c.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '2026-01-01', '2026-07-06')", (tag, name))
        c.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, left_at, join_source) "
            "VALUES (?, '2026-01-01', ?, 'test')", (tag, left_at))
        c.execute(
            "INSERT INTO war_participation "
            "(season_id, section_index, player_tag, fame, decks_used, observed_at) "
            "VALUES (151, 0, ?, ?, 16, '2026-07-06')", (tag, points))

    preview = compute_season_award_outcome(c, 151)
    assert [entry["tag"] for entry in preview["standings"]] == ["#ACTIVE"]
    close_season(c, 151, {}, "2026-07-06T10:00:00Z")
    official = c.execute(
        "SELECT war_champ_tag FROM war_seasons WHERE season_id=151"
    ).fetchone()
    assert official["war_champ_tag"] == "#ACTIVE"


def test_award_preview_and_grant_share_rookie_and_donation_outcomes(engine_conn):
    """A mid-season join is not a rookie if they fought in an earlier season;
    donation ties and every final grant use the preview's exact ordering."""
    from engine.award_outcomes import compute_season_award_outcome

    c = engine_conn
    c.execute(
        "INSERT OR IGNORE INTO clans "
        "(clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-01-01', '2026-07-06', 1)"
    )
    c.execute("INSERT INTO war_seasons (season_id, started_at) VALUES (160, '2026-06-01')")
    c.execute("INSERT INTO war_seasons (season_id, started_at) VALUES (161, '2026-07-01')")
    c.execute(
        "INSERT INTO war_weeks (season_id, section_index, created_date, finish_time) "
        "VALUES (161, 0, '2026-07-01', '2026-07-08')"
    )
    for tag, name, points in (
        ("#VET", "Veteran", 500),
        ("#NEW", "Newcomer", 900),
        ("#TIE", "Tie Donor", 700),
    ):
        c.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, '2026-01-01', '2026-07-06')", (tag, name),
        )
        c.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES (?, '2026-07-02', 'test')", (tag,),
        )
        c.execute(
            "INSERT INTO war_participation "
            "(season_id, section_index, player_tag, fame, decks_used, observed_at) "
            "VALUES (161, 0, ?, ?, 16, ?)", (tag, points, AT),
        )
        c.execute(
            "INSERT INTO player_daily_metrics "
            "(player_tag, metric_date, donations_week) VALUES (?, '2026-07-05', 100)",
            (tag,),
        )
    c.execute(
        "INSERT INTO war_participation "
        "(season_id, section_index, player_tag, fame, decks_used, observed_at) "
        "VALUES (160, 0, '#VET', 100, 4, ?)", (AT,),
    )

    preview = compute_season_award_outcome(c, 161)
    assert [r["tag"] for r in preview["rookie_mvps"]] == ["#NEW", "#TIE"]
    # Equal donation totals use the immutable tag as the final deterministic key.
    assert [r["tag"] for r in preview["donation_champs"]] == ["#NEW", "#TIE", "#VET"]

    grants = engine_awards.grant_season_awards(c, 161, AT, outcome=preview)
    assert [r["tag"] for r in grants["rookie_mvps"]] == ["#NEW", "#TIE"]
    assert [r["tag"] for r in grants["donation_champs"]] == ["#NEW", "#TIE", "#VET"]


def test_award_leaderboard_accepts_rank_and_limit():
    """QA H19: get_awards(mode='leaderboard') passed rank/limit that the storage
    fn didn't accept, so every call raised TypeError. Now it filters/caps."""
    from agent.tool_exec import _execute_get_awards
    conn = db.get_connection()
    try:
        _seed_season(conn)
        engine_awards.grant_season_awards(conn, SEASON, AT)
        conn.commit()
        # all-time counts, no crash
        board = db.award_leaderboard(award_type="war_champ")
        assert board and all("count" in r and "player_name" in r for r in board)
        # rank filter (1st-place only) + limit cap both apply
        top = db.award_leaderboard(award_type="war_champ", rank=1, limit=2)
        assert len(top) <= 2
        # the tool path no longer raises
        out = _execute_get_awards({"mode": "leaderboard", "award_type": "war_champ"})
        assert isinstance(out, dict) and out["mode"] == "leaderboard" and out["count"] >= 1
    finally:
        conn.close()


def test_iron_king_grants_with_full_coverage():
    conn = db.get_connection()
    try:
        _seed_season(conn, full_attendance=True)
        grants = engine_awards.grant_season_awards(conn, SEASON, AT)
        assert grants["iron_king_skipped"] is None
        assert [e["tag"] for e in grants["iron_kings"]] == ["#A", "#B"]  # 4/4 every day
    finally:
        conn.close()


def test_refresh_player_state_enforces_profile_and_roster_field_ownership():
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
            "VALUES ('#X', 'Xeno', '2026-01-01', '2026-07-06')")
        # projected (snake_case) roster entry — the shape the tick passes
        projections.refresh_player_state(conn, "#X", None, {
            "role": "member", "trophies": 5100, "donations": 42,
            "donations_received": 8, "exp_level": 44,
            "clan_rank": 7, "previous_clan_rank": 9,
        }, AT)
        row = conn.execute(
            "SELECT exp_level, clan_rank, previous_clan_rank, donations_received_week "
            "FROM player_current_state WHERE player_tag='#X'").fetchone()
        assert (row["exp_level"], row["clan_rank"], row["previous_clan_rank"],
                row["donations_received_week"]) == (None, 7, 9, 8)
        # A deep profile owns exp_level; a later sparse roster refresh preserves it.
        projections.refresh_player_state(
            conn, "#X", {"expLevel": 44}, None, AT
        )
        projections.refresh_player_state(conn, "#X", None, {"trophies": 5150}, AT)
        row = conn.execute(
            "SELECT exp_level, clan_rank FROM player_current_state "
            "WHERE player_tag='#X'").fetchone()
        assert (row["exp_level"], row["clan_rank"]) == (44, 7)
    finally:
        conn.close()
