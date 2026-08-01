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
        "small_spell_count": 0,
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
            # COUNT, not just presence. One small spell versus two is the cleanest
            # quantifiable matchup line in the game: a bait deck runs more
            # spell-fragile cards than a one-spell opponent can answer, so the
            # opponent must pick which threat to eat. `has_small_spell` cannot
            # express that, and the whole reason competent players run a second
            # small spell is to close it.
            counts["small_spell_count"] = counts.get("small_spell_count", 0) + 1
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


def deck_role_coverage(facts: Iterable[dict], *, family=None, avg_elixir=None) -> dict:
    """Which formula slot each card fills, and what the deck is missing.

    ``family`` is accepted and unused: it once drove an elixir-band check that fired
    on 61% of decks and was worth 2.5 points of win rate. See matchup_notes.

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
    coverage["gaps"] = gaps
    return coverage


# ── why a matchup goes the way it does ───────────────────────────────────────
#
# The line this code has to hold: deck STRUCTURE does not predict a single battle.
# Measured here across 9,481 real-level battles, air coverage is flat-to-inverted
# against outcome (0 air answers 62.3%, 3 answers 56.8%), and tank answers look
# monotonic pooled but fall apart inside an elixir band. An independent study on
# 70,200 battles puts deck composition at ~57% predictive accuracy versus 50%
# chance — about seven points total, with the rest being how people play.
#
# So these notes are NOT causes of a loss. They are the mechanism behind a
# TENDENCY, and they are only worth saying next to a measured pattern: "you are
# 4-11 into beatdown this month, and here is the structural reason that matchup is
# hard for this list." The pattern is the evidence; the note is the explanation.
# Each one below is list-diagnosable — checkable from eight cards, no gameplay.

_BAIT_CARD_FLOOR = 3  # a bait deck is one running enough fragile cards to force a choice


def matchup_notes(coverage: dict, their_family: Optional[str]) -> list[str]:
    """Structural reasons THIS deck tends to struggle against ``their_family``.

    Empty is a real answer and the common one: most decks have no structural
    problem with most archetypes, and inventing one to look useful is the failure
    mode this whole layer exists to avoid.
    """
    if not coverage or not coverage.get("facts_complete") or not their_family:
        return []
    air = coverage.get("air_answers") or {}
    notes: list[str] = []

    if their_family == "bait":
        # The cleanest quantifiable line in the game: a bait deck runs more
        # spell-fragile cards than one small spell can answer, so the defender has
        # to pick which threat to eat. Measured here: 0 spells 52.7%, 1 spell
        # 53.8%, 2+ 55.5% (n=1,144) — monotonic and in the predicted direction,
        # though only ~3 points, so this is deck advice and not a loss diagnosis.
        spells = len(coverage.get("small_spells") or [])
        if spells <= 1:
            have = "one small spell" if spells else "no small spell"
            notes.append(
                f"{have} against a bait deck — they run more spell-bait cards than "
                "you have answers, so something gets through every rotation"
            )
    if their_family == "beatdown":
        if not coverage.get("tank_answers"):
            notes.append(
                "nothing here melts a tank — swarming a Golem is not the same as "
                "answering it, and beatdown wins the long game if the tank survives"
            )
        elif not air.get("heavy") and not coverage.get("big_spells"):
            notes.append(
                "no heavy air answer and no big spell, which is the shape that loses "
                "to a Lava Hound specifically"
            )
    if their_family in ("bridge spam", "cycle"):
        if not coverage.get("splash_answers"):
            notes.append(
                "no splash, and fast decks bring cheap units in numbers — you spend "
                "more per answer than they spend per threat"
            )
    if their_family == "siege" and not coverage.get("big_spells"):
        notes.append(
            "no big spell to remove a siege building, so you have to win the race "
            "rather than end it"
        )
    return notes


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


def decisive_factor(
    *,
    level_gap: Optional[float],
    level_ok: bool,
    closeness: Optional[int],
    discipline_delta: Optional[float],
) -> str:
    """The single biggest driver of this battle's result, ranked.

    Every rank below is a factor MEASURED to separate outcome across 12,687 clan
    battles (53.6% baseline). Nothing else is accepted: air coverage, opponent
    defense and archetype matchup were all tried, all measured, and all cut.

        card_levels   level_gap -3 -> 48.7%, +2 -> 61.4%, +4 -> 71.1% (monotonic)
        elixir_mgmt   |delta| >= 3.5 splits 41.1% / 65.1%
        coin_flip     closeness band 3 is the measured toss-up
        (dropped)     opponent defense 4..8 -> 52.7/53.1/53.7/52.8/53.7%, flat
        (dropped)     air deficit -5..-2 -> all within 1.5% of baseline
        (dropped)     archetype matchup -> mean |player-adjusted lift| 3.2% over 20
                      cells; siege's apparent -10 was selection, not the deck

    Card levels outrank elixir because levels are the thing a player can act on,
    and because elixir leak is partly an EFFECT of losing rather than a cause.
    """
    if level_ok and level_gap is not None and abs(level_gap) >= 2.0:
        return "card_levels"
    if discipline_delta is not None and abs(discipline_delta) >= 3.5:
        return "elixir_management"
    if closeness == 3:
        return "coin_flip"
    return "even_game"
