"""Derived card/deck roles — the rules layer over enriched card facts (v2 Layer 2).

The LLM asserts behavior PRIMITIVES per card form (`card_facts`); the deck-level roles
a player actually cares about are derived here, in code:

    "does this deck have an air answer?"  ->  any card whose `targets` includes air
    "a tank answer?"                      ->  high single-target dps
    "a splash answer?"                    ->  area damage

Keeping roles derived (rather than asked of the model) shrinks the model's error surface
to simple checkable facts, makes every role auditable in one place, and lets us fix a
role definition without re-enriching 177 cards.

The deck-completeness fields mirror the community deck formula (one win condition, one
big spell, one small spell, one tank answer, one air answer, one splash answer, two cheap
cycle cards), which is what makes "your deck has no air answer" a computed fact rather
than a model opinion.

Pure functions. No DB, no I/O, no model.
"""

from __future__ import annotations

from typing import Iterable, Optional

CYCLE_MAX_ELIXIR = 2  # "cheap cycle card" per the deck formula


def is_air_answer(fact: dict) -> bool:
    """Can this card shoot down air? The single most load-bearing derived role —
    a deck with none of these loses to Balloon/Lava/Minion decks on structure alone."""
    if fact.get("targets") == "air_and_ground":
        return True
    # A cheap spell that hits air counts as a (limited) answer: Arrows/Zap/Fireball.
    # Big spells (Rocket/Lightning) do NOT — nobody defends a Lava push with Rocket.
    return bool(fact.get("spell_tier") in ("small", "medium") and fact.get("splash_hits_air"))


def is_air_troop(fact: dict) -> bool:
    """An air answer you can DEPLOY, as opposed to one you cast once and lose.

    is_air_answer() deliberately counts small spells, and that conflation shipped a
    real bad recommendation: a member was handed a deck whose "3 air answers" were
    Arrows, Giant Snowball, and Furnace — two one-shot spells and a spawner, no unit
    that can shoot up. Against a Balloon that deck has nothing that survives contact.
    The community tooling makes the same split (deckshop.pro tags "anti-air troop"
    separately from its spell properties), so report the two separately and let the
    reader see which kind of coverage they actually have.
    """
    return fact.get("targets") == "air_and_ground" and fact.get("spell_tier") in (None, "none")


def is_heavy_air_answer(fact: dict) -> bool:
    """Can it kill a Lava Hound or a Balloon before the tower dies — deckshop.pro's
    "air tank killer" tier. Chip damage that merely *reaches* air is not the same
    answer, and a deck can satisfy a numeric air-count floor with none of this."""
    return is_air_troop(fact) and fact.get("dps_tier") == "high"


def is_tank_answer(fact: dict) -> bool:
    """High single-target DPS — what actually melts a big tank. Splash units chew
    swarms but bounce off a Golem, so attack_style matters as much as dps."""
    return fact.get("dps_tier") == "high" and fact.get("attack_style") in ("single", "chain")


def is_splash_answer(fact: dict) -> bool:
    """Area damage — the natural counter to swarms."""
    return fact.get("attack_style") in ("splash_small", "splash_large")


def is_swarm(fact: dict) -> bool:
    return fact.get("unit_count") == "many" or fact.get("role") == "swarm"


def is_bait_unit(fact: dict) -> bool:
    """A unit a small spell wipes — the spell-bait tell."""
    return bool(fact.get("fragile_to_small_spell"))


def is_cycle_card(fact: dict, elixir_cost: Optional[float]) -> bool:
    return elixir_cost is not None and elixir_cost <= CYCLE_MAX_ELIXIR


