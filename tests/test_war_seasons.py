"""War-season shape + the two-axis river race model.

Guards the 2026-07-27 miss: Elixir advised adding boat defenses during a Colosseum
week, because the API reports `periodType: "training"` on that week's practice days
and nothing else knew the season's final week had begun.
"""

from datetime import date

import pytest

from engine.game_rules import river_race_mechanics
from engine.war_seasons import (
    final_section_index,
    is_final_section,
    season_bounds,
    season_shape,
    section_index_for,
    total_sections,
)

# (season_id, season start, observed colosseum section_index) — ground truth from
# poapkings.com/data/clash-royale.sqlite `river_race_weeks`, which carries the
# authoritative `is_colosseum` flag and ±100 `trophy_change` for every closed week.
OBSERVED_SEASONS = [
    (129, date(2026, 2, 2), 4, 3),
    (130, date(2026, 3, 2), 5, 4),
    (131, date(2026, 4, 6), 4, 3),
    (132, date(2026, 5, 4), 4, 3),
    (133, date(2026, 6, 1), 5, 4),
]


@pytest.mark.parametrize("season_id,start,weeks,colosseum_section", OBSERVED_SEASONS)
def test_calendar_predicts_every_recorded_season(season_id, start, weeks, colosseum_section):
    """A war season runs first-Monday to first-Monday, so its length (4 or 5) and
    therefore which section is Colosseum are both deterministic."""
    assert total_sections(start) == weeks, f"season {season_id} length"
    assert final_section_index(start) == colosseum_section, f"season {season_id} colosseum"
    assert is_final_section(start, colosseum_section) is True
    assert is_final_section(start, colosseum_section - 1) is False


def test_season_spanning_a_month_boundary_belongs_to_the_earlier_season():
    """Season 133's Colosseum week ran 2026-06-29 -> 07-06, so 07-02 is season 133
    (started 06-01), not July's season."""
    start, nxt = season_bounds(date(2026, 7, 2))
    assert (start, nxt) == (date(2026, 6, 1), date(2026, 7, 6))
    assert section_index_for(date(2026, 7, 2)) == 4
    assert is_final_section(date(2026, 7, 2)) is True


def test_todays_shape_names_the_finale():
    """2026-07-27 is section 3 of a 4-week season -> week 4 of 4, the Colosseum."""
    shape = season_shape(date(2026, 7, 27), 3)
    assert shape["week"] == 4
    assert shape["total_weeks"] == 4
    assert shape["weeks_remaining"] == 0
    assert shape["is_final_week"] is True


class TestTwoAxisMechanics:
    """Week type (normal vs colosseum) and day type (practice vs battle) are
    INDEPENDENT. Conflating them is what produced the bad post."""

    def test_normal_practice_is_the_defense_build_window(self):
        m = river_race_mechanics("training", "practice")
        assert m["boat_in_play"] is True
        assert m["boat_defenses_exist"] is True
        assert m["defenses_can_be_added"] is True
        assert m["defenses_earn_fame_today"] is False
        # The API only reports defenses for CLOSED days, so no live count exists
        # while they are being built — advise, never quote a number.
        assert m["defense_count_available"] is False
        assert m["score_metric"] == "fame"
        assert m["finish_line"] == 10_000

    def test_normal_battle_days_earn_defense_fame_but_cannot_add(self):
        m = river_race_mechanics("warDay", "battle")
        assert m["boat_in_play"] is True
        assert m["defenses_can_be_added"] is False
        assert m["defenses_earn_fame_today"] is True
        assert m["score_metric"] == "fame"

    def test_colosseum_practice_has_no_boat_at_all(self):
        """The regression: Colosseum practice days. The boat is parked, so there
        is nothing to add, defend or repair."""
        m = river_race_mechanics("colosseum", "practice")
        assert m["boat_in_play"] is False
        assert m["boat_defenses_exist"] is False
        assert m["boat_battles_exist"] is False
        assert m["defenses_can_be_added"] is False
        assert m["score_metric"] == "points"
        assert m["finish_line"] is None
        assert "no boat" in m["boat_guidance"].lower()

    def test_colosseum_battle_days_are_points_with_no_finish_line(self):
        m = river_race_mechanics("colosseum", "battle")
        assert m["score_metric"] == "points"
        assert m["finish_line"] is None
        assert m["boat_defenses_exist"] is False
        assert m["every_battle_counts_for_standings"] is True

    def test_phase_is_optional_and_never_guessed(self):
        m = river_race_mechanics("training")
        assert m["defenses_can_be_added"] is None
        assert m["defenses_earn_fame_today"] is None
