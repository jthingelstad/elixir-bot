"""Shared war intelligence contract tests."""

from capabilities.war import get_war_intelligence, get_war_season_view


class _WarSource:
    def get_current_war_status(self):
        return {
            "observed_at": "2026-07-15T02:00:00Z",
            "season_id": 134,
            "section_index": 2,
            "week": 3,
            "phase": "battle",
            "phase_display": "Battle Day 2",
            "primary_metric": "fame",
            "boat_scored": False,
            "race_rank": 3,
            "race_standings": [
                {"rank": 1, "clan_name": "POAP KINGS", "fame": 0, "is_us": True}
            ],
            "day_scored": True,
            "day_rank": 1,
            "day_standings": [
                {"rank": 1, "clan_name": "POAP KINGS", "period_points": 1400, "is_us": True}
            ],
            "fame": 0,
            "period_points": 1400,
            "finish_line": 10000,
            "projected_day_fame": 3000,
        }

    def build_war_now_context(self):
        return {"phase_display": "Battle Day 2", "race_standings": [], "day_standings": []}

    def get_current_war_day_state(self):
        return {
            "war_day_key": "134:2:1",
            "observed_at": "2026-07-15T02:00:00Z",
            "total_participants": 3,
            "finished_count": 1,
            "engaged_count": 2,
            "untouched_count": 1,
            "used_all_4": [{"tag": "#A", "name": "Alpha", "decks_used_today": 4}],
            "used_some": [{"tag": "#B", "name": "Bravo", "decks_used_today": 2}],
            "used_none": [{"tag": "#C", "name": "Charlie", "decks_used_today": 0}],
        }


def test_war_contract_keeps_weekly_and_daily_races_distinct():
    result = get_war_intelligence(source=_WarSource())

    assert result["capability"] == "war_intelligence"
    assert result["contract_version"] == 1
    assert result["weekly_race"]["metric"] == "fame"
    assert result["weekly_race"]["rank"] is None
    assert result["weekly_race"]["standings"][0]["rank"] is None
    assert result["current_state"]["race_rank"] is None
    assert result["current_state"]["race_standings"][0]["rank"] is None
    assert result["daily_race"]["metric"] == "period_points"
    assert result["daily_race"]["rank"] == 1
    assert result["engagement"]["remaining_decks"]["total"] == 2
    assert result["engagement"]["remaining_decks"]["partial"] == 1


def test_war_season_view_wraps_the_shared_contract():
    class Source:
        def get_war_season_summary(self, top_n):
            return {"season_id": 134, "top_n": top_n}

        def get_connection(self):
            raise RuntimeError("current-week detail unavailable")

    result = get_war_season_view(view="summary", limit=5, source=Source())

    assert result["capability"] == "war_intelligence"
    assert result["view"] == "summary"
    assert result["data"] == {
        "season_id": 134,
        "top_n": 5,
        "current_week_top": [],
    }
