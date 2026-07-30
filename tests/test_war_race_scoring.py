"""A River Race exposes TWO scoreboards and they must never be coalesced:

- **fame** = the weekly cumulative boat race (who wins the week); awarded at each
  day's close by that day's rank. 0 until the first battle day closes.
- **period points** = today's race (what members drive); resets each day.

The old code did max(fame, periodPoints) per clan and called the winner "the
score", so on a live battle day it paired OUR daily period points against rivals'
weekly fame (the loop #44 bug: "10,525 points, rank 1 — R.E.I.C.H at 1,800").
These tests pin the two races apart.
"""

from unittest.mock import patch

from runtime.awareness.read import _standing_block
from storage import war_status


def _projection(clans, *, period_type="warDay", period_index=4, our_tag="#US"):
    us = clans.get(our_tag, {})
    return {
        "season_id": 134,
        "section_index": 0,
        "period_index": period_index,
        "period_type": period_type,
        "our_tag": our_tag,
        "our_fame": us.get("fame", 0),
        "clans": clans,
        "participants": {},
    }


# The loop #44 moment: mid Battle Day 2, boat scored on Day 1, today's race live.
LOOP44 = {
    "#US": {"name": "POAP KINGS", "fame": 3435, "period_points": 10525},
    "#RIV": {"name": "R.E.I.C.H", "fame": 1800, "period_points": 450},
    "#OTH": {"name": "euromix", "fame": 1053, "period_points": 350},
}


def _status(clans, **kw):
    with patch.object(
        war_status,
        "_live_race",
        return_value=(_projection(clans, **kw), "2026-07-11T07:06:00Z"),
    ):
        return war_status.get_current_war_status(conn=None)


def test_two_races_are_kept_separate():
    war = _status(LOOP44)
    # fame is the WEEKLY boat (raw), period_points is TODAY — both first-class.
    assert war["fame"] == 3435
    assert war["period_points"] == 10525
    # Ranked independently: fame race 3435>1800>1053; day race 10525>450>350.
    assert war["race_rank"] == 1
    assert war["day_rank"] == 1
    assert war["primary_metric"] == "fame"
    assert war["boat_scored"] is True
    assert war["day_scored"] is True
    # The coalescing artifacts are gone.
    for dead in ("active_score", "score_source", "score_label", "raw_fame"):
        assert dead not in war


def test_daily_points_never_complete_the_weekly_race():
    # 10,525 period points > the 10,000 fame finish line, but the WEEKLY race is
    # fame (3,435) — it is NOT complete. This was the false-positive bug.
    war = _status(LOOP44)
    assert war["finish_line"] == 10000
    assert war["race_completed"] is False


def test_colosseum_has_no_finish_line_and_score_never_completes_it():
    colosseum = {
        "#US": {"name": "POAP KINGS", "fame": 40200, "period_points": 9000},
        "#RIV": {"name": "R.E.I.C.H", "fame": 700, "period_points": 500},
    }
    war = _status(colosseum, period_type="colosseum", period_index=31)
    assert war["finish_line"] is None
    assert war["race_completed"] is False
    assert war["clinches_finish_today"] is False


def test_standings_are_single_field_and_never_mixed():
    war = _status(LOOP44)
    # race_standings ordered by fame; day_standings by period points.
    assert [s["clan_name"] for s in war["race_standings"]] == [
        "POAP KINGS",
        "R.E.I.C.H",
        "euromix",
    ]
    assert [s["clan_name"] for s in war["day_standings"]] == [
        "POAP KINGS",
        "R.E.I.C.H",
        "euromix",
    ]
    # Every entry carries BOTH raw numbers so no caller has to re-mix.
    for s in war["race_standings"]:
        assert "fame" in s and "period_points" in s

    sb = _standing_block(war)
    # Weekly scoreboard is fame-only; today scoreboard is period-points-only.
    assert sb["weekly"]["fame"] == 3435
    assert all(set(e) == {"name", "fame", "rank"} for e in sb["weekly"]["scoreboard"])
    assert sb["weekly"]["scoreboard"][1] == {
        "name": "R.E.I.C.H",
        "fame": 1800,
        "rank": 2,
    }
    assert sb["today"]["period_points"] == 10525
    assert all(set(e) == {"name", "period_points", "rank"} for e in sb["today"]["scoreboard"])
    assert sb["today"]["scoreboard"][1] == {
        "name": "R.E.I.C.H",
        "period_points": 450,
        "rank": 2,
    }
    # Never our period points next to a rival's fame anywhere in the block.
    assert "period_points" not in sb["weekly"]["scoreboard"][0]
    assert "fame" not in sb["today"]["scoreboard"][0]


