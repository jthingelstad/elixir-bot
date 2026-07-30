"""Iron King has exactly one definition.

It had three, and they disagreed:

- ``engine/awards.py`` (the season-close GRANT) required perfection across
  every war day of the season, with every section covered.
- ``storage/awards.py`` (the award RACE) dropped the per-section check.
- ``storage/war_analytics.get_perfect_war_participants`` used
  ``perfect_days = battle_days`` — a PER-PLAYER denominator, so a member who
  played a single day perfectly qualified. That one fed the
  ``get_war_season(view="perfect_attendance")`` agent tool, so Elixir told
  members one thing while the grant did another.

The denominator is the CLAN's finalized battle days, never the player's. These
tests pin that, because three functions agreeing today is not a guarantee.
"""

from __future__ import annotations

import pytest

from engine.awards import perfect_attendance

SEASON = 900
CLAN = "#J2RGCRVG"


def _member(conn, tag, name, *, active=True):
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,'2026-07-01','2026-07-11')",
        (tag, name, name),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source, left_at) "
        "VALUES (?,?,'2026-07-01','roster',?)",
        (tag, CLAN, None if active else "2026-07-05"),
    )


def _day(conn, tag, section, day, used, available=4):
    conn.execute(
        "INSERT INTO war_attendance_days (season_id, section_index, war_day_index, "
        "player_tag, decks_used, decks_available, observed_at) VALUES (?,?,?,?,?,?,?)",
        (SEASON, section, day, tag, used, available, "2026-07-11T00:00:00Z"),
    )


@pytest.fixture()
def season(engine_conn):
    """One section, four finalized battle days, four members with distinct shapes."""
    engine_conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) "
        "VALUES (?,'2026-07-01','2026-07-11',1)",
        (CLAN,),
    )
    engine_conn.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (?, '2026-07-01')", (SEASON,)
    )
    engine_conn.execute("INSERT INTO war_weeks (season_id, section_index) VALUES (?, 0)", (SEASON,))

    _member(engine_conn, "#FULL", "Full")  # perfect on all four days
    _member(engine_conn, "#ONE", "OneDay")  # ONE perfect day — the bug's poster child
    _member(engine_conn, "#SLOP", "Sloppy")  # all four days, one imperfect
    _member(engine_conn, "#GONE", "Departed", active=False)  # perfect but left

    for day in range(4):
        _day(engine_conn, "#FULL", 0, day, 4)
        _day(engine_conn, "#GONE", 0, day, 4)
        _day(engine_conn, "#SLOP", 0, day, 4 if day else 1)
    _day(engine_conn, "#ONE", 0, 0, 4)
    engine_conn.commit()
    return engine_conn


def test_the_denominator_is_the_clan_not_the_player(season):
    """A single perfect day must not qualify. This is the bug, in one assert."""
    rows, total_days, skip = perfect_attendance(season, SEASON)
    assert skip is None
    assert total_days == 4
    tags = {r["player_tag"] for r in rows}

    assert "#FULL" in tags
    assert "#ONE" not in tags, (
        "a member perfect on 1 of 4 clan battle days qualified — the per-player denominator is back"
    )
    assert "#SLOP" not in tags
    assert "#GONE" not in tags, "a departed member must not hold current perfect attendance"


def test_every_surface_answers_the_same(season):
    """The grant, the award race and the agent tool must name the same members.

    Each reads through a different layer, so this is the test that actually
    catches a re-divergence.
    """
    import storage.awards as sa
    import storage.war_analytics as wa
    from storage import war_status

    canonical = {r["player_tag"] for r in perfect_attendance(season, SEASON)[0]}

    race = {m["tag"] for m in sa.get_iron_king_candidates(season_id=SEASON, conn=season)}

    # No live war → nothing is excluded, so the tool sees the same four days.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(war_status, "_live_race", lambda *a, **k: None)
        tool = {m["tag"] for m in wa.get_perfect_war_participants(season_id=SEASON, conn=season)}

    assert canonical == race == tool, (
        f"Iron King surfaces disagree — grant={canonical} race={race} tool={tool}"
    )


def test_excluding_the_live_day_changes_which_days_count_not_the_rule(season):
    """QA H10: dropping the unfinished day is a scope change, not a looser rule.

    With day 3 excluded, Sloppy is still out (imperfect on day 0) and OneDay is
    still out (1 of 3). Only the denominator moves.
    """
    rows, total_days, _ = perfect_attendance(season, SEASON, exclude_day=(0, 3))
    assert total_days == 3
    tags = {r["player_tag"] for r in rows}
    assert tags == {"#FULL"}


def test_full_season_guard_is_a_grant_precondition_not_eligibility(engine_conn):
    """Mid-season there are no later war_weeks rows, so the coverage guard must
    not silently empty the in-season view — it only gates the grant."""
    engine_conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) "
        "VALUES (?,'2026-07-01','2026-07-11',1)",
        (CLAN,),
    )
    engine_conn.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (?, '2026-07-01')", (SEASON,)
    )
    _member(engine_conn, "#FULL", "Full")
    _day(engine_conn, "#FULL", 0, 0, 4)
    engine_conn.commit()

    # No war_weeks rows at all: the grant refuses to judge…
    granted, _, skip = perfect_attendance(engine_conn, SEASON)
    assert granted == [] and skip == "insufficient attendance data"

    # …while the in-season view still answers from the days that exist.
    rows, total_days, skip = perfect_attendance(engine_conn, SEASON, require_full_season=False)
    assert skip is None and total_days == 1
    assert {r["player_tag"] for r in rows} == {"#FULL"}