def deck_facts(facts: Iterable[dict]) -> dict:
    """Roll a deck's 8 card-facts into the deck-formula completeness counts.

    ``facts`` items are card_facts rows (dicts), each optionally carrying
    ``elixir_cost``. Cards with no enriched facts are skipped and reported via
    ``facts_complete`` so a partial deck never masquerades as a complete read.
    """
    rows = [f for f in facts if f]
    counts = {
        "air_answer_count": 0,
        "tank_answer_count": 0,
        "splash_answer_count": 0,
        "swarm_count": 0,
        "bait_unit_count": 0,
        "has_big_spell": 0,
        "has_small_spell": 0,
        "win_condition_count": 0,
        "cycle_card_count": 0,
    }
    for f in rows:
        if is_air_answer(f):
            counts["air_answer_count"] += 1
        if is_tank_answer(f):
            counts["tank_answer_count"] += 1
        if is_splash_answer(f):
            counts["splash_answer_count"] += 1
        if is_swarm(f):
            counts["swarm_count"] += 1
        if is_bait_unit(f):
            counts["bait_unit_count"] += 1
        if f.get("spell_tier") == "big":
            counts["has_big_spell"] = 1
        if f.get("spell_tier") == "small":
            counts["has_small_spell"] = 1
        if f.get("is_win_condition"):
            counts["win_condition_count"] += 1
        if is_cycle_card(f, f.get("elixir_cost")):
            counts["cycle_card_count"] += 1
    counts["facts_complete"] = 1 if len(rows) == 8 else 0
    return counts


# ── deck-formula coverage (what goes back to the player) ─────────────────────
#
# deck_facts() above answers "does this deck pass?" for the candidate filter. The
# functions below answer "why is each card here, and what is missing?", which is the
# half a player can actually learn from. Same predicates, reported instead of consumed.

# Published archetype elixir bands, cross-checked against our own corpus (11,775
# profiled decks): our observed means land inside every band we could check — cycle
# 2.98 (band <3.0), bait 3.41 (3.0-3.5), beatdown 4.07 (4.0-4.5+). The tails are the
# problem the check exists for: "beatdown" decks up to 7.25 elixir and "siege" up to
# 6.75 are all real decks somebody played, and all terrible things to recommend.
# Ranges are (low, high); None = unbounded on that side.
ELIXIR_BANDS = {
    "cycle": (None, 3.0),
    "control": (3.0, 3.6),
    "siege": (3.0, 3.6),
    "bait": (3.0, 3.6),
    "bridge spam": (3.4, 3.9),
    "beatdown": (4.0, 4.6),
}

# One Evolution slot + one Hero slot + one Wild slot (Evo OR Hero OR Champion) since
# the March 2026 update. Verified first-party against 13,701 real 8-card decks played
# in July 2026: the special-form count is 0, 1, 2, or 3 and NEVER 4+. Every deck we
# profile is one somebody actually played, so nothing violates this today — but the
# invariant lives nowhere else, and the first deck built combinatorially rather than
# observed would be unfieldable with no test to catch it.
MAX_SPECIAL_SLOTS = 3

# Guides converge on 2-3 air answers, with an explicit exemption for very cheap cycle
# decks that defend by rotation. Our floor was 1, which passed decks that answer air
# with a single spell. Measured cost of moving to 2: 113 of 11,775 profiles (1.0%).
CHEAP_CYCLE_ELIXIR = 2.8


def min_air_answers(avg_elixir: Optional[float]) -> int:
    """The air floor this deck should clear. Cheap cycle decks get the exemption the
    guides give them; everything else needs two."""
    if avg_elixir is not None and avg_elixir <= CHEAP_CYCLE_ELIXIR:
        return 1
    return 2


def elixir_band_note(family: Optional[str], avg_elixir: Optional[float]) -> Optional[str]:
    """``None`` when the deck's cost fits its archetype, else a plain-language note.

    A 7-elixir "beatdown" is not a beatdown deck, it is a deck that cannot cycle. The
    archetype label alone never says that, so nothing downstream could warn about it.
    """
    if family is None or avg_elixir is None:
        return None
    band = ELIXIR_BANDS.get(family)
    if not band:
        return None
    low, high = band
    if low is not None and avg_elixir < low:
        return f"{avg_elixir:.2f} elixir is cheap for {family} (typical {low:.1f}-{high:.1f})"
    if high is not None and avg_elixir > high:
        return f"{avg_elixir:.2f} elixir is heavy for {family} (typical {low or 0:.1f}-{high:.1f})"
    return None