def test_projected_day_fame_mirrors_in_game_boat_reward():
    # Leading today (1st in period points) projects +3,000 fame at day close;
    # the read surfaces it as projected_fame_if_held.
    war = _status(LOOP44)
    assert war["projected_day_fame"] == 3000
    assert _standing_block(war)["today"]["projected_fame_if_held"] == 3000
    # 2nd today would project +1,800.
    second = {
        "#US": {"name": "POAP KINGS", "fame": 3435, "period_points": 400},
        "#RIV": {"name": "R.E.I.C.H", "fame": 1800, "period_points": 900},
    }
    assert _status(second)["projected_day_fame"] == 1800
    # No projection before the day scores, or in Colosseum (no fame).
    fresh = {"#US": {"name": "POAP KINGS", "fame": 6870, "period_points": 0}}
    assert _status(fresh, period_index=5)["projected_day_fame"] is None


def test_day_one_boat_not_scored():
    # Battle Day 1: no day has closed, so every clan's fame is still 0; the live
    # action is today's period-point race.
    day1 = {
        "#US": {"name": "POAP KINGS", "fame": 0, "period_points": 3000},
        "#RIV": {"name": "R.E.I.C.H", "fame": 0, "period_points": 1200},
        "#OTH": {"name": "euromix", "fame": 0, "period_points": 900},
    }
    war = _status(day1, period_index=3)
    assert war["boat_scored"] is False
    assert war["day_scored"] is True
    assert war["day_rank"] == 1
    sb = _standing_block(war)
    assert sb["weekly"]["boat_scored"] is False  # weekly present but flagged unscored
    assert sb["today"]["period_points"] == 3000


def test_unranked_race_when_no_clan_has_scored():
    """A practice day / start of week: every clan at 0 fame → the race is NOT
    ranked. The brain must not read the API's arbitrary tiebreaker order as a
    standing (the real 'rank 3' bug). rank is null, race_ranked is false."""
    practice = {
        "#RIV": {"name": "R.E.I.C.H", "fame": 0, "period_points": 0},
        "#GRV": {"name": "Graveborn", "fame": 0, "period_points": 0},
        "#US": {"name": "POAP KINGS", "fame": 0, "period_points": 0},
    }
    war = _status(practice, period_type="training", period_index=1)
    weekly = _standing_block(war)["weekly"]
    assert weekly["race_ranked"] is False
    assert weekly["rank"] is None
    assert weekly["unranked_reason"]
    # Every scoreboard rank is nulled too — none of them mean anything yet.
    assert all(c["rank"] is None for c in weekly["scoreboard"])


def test_ranked_race_keeps_rank_once_a_clan_scores():
    scored = {
        "#US": {"name": "POAP KINGS", "fame": 6870, "period_points": 0},
        "#RIV": {"name": "R.E.I.C.H", "fame": 3600, "period_points": 0},
    }
    weekly = _standing_block(_status(scored, period_index=5))["weekly"]
    assert weekly["race_ranked"] is True
    assert weekly["rank"] == 1


def test_no_daily_action_suppresses_meaningless_day_rank():
    # Start of a fresh day: all period points 0. day_scored must be False so the
    # brain never reads a 0-0-0 tie as "losing today".
    fresh = {
        "#US": {"name": "POAP KINGS", "fame": 6870, "period_points": 0},
        "#RIV": {"name": "R.E.I.C.H", "fame": 3600, "period_points": 0},
    }
    war = _status(fresh, period_index=5)
    assert war["day_scored"] is False
    assert war["boat_scored"] is True
    sb = _standing_block(war)
    assert sb["today"] is None  # daily race suppressed until it scores
    assert sb["weekly"]["fame"] == 6870


def test_boat_defense_fame_clinches_finish_today():
    """Boat-defense fame (from the API's periodLogs) turns a 'just short'
    placement projection into finishing tonight — the Loop #50 gap. Live BD3:
    6,870 + 3,000 placement = 9,870 (130 short) but + 435 defenses = 10,305."""
    proj = {
        "season_id": 134,
        "section_index": 0,
        "period_index": 5,
        "period_type": "warDay",
        "our_tag": "#US",
        "our_fame": 6870,
        "our_defense": {
            "defense_fame_recent": 435,
            "defenses_remaining": 15,
            "defense_fame_days": [435, 435],
        },
        "clans": {
            "#US": {"name": "POAP KINGS", "fame": 6870, "period_points": 3900},
            "#R": {"name": "R.E.I.C.H", "fame": 3600, "period_points": 0},
        },
        "participants": {},
    }
    with patch.object(war_status, "_live_race", return_value=(proj, "2026-07-11T13:06:00Z")):
        war = war_status.get_current_war_status(conn=None)
    assert war["projected_day_fame"] == 3000  # placement only
    assert war["projected_defense_fame"] == 435  # from the API, not back-calc
    assert war["projected_fame_at_close"] == 10305  # 6870 + 3000 + 435
    assert war["clinches_finish_today"] is True  # 9870 alone would read "130 short"
    assert war["defenses_remaining"] == 15

    # Without defenses standing, placement alone is 130 short → does NOT clinch.
    proj["our_defense"] = {
        "defense_fame_recent": 0,
        "defenses_remaining": 0,
        "defense_fame_days": [],
    }
    with patch.object(war_status, "_live_race", return_value=(proj, "2026-07-11T13:06:00Z")):
        war2 = war_status.get_current_war_status(conn=None)
    assert war2["projected_fame_at_close"] == 9870 and war2["clinches_finish_today"] is False


