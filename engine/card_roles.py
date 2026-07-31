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
    if our_answers == 0:
        return "stressed"
    if our_answers == 1 and their_air >= 2:
        return "stressed"
    return "favored" if our_answers >= 3 else "even"


def wincon_pressure(our: dict, their: dict) -> Optional[str]:
    """Could their defense actually handle our win condition?"""
    if our is None or their is None:
        return None
    if not our.get("win_condition_count"):
        return "no_wincon"  # checked before `their`: a deck with no win-con is the story
    if not their:
        return None
    defense = their.get("tank_answer_count", 0) + their.get("splash_answer_count", 0)
    if defense == 0:
        return "clear"
    return "countered" if defense >= 4 else "contested"


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
