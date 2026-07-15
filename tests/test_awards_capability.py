"""Shared awards and recognition contract tests."""

from unittest.mock import patch

from capabilities.awards import award_period, get_awards_recognition


class _AwardsSource:
    def get_season_awards_standings(self, season_id):
        return {"season_id": season_id or 134, "war_champ": [{"tag": "#A", "rank": 1}]}

    def get_award_races(self, season_id, war_champ_limit, rookie_limit):
        return {
            "season_id": season_id,
            "limits": [war_champ_limit, rookie_limit],
            "war_champ": [{"tag": "#A"}],
        }

    def list_awards(self, **kwargs):
        return [{"award_type": "war_champ", "season_id": 133, "player_tag": "#A"}]

    def award_leaderboard(self, **kwargs):
        return [{"player_tag": "#A", "count": 2, "query": kwargs}]


def test_award_period_distinguishes_war_seasons_from_ranked_months():
    assert award_period(134, "war_champ") == {"id": 134, "kind": "war_season"}
    assert award_period(202607, "pol_champ") == {
        "id": 202607,
        "kind": "path_of_legends_month",
    }


def test_live_award_standings_are_explicitly_provisional():
    with patch(
        "capabilities.awards.get_war_season_view",
        return_value={"data": {"freshness": {"as_of": "2026-07-15T02:00:00Z"}}},
    ):
        result = get_awards_recognition(
            view="current_standings", season_id=134, source=_AwardsSource()
        )

    assert result["capability"] == "awards_recognition"
    assert result["contract_version"] == 1
    assert result["state"] == "provisional"
    assert result["data"]["provisional"] is True
    assert result["data"]["freshness"]["as_of"] == "2026-07-15T02:00:00Z"


def test_live_award_standings_report_the_resolved_current_period():
    with patch(
        "capabilities.awards.get_war_season_view",
        return_value={"data": {"freshness": {"as_of": "2026-07-15T02:00:00Z"}}},
    ):
        result = get_awards_recognition(
            view="current_standings", source=_AwardsSource()
        )

    assert result["period"] == {"id": 134, "kind": "war_season"}


def test_durable_award_list_carries_period_identity():
    result = get_awards_recognition(
        view="list",
        award_type="war_champ",
        season_id=133,
        limit=10,
        source=_AwardsSource(),
    )

    assert result["state"] == "durable"
    assert result["period"] == {"id": 133, "kind": "war_season"}
    assert result["data"]["count"] == 1
    assert result["data"]["truncated"] is False