def test_war_season_snapshot_exposes_live_race(engine_conn):
    """get_war_season_snapshot (feeds get_elixir_state) must map the real
    get_current_war_status keys — regression for QA H13/H14 where it read stale
    'standings'/'our_fame'/'day_number' and returned an empty race block."""
    with patch.object(
        war_status,
        "_live_race",
        return_value=(_projection(LOOP44), "2026-07-11T07:06:00Z"),
    ):
        snap = war_status.get_war_season_snapshot(conn=engine_conn)
    race = snap["state"]["race"]
    assert race["fame"] == 3435 and race["race_rank"] == 1
    assert [s["clan_name"] for s in race["race_standings"]] == [
        "POAP KINGS",
        "R.E.I.C.H",
        "euromix",
    ]
    assert race["period_points"] == 10525 and race["day_rank"] == 1
    assert race["primary_metric"] == "fame"
    assert snap["state"]["day_number"] is not None
    # The old buggy keys must be gone.
    assert "standings" not in race and "our_fame" not in race


def test_war_season_snapshot_nulls_ranks_on_unranked_race(engine_conn):
    """The 'six straight weeks / rank 3 showing' incident: on a practice day every
    clan sits at 0 fame, but state.race passed the API's raw tiebreaker order
    through as race_rank/day_rank/standings ranks, so the brain cited a phantom
    'rank 3'. The whole race block must read unranked (nulls) until a clan scores,
    matching _standing_block — and the streak must be present so it can say
    '22 straight weeks' instead of inventing a number."""
    practice = {
        "#RIV": {"name": "R.E.I.C.H", "fame": 0, "period_points": 0},
        "#GRV": {"name": "Graveborn", "fame": 0, "period_points": 0},
        "#US": {"name": "POAP KINGS", "fame": 0, "period_points": 0},
    }
    with patch.object(
        war_status,
        "_live_race",
        return_value=(
            _projection(practice, period_type="training", period_index=1),
            "2026-07-13T10:00:00Z",
        ),
    ):
        snap = war_status.get_war_season_snapshot(conn=engine_conn)
    race = snap["state"]["race"]
    assert race["race_ranked"] is False
    assert race["race_rank"] is None and race["day_rank"] is None
    assert all(s["rank"] is None for s in race["race_standings"])
    assert all(s["rank"] is None for s in race["day_standings"])
    assert snap["race_ranked"] is False
    assert "not yet ranked" in snap["summary"] and "rank 3" not in snap["summary"]
    assert "week_win_streak" in snap  # the streak the brain cites instead of guessing


def test_week_win_streak_counts_consecutive_firsts(engine_conn):
    """The War-week #1 streak is the raw material for the recap's '22 straight
    weeks' claim — count consecutive our_rank==1 from the newest finalized week
    back, ignoring the in-progress (NULL-rank) period."""
    for s in (100, 101):
        engine_conn.execute(
            "INSERT INTO war_seasons (season_id, started_at) VALUES (?, '2026-01-01')",
            (s,),
        )
    rows = [
        (100, 0, 1),
        (100, 1, 1),
        (100, 2, 1),  # season 100: three #1s
        (101, 0, 1),
        (101, 1, 1),  # season 101: two #1s
        (101, 2, None),  # in-progress week — not counted
    ]
    for season, section, rank in rows:
        engine_conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, our_rank) VALUES (?,?,?)",
            (season, section, rank),
        )
    streak = war_status.get_week_win_streak(conn=engine_conn)
    assert streak["streak"] == 5
    assert streak["weeks_tracked"] == 5
    assert streak["all_first"] is True


