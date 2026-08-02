"""Derived card/deck roles + per-battle structural tags (Battle Intelligence v2)."""

from engine.card_roles import (
    decisive_factor,
    deck_facts,
    deck_role_coverage,
    is_air_answer,
    is_air_defender,
    is_heavy_air_answer,
    is_splash_answer,
    is_tank_answer,
    level_validity,
    matchup_notes,
    min_air_answers,
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


def test_decisive_factor_ranks_levels_over_elixir():
    """Card levels outrank elixir: levels are monotonic vs outcome AND actionable,
    while elixir leak is partly an effect of already losing."""
    assert (
        decisive_factor(
            level_gap=2.5,
            level_ok=True,
            closeness=1,
            discipline_delta=9.0,
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
        )
        != "card_levels"
    )
    assert (
        decisive_factor(
            level_gap=-4.0,
            level_ok=True,
            closeness=1,
            discipline_delta=0.1,
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


def test_a_spell_is_an_air_answer_but_not_an_air_defender():
    """The conflation that shipped a bad deck: Arrows and a Musketeer both satisfy
    'has an air answer', and only one of them survives contact with a Balloon."""
    assert is_air_answer(_ARROWS) is True
    assert is_air_defender(_ARROWS) is False
    assert is_air_defender(_MUSKETEER) is True
    assert is_heavy_air_answer(_MUSKETEER) is True
    assert is_heavy_air_answer(_FURNACE) is False, "low dps is not an answer to an air tank"


def test_spell_only_air_coverage_is_called_out():
    """The real deck a member was handed: three 'air answers', none of them a unit."""
    deck = [_ARROWS, _SNOWBALL] + [dict(KNIGHT, name=f"Ground{i}", elixir_cost=3) for i in range(6)]
    cov = deck_role_coverage(deck, family="control", avg_elixir=3.4)
    assert cov["air_answers"]["count"] == 2
    assert cov["air_answers"]["defenders"] == []
    assert any("spells only" in g for g in cov["gaps"])


def test_air_answers_present_but_none_heavy_is_its_own_warning():
    deck = [_FURNACE, _ARROWS] + [dict(KNIGHT, name=f"G{i}", elixir_cost=3) for i in range(6)]
    cov = deck_role_coverage(deck, family="control", avg_elixir=3.4)
    assert cov["air_answers"]["defenders"] == ["Furnace"]
    assert any("no heavy air answer" in g for g in cov["gaps"])
    assert not any("spells only" in g for g in cov["gaps"])


def test_the_air_floor_exempts_genuinely_cheap_cycle_decks():
    """Guides ask for 2-3 air answers and exempt sub-2.8 cycle decks, which defend by
    rotating back rather than by holding. A flat floor punishes a legitimate archetype."""
    assert min_air_answers(2.6) == 1
    assert min_air_answers(3.4) == 2
    assert min_air_answers(None) == 2, "unknown cost must not buy the exemption"


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
    assert "Musketeer" in cov["air_answers"]["defenders"]
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


# ── matchup notes: the mechanism behind a tendency, never a cause ────────────


def _covered(**over):
    base = {
        "facts_complete": True,
        "win_conditions": ["Hog Rider"],
        "air_answers": {"defenders": ["Musketeer"], "spells": ["Arrows"], "heavy": ["Musketeer"]},
        "tank_answers": ["Mini P.E.K.K.A"],
        "splash_answers": ["Valkyrie"],
        "small_spells": ["The Log", "Zap"],
        "big_spells": ["Fireball"],
    }
    base.update(over)
    return base


def test_one_small_spell_against_bait_is_named():
    """The cleanest quantifiable line in the game: a bait deck runs more fragile
    cards than one small spell can answer."""
    notes = matchup_notes(_covered(small_spells=["The Log"]), "bait")
    assert any("one small spell" in n for n in notes)
    assert matchup_notes(_covered(), "bait") == [], "two small spells closes the exploit"


def test_no_tank_answer_against_beatdown_is_named():
    notes = matchup_notes(_covered(tank_answers=[]), "beatdown")
    assert any("melts a tank" in n for n in notes)


def test_a_structurally_sound_deck_gets_no_notes_for_any_archetype():
    """Empty is the common and correct answer. Inventing a weakness to look useful
    is the failure this whole layer exists to prevent."""
    for fam in ("beatdown", "bridge spam", "control", "cycle", "bait", "siege"):
        assert matchup_notes(_covered(), fam) == [], fam


def test_notes_need_complete_facts_and_a_named_opponent():
    assert matchup_notes(_covered(facts_complete=False, small_spells=[]), "bait") == []
    assert matchup_notes(_covered(small_spells=[]), None) == []
    assert matchup_notes({}, "bait") == []


def test_small_spells_are_counted_not_just_flagged():
    """has_small_spell is a boolean and cannot express one-versus-two, which is the
    whole distinction that decides the bait matchup."""
    log = {"name": "The Log", "spell_tier": "small", "targets": "none", "elixir_cost": 2}
    zap = {"name": "Zap", "spell_tier": "small", "targets": "none", "elixir_cost": 2}
    one = deck_facts([log] + [KNIGHT] * 7)
    two = deck_facts([log, zap] + [KNIGHT] * 6)
    assert one["has_small_spell"] == two["has_small_spell"] == 1
    assert one["small_spell_count"] == 1
    assert two["small_spell_count"] == 2


# ── regressions from real #ask-elixir answers (2026-08-01) ───────────────────

_ROCKET = {
    "name": "Rocket",
    "spell_tier": "big",
    "targets": "air_and_ground",
    "splash_hits_air": 1,
    "dps_tier": "high",
    "attack_style": "splash_large",
    "elixir_cost": 6,
}
_ZAP = {
    "name": "Zap",
    "spell_tier": "small",
    "targets": "air_and_ground",
    "splash_hits_air": 1,
    "dps_tier": "low",
    "attack_style": "splash_small",
    "elixir_cost": 2,
}
_DART = {
    "name": "Dart Goblin",
    "targets": "air_and_ground",
    "hp_tier": "low",
    "unit_count": "one",
    "dps_tier": "high",
    "attack_style": "single",
    "spell_tier": "none",
    "role": "support",
    "elixir_cost": 3,
}
_SKARMY = {
    "name": "Skeleton Army",
    "targets": "ground",
    "hp_tier": "low",
    "unit_count": "many",
    "dps_tier": "high",
    "attack_style": "single",
    "spell_tier": "none",
    "role": "swarm",
    "elixir_cost": 3,
}
_GOB_BARREL = {
    "name": "Goblin Barrel",
    "targets": "ground",
    "hp_tier": "low",
    "unit_count": "few",
    "dps_tier": "high",
    "attack_style": "single",
    "spell_tier": "none",
    "role": "win_condition",
    "is_win_condition": 1,
    "elixir_cost": 3,
}
_BALLOON = {
    "name": "Balloon",
    "targets": "buildings_only",
    "hp_tier": "medium",
    "unit_count": "one",
    "dps_tier": "high",
    "attack_style": "single",
    "spell_tier": "none",
    "role": "win_condition",
    "is_win_condition": 1,
    "elixir_cost": 5,
}


def test_a_big_spell_is_never_an_air_answer():
    """The live failure: a member was told his deck had "4 air answers (Witch, Dart
    Goblin, Rocket, Zap)". Nobody defends a Lava Hound with a Rocket.

    The bug was ordering. Every damaging spell is enriched targets='air_and_ground'
    (a Rocket does hit an air unit), so a leading targets check returned True and the
    big-spell exclusion under it was unreachable. It inflated the air count on 10% of
    the deck corpus, and that count also gates the candidate filter."""
    assert is_air_answer(_ROCKET) is False
    assert is_air_answer(_ZAP) is True, "a cheap spell that hits air is still an answer"
    assert is_air_answer(_DART) is True
    assert is_air_defender(_ROCKET) is False


def test_a_card_that_only_hits_buildings_cannot_defend():
    """Balloon, Battle Ram, Ram Rider and Hog Rider walk straight past whatever you
    are trying to stop, however hard they hit."""
    assert is_tank_answer(_BALLOON) is False


def test_a_fragile_card_is_only_a_tank_answer_in_numbers():
    """Skeleton Army melts a Golem precisely because there are fifteen of them; one
    Dart Goblin does not. The member's reported tank answers were "Dart Goblin and
    Goblin Barrel", and the deck was called structurally sound with no gaps."""
    assert is_tank_answer(_SKARMY) is True
    assert is_tank_answer(_DART) is False
    assert is_tank_answer(_GOB_BARREL) is False, "a barrel is thrown at a tower, not a Golem"


def test_the_deck_with_no_real_tank_answer_now_says_so():
    """Same eight cards that came back with 'Gaps: none'."""
    deck = [
        _ROCKET,
        _ZAP,
        _DART,
        _GOB_BARREL,
        _SKARMY.copy(),
        _BALLOON,
        dict(_ZAP, name="Barbarian Barrel"),
        dict(_DART, name="Witch"),
    ]
    deck[4] = dict(_SKARMY, unit_count="one", name="Valkyrie")  # no swarm reprieve
    cov = deck_role_coverage(deck, avg_elixir=3.5)
    assert cov["tank_answers"] == []
    assert any("melts a tank" in g or "tank answer" in g for g in cov["gaps"])
    assert "Rocket" not in cov["air_answers"]["spells"]


def test_a_defensive_building_counts_as_an_air_defender():
    """Tesla and Inferno Tower are buildings, not troops, and are among the best
    anti-air in the game. The predicate was once called is_air_troop, which was
    accurate for 58 of the 61 cards satisfying it and wrong for the three that
    matter most on defence. A deck holding Tesla has real, deployed air coverage
    and must never be told its air answers are spells only."""
    tesla = {
        "name": "Tesla",
        "targets": "air_and_ground",
        "spell_tier": "none",
        "role": "building",
        "unit_domain": "none",
        "dps_tier": "medium",
    }
    assert is_air_defender(tesla) is True
    assert is_air_answer(tesla) is True

    arrows = {
        "name": "Arrows",
        "targets": "air_and_ground",
        "spell_tier": "small",
        "splash_hits_air": True,
    }
    assert is_air_answer(arrows) is True
    assert is_air_defender(arrows) is False, "a spell is cast once, not deployed"
