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

from storage import war_status
from runtime.awareness.read import _standing_block


def _projection(clans, *, period_type="warDay", period_index=4, our_tag="#US"):
    us = clans.get(our_tag, {})
    return {
        "season_id": 134, "section_index": 0, "period_index": period_index,
        "period_type": period_type, "our_tag": our_tag,
        "our_fame": us.get("fame", 0),
        "clans": clans, "participants": {},
    }


# The loop #44 moment: mid Battle Day 2, boat scored on Day 1, today's race live.
LOOP44 = {
    "#US": {"name": "POAP KINGS", "fame": 3435, "period_points": 10525},
    "#RIV": {"name": "R.E.I.C.H", "fame": 1800, "period_points": 450},
    "#OTH": {"name": "euromix", "fame": 1053, "period_points": 350},
}


def _status(clans, **kw):
    with patch.object(war_status, "_live_race",
                      return_value=(_projection(clans, **kw), "2026-07-11T07:06:00Z")):
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


def test_standings_are_single_field_and_never_mixed():
    war = _status(LOOP44)
    # race_standings ordered by fame; day_standings by period points.
    assert [s["clan_name"] for s in war["race_standings"]] == ["POAP KINGS", "R.E.I.C.H", "euromix"]
    assert [s["clan_name"] for s in war["day_standings"]] == ["POAP KINGS", "R.E.I.C.H", "euromix"]
    # Every entry carries BOTH raw numbers so no caller has to re-mix.
    for s in war["race_standings"]:
        assert "fame" in s and "period_points" in s

    sb = _standing_block(war)
    # Weekly scoreboard is fame-only; today scoreboard is period-points-only.
    assert sb["weekly"]["fame"] == 3435
    assert all(set(e) == {"name", "fame", "rank"} for e in sb["weekly"]["scoreboard"])
    assert sb["weekly"]["scoreboard"][1] == {"name": "R.E.I.C.H", "fame": 1800, "rank": 2}
    assert sb["today"]["period_points"] == 10525
    assert all(set(e) == {"name", "period_points", "rank"} for e in sb["today"]["scoreboard"])
    assert sb["today"]["scoreboard"][1] == {"name": "R.E.I.C.H", "period_points": 450, "rank": 2}
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
    assert sb["weekly"]["boat_scored"] is False   # weekly present but flagged unscored
    assert sb["today"]["period_points"] == 3000


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
    assert sb["today"] is None          # daily race suppressed until it scores
    assert sb["weekly"]["fame"] == 6870


def test_war_season_snapshot_exposes_live_race(engine_conn):
    """get_war_season_snapshot (feeds get_elixir_state) must map the real
    get_current_war_status keys — regression for QA H13/H14 where it read stale
    'standings'/'our_fame'/'day_number' and returned an empty race block."""
    with patch.object(war_status, "_live_race",
                      return_value=(_projection(LOOP44), "2026-07-11T07:06:00Z")):
        snap = war_status.get_war_season_snapshot(conn=engine_conn)
    race = snap["state"]["race"]
    assert race["fame"] == 3435 and race["race_rank"] == 1
    assert [s["clan_name"] for s in race["race_standings"]] == ["POAP KINGS", "R.E.I.C.H", "euromix"]
    assert race["period_points"] == 10525 and race["day_rank"] == 1
    assert race["primary_metric"] == "fame"
    assert snap["state"]["day_number"] is not None
    # The old buggy keys must be gone.
    assert "standings" not in race and "our_fame" not in race


def test_colosseum_is_period_points_only():
    # Colosseum: no weekly fame/boat — the race is decided by period points, and
    # the finish line is 5,000 period points.
    colo = {
        "#US": {"name": "POAP KINGS", "fame": 0, "period_points": 5200},
        "#RIV": {"name": "R.E.I.C.H", "fame": 0, "period_points": 3100},
    }
    war = _status(colo, period_type="colosseum")
    assert war["colosseum_week"] is True
    assert war["primary_metric"] == "period_points"
    assert war["finish_line"] == 5000
    assert war["race_completed"] is True   # 5,200 period points >= 5,000
    sb = _standing_block(war)
    assert sb["primary_metric"] == "period_points"
    assert sb["weekly"] is None            # no weekly fame in Colosseum
    assert sb["today"]["period_points"] == 5200
