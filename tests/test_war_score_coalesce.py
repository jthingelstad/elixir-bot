"""The River Race API moved the accumulating war score into ``periodPoints``;
``fame`` reads 0 all week in the current format (verified 2026-07-10: our clan
fame=0 / periodPoints=8050 mid-war, and we were leading). The war status must
coalesce the two so the brain sees the real score, not 0."""

from unittest.mock import patch

from storage import war_status
from storage.war_status import _race_score
from runtime.awareness.read import _standing_block


def test_race_score_prefers_period_points_when_fame_is_zero():
    score, source, label = _race_score({"fame": 0, "period_points": 8050})
    assert (score, source) == (8050, "period_points")
    # Legacy weeks that carry it in fame still read from fame.
    assert _race_score({"fame": 42600, "period_points": 0}) == (42600, "fame", "fame")
    # Never let a stale daily period subset lower a cumulative fame.
    assert _race_score({"fame": 6000, "period_points": 2000})[0] == 6000


def _projection():
    # A live warDay projection like the real payload: every fame=0, the score in
    # periodPoints, us leading.
    return {
        "season_id": 134, "section_index": 0, "period_index": 3,
        "period_type": "warDay", "our_tag": "#US", "our_fame": 0,
        "clans": {
            "#US": {"name": "POAP KINGS", "fame": 0, "period_points": 8050},
            "#RIV": {"name": "R.E.I.C.H", "fame": 0, "period_points": 450},
            "#OTH": {"name": "euromix", "fame": 0, "period_points": 200},
        },
        "participants": {},
    }


def test_war_status_and_standing_surface_the_real_score():
    with patch.object(war_status, "_live_race",
                      return_value=(_projection(), "2026-07-10T02:19:00Z")):
        war = war_status.get_current_war_status(conn=None)

    assert war["fame"] == 8050          # the real score, not 0
    assert war["raw_fame"] == 0         # original field preserved
    assert war["period_points"] == 8050
    assert war["score_source"] == "period_points"
    assert war["race_rank"] == 1

    sb = _standing_block(war)
    assert sb["fame"] == 8050
    assert sb["rank"] == 1
    assert sb["deficit_to_leader"] == 0  # we ARE the leader
    assert sb["scoreboard"][0] == {"name": "POAP KINGS", "fame": 8050, "rank": 1}
    assert sb["scoreboard"][1]["fame"] == 450
