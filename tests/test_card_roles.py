"""Derived card/deck roles + per-battle structural tags (Battle Intelligence v2)."""

from engine.card_roles import (
    air_matchup,
    decisive_factor,
    deck_facts,
    deck_role_coverage,
    elixir_band_note,
    is_air_answer,
    is_air_troop,
    is_heavy_air_answer,
    is_splash_answer,
    is_tank_answer,
    level_validity,
    min_air_answers,
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


# ── deck-formula coverage (the half the player sees) ─────────────────────────

_FURNACE = {
    "name": "Furnace",
    "targets": "air_and_ground",
    "spell_tier": "none",
    "dps_tier": "low",
    "attack_style": "splash_small",
    "role": "spawner",
    "elixir_cost": 4,
}
_ARROWS = dict(ARROWS, name="Arrows", elixir_cost=3)
_SNOWBALL = {
    "name": "Giant Snowball",
    "targets": "none",
    "spell_tier": "small",
    "splash_hits_air": True,
    "elixir_cost": 2,
}
_MUSKETEER = dict(MUSKETEER, name="Musketeer", elixir_cost=4)


def test_a_spell_is_an_air_answer_but_not_an_air_troop():
    """The conflation that shipped a bad deck: Arrows and a Musketeer both satisfy
    'has an air answer', and only one of them survives contact with a Balloon."""
    assert is_air_answer(_ARROWS) is True
    assert is_air_troop(_ARROWS) is False
    assert is_air_troop(_MUSKETEER) is True
    assert is_heavy_air_answer(_MUSKETEER) is True
    assert is_heavy_air_answer(_FURNACE) is False, "low dps is not an answer to an air tank"


def test_spell_only_air_coverage_is_called_out():
    """The real deck a member was handed: three 'air answers', none of them a unit."""
    deck = [_ARROWS, _SNOWBALL] + [dict(KNIGHT, name=f"Ground{i}", elixir_cost=3) for i in range(6)]
    cov = deck_role_coverage(deck, family="control", avg_elixir=3.4)
    assert cov["air_answers"]["count"] == 2
    assert cov["air_answers"]["troops"] == []
    assert any("spells only" in g for g in cov["gaps"])


def test_air_answers_present_but_none_heavy_is_its_own_warning():
    deck = [_FURNACE, _ARROWS] + [dict(KNIGHT, name=f"G{i}", elixir_cost=3) for i in range(6)]
    cov = deck_role_coverage(deck, family="control", avg_elixir=3.4)
    assert cov["air_answers"]["troops"] == ["Furnace"]
    assert any("no heavy air answer" in g for g in cov["gaps"])
    assert not any("spells only" in g for g in cov["gaps"])


def test_the_air_floor_exempts_genuinely_cheap_cycle_decks():
    """Guides ask for 2-3 air answers and exempt sub-2.8 cycle decks, which defend by
    rotating back rather than by holding. A flat floor punishes a legitimate archetype."""
    assert min_air_answers(2.6) == 1
    assert min_air_answers(3.4) == 2
    assert min_air_answers(None) == 2, "unknown cost must not buy the exemption"


def test_elixir_band_flags_a_deck_too_heavy_for_its_own_archetype():
    """A 7-elixir 'beatdown' is not a beatdown, it is a deck that cannot cycle. The
    archetype label alone never says so."""
    assert elixir_band_note("beatdown", 4.2) is None
    assert "heavy" in elixir_band_note("beatdown", 7.25)
    assert "cheap" in elixir_band_note("beatdown", 2.9)
    assert elixir_band_note("unknown family", 9.0) is None, "never guess a band we lack"
    assert elixir_band_note("cycle", None) is None


def test_missing_enrichment_produces_no_critique_at_all():
    """Absence of facts is not absence of roles. Eight unenriched cards must not read
    as 'no win condition, no air answer, no spell'."""
    cov = deck_role_coverage([None] * 8)
    assert cov["gaps"] == []
    assert cov["unknown"] is True


def test_partial_enrichment_never_asserts_an_absence():
    """Seven cards known, one unknown — and the unknown one may be the win condition."""
    cov = deck_role_coverage([_MUSKETEER] * 7, family="control", avg_elixir=3.4)
    assert cov["facts_complete"] is False
    assert cov["gaps"] == []
    assert cov["unknown_cards"] == 1


def test_a_card_is_listed_under_every_role_it_fills():
    """Roles overlap and the report must show all of them. A Musketeer is the deck's
    air answer AND its heavy air answer AND its tank answer at once; showing her under
    one heading hides two of the three reasons she is in the list."""
    cov = deck_role_coverage([_MUSKETEER] * 8, family="control", avg_elixir=4.0)
    assert "Musketeer" in cov["air_answers"]["troops"]
    assert "Musketeer" in cov["air_answers"]["heavy"]
    assert "Musketeer" in cov["tank_answers"]


def test_splash_is_not_a_tank_answer():
    """Splash chews swarms and bounces off a Golem. The guides are explicit that an
    improvised answer is not an answer, and Valkyrie must not fill the tank slot."""
    valk = {
        "name": "Valkyrie",
        "targets": "ground",
        "attack_style": "splash_small",
        "dps_tier": "high",
        "role": "mini_tank",
        "elixir_cost": 4,
    }
    cov = deck_role_coverage([valk] * 8, family="control", avg_elixir=4.0)
    assert "Valkyrie" in cov["splash_answers"]
    assert cov["tank_answers"] == []
    assert any("no real tank answer" in g for g in cov["gaps"])


def test_a_structurally_complete_deck_reports_no_gaps():
    """The honest empty list — never a manufactured critique to look thorough."""
    deck = [
        dict(_MUSKETEER, name="Musketeer"),
        dict(WIZARD, name="Wizard", elixir_cost=5),
        dict(MUSKETEER, name="Mini P.E.K.K.A", elixir_cost=4),
        dict(WIZARD, name="Valkyrie", elixir_cost=4),
        dict(_ARROWS, name="Arrows", spell_tier="small"),
        {"name": "Fireball", "targets": "none", "spell_tier": "big", "elixir_cost": 4},
        {"name": "Hog Rider", "targets": "buildings_only", "is_win_condition": 1, "elixir_cost": 4},
        {"name": "Skeletons", "targets": "ground", "unit_count": "many", "elixir_cost": 1},
    ]
    cov = deck_role_coverage(deck, family="control", avg_elixir=3.5)
    assert cov["gaps"] == [], cov["gaps"]
    assert cov["win_conditions"] == ["Hog Rider"]
    assert "Skeletons" in cov["cycle_cards"]
