"""The season race is computed once.

There were three implementations. `engine/award_outcomes.py` claims in its own
docstring to be the single authority so that "eligibility, donation tiebreaking,
and rotation cannot drift" — and the other two neither called it nor matched it:

- `storage/war_analytics.get_war_champ_standings` sorted `points, races` and
  filtered `WHERE fame > 0` per row.
- `storage/member_ranks._populate_war_points_rank_season` mirrored *that* one,
  with a docstring saying it aligned with the season-end result.
- `engine.award_outcomes.compute_season_award_outcome` sorts
  `points, donations, tag` and filters `HAVING points > 0` over the season sum.

Both differences change who ranks first, and both paths are member-facing: the
first two reach the `war` capability's standings, `top_contributors` and the
member board; the authority reaches award races and season-close grants. Elixir
could name one War Champ leader in an announcement and rank a different member
first in a tool answer, in the same session.

These tests construct the exact data where the old implementations disagreed
and assert all three surfaces now give one answer.
"""

from __future__ import annotations

import pytest

import db
from engine.award_outcomes import compute_season_award_outcome
from storage.member_ranks import _populate_war_points_rank_season
from storage.war_analytics import get_war_champ_standings

SEASON = 140
# A: fewer races, MORE donations.  B: more races, fewer donations.
# Tied on points, so only the tiebreak separates them.
A, B = "#AAAA", "#BBBB"


@pytest.fixture
def race_db(tmp_path, monkeypatch):
    path = str(tmp_path / "race.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        _seed(conn)
        yield conn
    finally:
        conn.close()


def _member(conn, tag: str, name: str):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-07-30', 1)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, '2026-05-01', '2026-07-30')",
        (tag, name, name),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
        "VALUES (?, '#J2RGCRVG', '2026-05-01', 'test')",
        (tag,),
    )


def _season_window(conn):
    """Donation totals are bounded by the season's war_weeks dates."""
    conn.execute(
        "INSERT OR IGNORE INTO war_seasons (season_id, started_at, ended_at) "
        "VALUES (?, '2026-06-01', '2026-07-14')",
        (SEASON,),
    )
    for section in range(5):
        conn.execute(
            "INSERT OR IGNORE INTO war_weeks (season_id, section_index, created_date, "
            "finish_time) VALUES (?, ?, ?, ?)",
            (SEASON, section, f"2026-06-{1 + section:02d}", f"2026-07-{10 + section:02d}"),
        )


def _week(conn, tag: str, *, section: int, fame: int, decks: int = 4):
    conn.execute(
        "INSERT INTO war_participation (season_id, section_index, player_tag, fame, "
        "decks_used, observed_at) VALUES (?, ?, ?, ?, ?, '2026-07-01T00:00:00')",
        (SEASON, section, tag, fame, decks),
    )


def _seed(conn):
    _member(conn, A, "Ada")
    _member(conn, B, "Ben")
    # Equal season totals (2400 each).
    # A: 4 scoring weeks. B: 4 scoring weeks + one ZERO week, so the two
    # implementations disagreed on B's races_participated (4 vs 5).
    for section in range(4):
        _week(conn, A, section=section, fame=600)
        _week(conn, B, section=section, fame=600)
    _week(conn, B, section=4, fame=0)
    _season_window(conn)
    conn.commit()


def _donations(conn, a: int, b: int):
    for tag, cards in ((A, a), (B, b)):
        conn.execute(
            "INSERT INTO player_daily_metrics (player_tag, metric_date, donations_week) "
            "VALUES (?, '2026-07-01', ?)",
            (tag, cards),
        )
    conn.commit()


def test_the_three_surfaces_agree_on_the_leader(race_db):
    """The assertion that did not exist. A donates more, B races more."""
    _donations(race_db, a=3000, b=1000)

    authority = compute_season_award_outcome(race_db, SEASON)["standings"]
    standings = get_war_champ_standings(season_id=SEASON, conn=race_db)
    ranks = {A: {}, B: {}}
    _populate_war_points_rank_season(race_db, ranks, SEASON)

    assert authority[0]["tag"] == A, "the authority itself changed meaning"
    assert standings[0]["tag"] == A, "the standings surface disagreed with the authority"
    assert ranks[A]["war_points_rank_season"] == 1, "the member board disagreed"
    assert ranks[B]["war_points_rank_season"] == 2


def test_the_tiebreak_is_donations_not_races(race_db):
    """Flip only the donations and the order must flip with it — proving the
    race count is not what is separating them."""
    _donations(race_db, a=1000, b=3000)

    standings = get_war_champ_standings(season_id=SEASON, conn=race_db)
    ranks = {A: {}, B: {}}
    _populate_war_points_rank_season(race_db, ranks, SEASON)

    assert standings[0]["tag"] == B
    assert ranks[B]["war_points_rank_season"] == 1


def test_a_zero_fame_week_still_counts_as_participation(race_db):
    """Filter placement: `HAVING points > 0` over the season sum keeps B's
    zero-fame week in races_participated. The old `WHERE fame > 0` dropped it
    before aggregating, which silently changed both the race count and the
    decks total the standings report."""
    _donations(race_db, a=3000, b=1000)

    by_tag = {row["tag"]: row for row in get_war_champ_standings(season_id=SEASON, conn=race_db)}
    assert by_tag[B]["races_participated"] == 5, "the zero-fame week was dropped"
    assert by_tag[A]["races_participated"] == 4


def test_a_member_with_no_points_at_all_is_excluded(race_db):
    """`HAVING points > 0` is over the SUM, so a member whose every week is
    zero has no standing — but one scoring week is enough to appear."""
    _member(race_db, "#ZERO", "Zed")
    _week(race_db, "#ZERO", section=0, fame=0)
    race_db.commit()
    _donations(race_db, a=3000, b=1000)

    tags = {row["tag"] for row in get_war_champ_standings(season_id=SEASON, conn=race_db)}
    assert "#ZERO" not in tags
    assert {A, B} <= tags