def deck_role_coverage(facts: Iterable[dict], *, family=None, avg_elixir=None) -> dict:
    """Which formula slot each card fills, and what the deck is missing.

    ``facts`` items are card_facts rows carrying ``name`` and ``elixir_cost``. Cards
    are listed under EVERY role they fill — Valkyrie is a mini-tank and a splash answer
    and an anti-swarm card at once, and collapsing that to one label is what makes
    "why is this card here?" unanswerable.
    """
    rows = [f for f in facts if f]
    if not rows:
        # No enrichment for a single card in this deck. Every named() below would come
        # back empty and every gap would then be asserted — "no win condition, no air
        # answer, no spell" about a deck we simply have not looked at. Absence of facts
        # is not absence of roles; say nothing rather than invent a critique.
        return {"facts_complete": False, "gaps": [], "unknown": True}

    def named(pred) -> list[str]:
        return [f.get("name") for f in rows if pred(f) and f.get("name")]

    air_troops = named(is_air_troop)
    air_spells = named(lambda f: is_air_answer(f) and not is_air_troop(f))
    coverage = {
        "win_conditions": named(lambda f: f.get("is_win_condition")),
        "air_answers": {
            "troops": air_troops,
            "spells": air_spells,
            "heavy": named(is_heavy_air_answer),
            "count": len(air_troops) + len(air_spells),
        },
        "tank_answers": named(is_tank_answer),
        "splash_answers": named(is_splash_answer),
        "swarm": named(is_swarm),
        "big_spells": named(lambda f: f.get("spell_tier") == "big"),
        "small_spells": named(lambda f: f.get("spell_tier") == "small"),
        "cycle_cards": named(lambda f: is_cycle_card(f, f.get("elixir_cost"))),
        "facts_complete": len(rows) == 8,
    }

    # Gaps are phrased as the sentence a player should hear, not as a field name. An
    # empty list is the honest "this deck is structurally sound" — never invent one.
    #
    # Every gap below is an absence claim, and an absence claim is only true if we
    # looked at all eight cards. With seven enriched and the win condition among the
    # missing one, "no clear win condition" would be asserted about a deck that has
    # one. Partial facts get the coverage they earned and no critique.
    gaps: list[str] = []
    if not coverage["facts_complete"]:
        coverage["gaps"] = gaps
        coverage["unknown_cards"] = 8 - len(rows)
        return coverage
    if not coverage["win_conditions"]:
        gaps.append("no clear win condition — nothing here reliably reaches the tower")
    floor = min_air_answers(avg_elixir)
    if coverage["air_answers"]["count"] < floor:
        gaps.append(f"only {coverage['air_answers']['count']} air answer(s); wants {floor}")
    elif not air_troops:
        gaps.append("air coverage is spells only — no unit here can shoot up")
    elif not coverage["air_answers"]["heavy"]:
        gaps.append("can chip air but has no heavy air answer for a Balloon or Lava Hound")
    if not coverage["tank_answers"]:
        gaps.append("no real tank answer — swarming a tank is not the same as answering it")
    if not coverage["splash_answers"]:
        gaps.append("no splash — swarms go unanswered")
    if not coverage["small_spells"]:
        gaps.append("no small spell")
    if not coverage["big_spells"]:
        gaps.append("no big spell")
    band = elixir_band_note(family, avg_elixir)
    if band:
        gaps.append(band)
    coverage["gaps"] = gaps
    return coverage


# ── per-battle structural tags (v2 Layer 3) ──────────────────────────────────
#
# These replace Feature 3's prose: the same reads, as structured data a summarizer
# can aggregate, with no per-battle model call and no elixir-discipline crutch.