def test_week_win_streak_breaks_on_a_non_first(engine_conn):
    engine_conn.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (200, '2026-01-01')"
    )
    for season, section, rank in [(200, 0, 1), (200, 1, 2), (200, 2, 1), (200, 3, 1)]:
        engine_conn.execute(
            "INSERT INTO war_weeks (season_id, section_index, our_rank) VALUES (?,?,?)",
            (season, section, rank),
        )
    streak = war_status.get_week_win_streak(conn=engine_conn)
    assert streak["streak"] == 2  # only the two most-recent #1s
    assert streak["weeks_tracked"] == 4
    assert streak["all_first"] is False


def test_perfect_attendance_excludes_in_progress_day(engine_conn):
    """QA H10: a member perfect on the finalized battle days must count even if
    they haven't finished the in-progress day's decks yet."""
    import storage.war_analytics as wa

    engine_conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, first_seen_at, last_seen_at, is_home) VALUES ('#J2RGCRVG','2026-07-01','2026-07-11',1)"
    )
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, display_name, first_seen_at, last_seen_at) VALUES ('#PERF','Perf','Perf','2026-07-01','2026-07-11')"
    )
    # Perfect attendance is a CURRENT-roster view, so the member has to be in
    # the clan. The old query had no active filter (the season-close grant and
    # the award race both did); unifying on engine.awards.perfect_attendance
    # applies it here too, which is the fix — a departed member should never
    # appear in "who has perfect attendance".
    engine_conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
        "VALUES ('#PERF','#J2RGCRVG','2026-07-01','roster')"
    )
    engine_conn.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (555, '2026-07-01')"
    )
    for day, used in (
        (0, 4),
        (1, 4),
        (2, 1),
    ):  # perfect days 0-1; day 2 in progress (1/4)
        engine_conn.execute(
            "INSERT INTO war_attendance_days (season_id, section_index, war_day_index, player_tag, decks_used, decks_available, observed_at) "
            "VALUES (555, 0, ?, '#PERF', ?, 4, '2026-07-11T00:00:00Z')",
            (day, used),
        )
    engine_conn.commit()
    # live war = Battle Day 3 (war_day_index 2) of section 0 → that day is excluded
    proj = {
        "season_id": 555,
        "section_index": 0,
        "period_index": 5,
        "period_type": "warDay",
        "our_tag": "#US",
        "our_fame": 0,
        "clans": {"#US": {"name": "POAP KINGS", "fame": 0, "period_points": 0}},
        "participants": {},
    }
    with patch.object(war_status, "_live_race", return_value=(proj, "2026-07-11T07:06:00Z")):
        perfect = wa.get_perfect_war_participants(season_id=555, conn=engine_conn)
    names = {m["name"] for m in perfect}
    assert "Perf" in names  # counted on the 2 finalized days despite the unfinished day 3
    perf = next(m for m in perfect if m["name"] == "Perf")
    assert perf["battle_days"] == 2  # only finalized days counted


def test_war_season_summary_counts_in_progress_fame(engine_conn):
    """QA H7/H8: war_weeks.our_fame is NULL until a week finalizes, so a live
    season summed to 0 clan fame while members had thousands. The in-progress
    week's live boat fame must be folded in."""
    engine_conn.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (999, '2026-07-01')"
    )
    engine_conn.execute(
        "INSERT INTO war_weeks (season_id, section_index) VALUES (999, 0)"
    )  # our_fame NULL
    engine_conn.commit()
    proj = {
        "season_id": 999,
        "section_index": 0,
        "period_index": 4,
        "period_type": "warDay",
        "our_tag": "#US",
        "our_fame": 5000,
        "clans": {"#US": {"name": "POAP KINGS", "fame": 5000, "period_points": 0}},
        "participants": {},
    }
    with patch.object(war_status, "_live_race", return_value=(proj, "2026-07-11T07:06:00Z")):
        summ = war_status.get_war_season_summary(season_id=999, conn=engine_conn)
    assert summ["total_clan_fame"] == 5000  # not 0
    assert summ["current_week_in_progress"] is True
    assert summ["races"] == 1  # the NULL-fame week isn't double-counted


def test_colosseum_is_period_points_only():
    # Colosseum: no weekly fame/boat and no mid-week finish line. The standings
    # continue through all four battle days even after passing 5,000 points.
    colo = {
        "#US": {"name": "POAP KINGS", "fame": 0, "period_points": 5200},
        "#RIV": {"name": "R.E.I.C.H", "fame": 0, "period_points": 3100},
    }
    war = _status(colo, period_type="colosseum")
    assert war["colosseum_week"] is True
    assert war["primary_metric"] == "period_points"
    assert war["finish_line"] is None
    assert war["race_completed"] is False
    sb = _standing_block(war)
    assert sb["primary_metric"] == "period_points"
    assert sb["weekly"] is None  # no weekly fame in Colosseum
    assert sb["today"]["period_points"] == 5200
