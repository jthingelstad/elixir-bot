"""Colosseum resolution + the guards that stop boat talk in a Colosseum week.

Regression for awareness loop L372 (2026-07-27): the read reported
`is_colosseum_week: false` on Colosseum practice day 1, framed the week as a normal
fame race (`primary_metric: "fame"`, `finish_line: 10000`, defense fields present),
and Elixir advised adding boat defenses to a parked boat.
"""

from datetime import date

from engine.game_check import _check_colosseum
from storage.war_status import resolve_colosseum_week


class TestResolveColosseumWeek:
    def test_observed_period_type_wins(self):
        assert resolve_colosseum_week("colosseum") == (True, "observed")

    def test_trophy_stakes_of_100_confirm_it(self):
        got, source = resolve_colosseum_week("warDay", trophy_change=100, trophy_stakes_known=True)
        assert (got, source) == (True, "trophy_stakes")

    def test_derived_from_calendar_on_a_practice_day(self):
        """THE regression. 2026-07-27 is section 3 of a 4-week season, so it is the
        final week — even though the API still says "training"."""
        got, source = resolve_colosseum_week("training", section_index=3, on=date(2026, 7, 27))
        assert (got, source) == (True, "derived")

    def test_normal_week_practice_day_is_not_colosseum(self):
        """Same period_type, same phase — but section 1 of a 4-week season."""
        got, source = resolve_colosseum_week("training", section_index=1, on=date(2026, 7, 13))
        assert (got, source) == (False, None)

    def test_five_week_season_does_not_call_section_3_the_finale(self):
        """Season 133 ran 5 weeks, so section 3 was a normal week and section 4 was
        the Colosseum. A hardcoded 'section 3' rule would have been wrong here."""
        assert resolve_colosseum_week("training", section_index=3, on=date(2026, 6, 22))[0] is False
        assert resolve_colosseum_week("training", section_index=4, on=date(2026, 6, 29))[0] is True

    def test_missing_section_index_does_not_guess(self):
        assert resolve_colosseum_week("training") == (False, None)


class TestColosseumBoatGuard:
    """`engine.game_check` runs before an awareness post is sent; a finding forces a
    repair and, failing that, fails the tick. Boat claims were not covered."""

    def _findings(self, copy, colosseum=True):
        out: list = []
        _check_colosseum(copy, {"is_colosseum_week": colosseum}, out)
        return out

    def test_blocks_the_actual_bad_advice(self):
        out = self._findings("Colosseum week — top up our boat defenses before battle days.")
        assert any("no boat" in f["issue"] for f in out)
        assert all(f["severity"] == "error" for f in out)

    def test_blocks_boat_battles_and_defense_counts(self):
        assert self._findings("Boat battle attacks are worth it this week.")
        assert self._findings("We have 12 defenses remaining on the boat.")

    def test_allows_correctly_negated_copy(self):
        assert not self._findings("Colosseum week: no boat defenses this week — decks only.")

    def test_silent_in_a_normal_week(self):
        assert not self._findings("Set your boat defenses today.", colosseum=False)