# Modes where card levels are normalized in-battle, so a level_gap computed from the
# players' ACCOUNT levels is fictional. This is the Feature-3 bug, fixed: prose cited
# "4 levels down" on Showdown_Friendly battles where everyone plays at tournament caps.
_LEVEL_NORMALIZED_MODES = ("showdown", "tournament", "challenge", "friendly", "draft")


def level_validity(game_mode_name: Optional[str], is_ranked) -> str:
    """``real`` when the battle used the players' own card levels, else ``normalized``."""
    if is_ranked:
        return "normalized"  # Path of Legends caps every card at level 11
    mode = (game_mode_name or "").lower()
    return "normalized" if any(m in mode for m in _LEVEL_NORMALIZED_MODES) else "real"


def air_matchup(our: dict, their: dict) -> Optional[str]:
    """Did we bring answers to what they fly? ``stressed`` is the read that matters:
    they had real air pressure and we had ~nothing to shoot it down."""
    if not our or not their:
        return None
    their_air = their.get("air_threat_count", 0)
    our_answers = our.get("air_answer_count", 0)
    if their_air == 0:
        return "untested"
    # Thresholds are calibrated to the observed spread (0-6, median 4): "stressed"
    # is the bottom tail, "favored" the top, so each tag means something.
    if our_answers <= 1 or (our_answers == 2 and their_air >= 3):
        return "stressed"
    return "favored" if our_answers >= 5 else "even"


def wincon_pressure(our: dict, their: dict) -> Optional[str]:
    """Could their defense actually handle our win condition?"""
    if our is None or their is None:
        return None
    if not our.get("win_condition_count"):
        return "no_wincon"  # checked before `their`: a deck with no win-con is the story
    if not their:
        return None
    defense = their.get("tank_answer_count", 0) + their.get("splash_answer_count", 0)
    # Observed spread is 2-8 with median 5, so "countered" is the top tail (>=7) and
    # "clear" the bottom (<=3). An earlier >=4 cut tagged 92% of decks "countered".
    if defense <= 3:
        return "clear"
    return "countered" if defense >= 7 else "contested"


def spell_bait_exposed(our: dict, their: dict) -> Optional[int]:
    """Did we bring a pile of spell-fragile units into a deck holding a small spell?"""
    if not our or not their:
        return None
    return 1 if our.get("bait_unit_count", 0) >= 3 and their.get("has_small_spell") else 0


def decisive_factor(
    *,
    level_gap: Optional[float],
    level_ok: bool,
    closeness: Optional[int],
    discipline_delta: Optional[float],
    performance: Optional[int],
    air: Optional[str],
    wincon: Optional[str],
) -> str:
    """The single biggest driver of this battle's result, ranked.

    Every rank below is a factor MEASURED to separate outcome across 12,687 clan
    battles (53.6% baseline). ``air`` and ``wincon`` are accepted for signature
    compatibility but deliberately unused: at complete card-facts coverage their
    win rates are flat, so they describe a deck without diagnosing a battle.

        card_levels   level_gap -3 -> 48.7%, +2 -> 61.4%, +4 -> 71.1% (monotonic)
        elixir_mgmt   |delta| >= 3.5 splits 41.1% / 65.1%
        coin_flip     closeness band 3 is the measured toss-up
        (dropped)     opponent defense 4..8 -> 52.7/53.1/53.7/52.8/53.7%, flat
        (dropped)     air deficit -5..-2 -> all within 1.5% of baseline

    Card levels outrank elixir because levels are the thing a player can act on,
    and because elixir leak is partly an EFFECT of losing rather than a cause.
    """
    if level_ok and level_gap is not None and abs(level_gap) >= 2.0:
        return "card_levels"
    if discipline_delta is not None and abs(discipline_delta) >= 3.5:
        return "elixir_management"
    if closeness == 3:
        return "coin_flip"
    if performance:
        return "matchup"
    return "even_game"
