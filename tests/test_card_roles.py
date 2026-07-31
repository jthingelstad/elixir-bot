"""Derived card/deck roles + per-battle structural tags (Battle Intelligence v2)."""

from engine.card_roles import (
    air_matchup,
    decisive_factor,
    deck_facts,
    is_air_answer,
    is_splash_answer,
    is_tank_answer,
    level_validity,
    spell_bait_exposed,
    wincon_pressure,
)

MUSKETEER = {"targets": "air_and_ground", "dps_tier": "high", "attack_style": "single"}
KNIGHT = {"targets": "ground", "dps_tier": "medium", "attack_style": "single"}
WIZARD = {"targets": "air_and_ground", "dps_tier": "medium", "attack_style": "splash_small"}
ARROWS = {"targets": "none", "spell_tier": "small", "splash_hits_air": True}
ROCKET = {"targets": "none", "spell_tier": "big", "splash_hits_air": True}
SKARMY = {
    "targets": "ground",
    "unit_count": "many",
    "fragile_to_small_spell": True,
    "attack_style": "single",
    "dps_tier": "high",
}


def test_air_answer_needs_air_targeting():
    assert is_air_answer(MUSKETEER) is True
    assert is_air_answer(KNIGHT) is False
    assert is_air_answer(ARROWS) is True  # a spell that hits air counts


def test_tank_answer_is_high_dps_single_target():
    assert is_tank_answer(MUSKETEER) is True
    assert is_tank_answer(WIZARD) is False  # splash chews swarms, not tanks
    assert is_tank_answer(KNIGHT) is False


def test_splash_answer():
    assert is_splash_answer(WIZARD) is True
    assert is_splash_answer(MUSKETEER) is False


def test_deck_facts_counts_the_formula_slots():
    d = deck_facts([MUSKETEER, KNIGHT, WIZARD, ARROWS, ROCKET, SKARMY, KNIGHT, KNIGHT])
    assert d["air_answer_count"] == 3  # Musketeer, Wizard, Arrows
    assert d["splash_answer_count"] == 1
    assert d["has_small_spell"] == 1 and d["has_big_spell"] == 1
    assert d["bait_unit_count"] == 1
    assert d["facts_complete"] == 1


def test_deck_facts_flags_incomplete():
    assert deck_facts([MUSKETEER, KNIGHT])["facts_complete"] == 0


def test_level_validity_catches_normalized_modes():
    # The Feature-3 bug: prose cited "4 levels down" on Showdown, where levels are capped.
    assert level_validity("Showdown_Friendly", 0) == "normalized"
    assert level_validity("Ladder", 1) == "normalized"  # ranked caps at 11
    assert level_validity("Ladder", 0) == "real"
    assert level_validity("CW_Battle_1v1", 0) == "real"


def test_air_matchup_flags_an_air_hole():
    no_air = {"air_answer_count": 0}
    flying = {"air_threat_count": 2}
    assert air_matchup(no_air, flying) == "stressed"
    assert air_matchup({"air_answer_count": 5}, flying) == "favored"
    assert air_matchup({"air_answer_count": 4}, flying) == "even"  # the common case
    assert air_matchup(no_air, {"air_threat_count": 0}) == "untested"


def test_wincon_pressure():
    ours = {"win_condition_count": 1}
    # Calibrated to the real spread (2-8, median 5): 5 is typical, not "countered".
    assert wincon_pressure(ours, {"tank_answer_count": 3, "splash_answer_count": 2}) == "contested"
    assert wincon_pressure(ours, {"tank_answer_count": 4, "splash_answer_count": 3}) == "countered"
    assert wincon_pressure(ours, {"tank_answer_count": 1, "splash_answer_count": 1}) == "clear"
    assert wincon_pressure({"win_condition_count": 0}, {}) == "no_wincon"


def test_spell_bait_exposed():
    baity = {"bait_unit_count": 3}
    assert spell_bait_exposed(baity, {"has_small_spell": 1}) == 1
    assert spell_bait_exposed(baity, {"has_small_spell": 0}) == 0


def test_decisive_factor_ignores_unpredictive_structure():
    """air/wincon must NEVER drive the read: measured against 12,687 clan battles
    both are flat vs outcome, so citing them would narrate noise as a cause."""
    for air, wincon in (("stressed", "countered"), ("favored", "clear")):
        assert (
            decisive_factor(
                level_gap=0.1,
                level_ok=True,
                closeness=1,
                discipline_delta=0.0,
                performance=0,
                air=air,
                wincon=wincon,
            )
            == "even_game"
        )


def test_decisive_factor_ranks_levels_over_elixir():
    """Card levels outrank elixir: levels are monotonic vs outcome AND actionable,
    while elixir leak is partly an effect of already losing."""
    assert (
        decisive_factor(
            level_gap=2.5,
            level_ok=True,
            closeness=1,
            discipline_delta=9.0,
            performance=-1,
            air="stressed",
            wincon="clear",
        )
        == "card_levels"
    )
    # elixir still wins when levels are even
    assert (
        decisive_factor(
            level_gap=0.1,
            level_ok=True,
            closeness=1,
            discipline_delta=9.0,
            performance=0,
            air="even",
            wincon="clear",
        )
        == "elixir_management"
    )


def test_decisive_factor_ignores_levels_when_normalized():
    """A big level_gap must NOT drive the read in a level-capped mode."""
    assert (
        decisive_factor(
            level_gap=-4.0,
            level_ok=False,
            closeness=1,
            discipline_delta=0.1,
            performance=0,
            air="even",
            wincon="clear",
        )
        != "card_levels"
    )
    assert (
        decisive_factor(
            level_gap=-4.0,
            level_ok=True,
            closeness=1,
            discipline_delta=0.1,
            performance=0,
            air="even",
            wincon="clear",
        )
        == "card_levels"
    )
