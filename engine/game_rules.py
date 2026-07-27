"""Canonical deterministic Clash Royale mechanics used across engine layers.

This module is intentionally small: it contains rules Elixir must never ask an
LLM to reconstruct.  Capabilities expose these rules to tools and composition;
engine/read layers import them directly so duplicated constants cannot drift.
"""

from __future__ import annotations

NORMAL_RIVER_RACE_FINISH_LINE = 10_000
COLOSSEUM_FINISH_LINE = None
RIVER_RACE_BATTLE_DAYS = 4


def is_colosseum(period_type: str | None) -> bool:
    return str(period_type or "").strip().lower() == "colosseum"


def river_race_finish_line(period_type: str | None) -> int | None:
    """Return the real finish line for this week type.

    Colosseum has no finish line: standings continue across all four battle
    days.  This was verified against live Season 133 data and is pinned here so
    prompts, reads, and the engine cannot revive the retired 5,000-point myth.
    """
    return COLOSSEUM_FINISH_LINE if is_colosseum(period_type) else NORMAL_RIVER_RACE_FINISH_LINE


def river_race_completed_from_score(
    period_type: str | None,
    score: int | float | None,
    *,
    active_battle_phase: bool,
) -> bool:
    """Whether a score alone proves the race is complete.

    Only a normal River Race has a score threshold.  Colosseum completion is
    determined by the end of its four-day competition, never by crossing a
    number mid-week.
    """
    finish_line = river_race_finish_line(period_type)
    if not active_battle_phase or finish_line is None:
        return False
    try:
        return int(score or 0) >= finish_line
    except TypeError, ValueError:
        return False


def river_race_mechanics(period_type: str | None, phase: str | None = None) -> dict:
    """Audience-neutral mechanics contract for composition and tools.

    A River Race week varies along TWO independent axes, and conflating them is
    what produced the 2026-07-27 miss (advice to add boat defenses during a
    Colosseum week):

    * **week type** — a normal week is a BOAT race: the clan sails, players earn
      fame for it, and the boat can be attacked and defended. Colosseum is the
      season's final week and the boat is PARKED: no boat battles, no boat
      defenses, no fame — only war points, with no finish line.
    * **day type** — within either week, the first three days are practice and the
      last four are battle days. Boat defenses can ONLY BE ADDED during practice
      days; they are not used then. On battle days they cannot be added any more,
      but they pay survival fame at each day's close.

    ``phase`` accepts the several vocabularies in use ("practice"/"training" and
    "battle"/"war_day"); when omitted, the day-scoped keys are None rather than
    guessed.
    """
    colosseum = is_colosseum(period_type)
    practice = str(phase or "").lower() in {"practice", "training"}
    battle = str(phase or "").lower() in {"battle", "war_day", "warday", "colosseum"}
    known_phase = practice or battle

    # The boat is the whole difference. Everything boat-shaped is false in Colosseum.
    boat_in_play = not colosseum
    return {
        "period_type": "colosseum" if colosseum else "normal",
        "is_colosseum_week": colosseum,
        "battle_days": RIVER_RACE_BATTLE_DAYS,
        "finish_line": river_race_finish_line(period_type),
        "finish_line_metric": None if colosseum else "fame",
        "score_metric": "points" if colosseum else "fame",
        "every_battle_counts_for_standings": True,
        # --- boat axis -------------------------------------------------------
        "boat_in_play": boat_in_play,
        "boat_defenses_exist": boat_in_play,
        "boat_battles_exist": boat_in_play,
        # Defenses can be ADDED only on a normal week's practice days. They are
        # never addable on battle days, and never exist at all in Colosseum.
        "defenses_can_be_added": bool(boat_in_play and practice) if known_phase else None,
        "defenses_earn_fame_today": bool(boat_in_play and battle) if known_phase else None,
        # The API reports numOfDefensesRemaining only for CLOSED days, so during
        # the practice build window there is no live count. Advise, never quote.
        "defense_count_available": False if (practice and boat_in_play) else boat_in_play,
        "objective": (
            "Maximise total war points across all four battle days — there is no finish line."
            if colosseum
            else "Reach the 10,000 fame finish line before the other clans."
        ),
        "completion_rule": (
            "Colosseum has no finish line; standings continue through all four battle days."
            if colosseum
            else "The normal weekly race finishes at 10,000 clan fame."
        ),
        "member_language": (
            "Every Colosseum battle continues to count toward clan and member standings."
            if colosseum
            else "Member contributions are points; fame is the clan boat metric."
        ),
        "boat_guidance": _boat_guidance(colosseum, practice, battle),
    }


def _boat_guidance(colosseum: bool, practice: bool, battle: bool) -> str:
    """The strategy that follows from the two axes (clan-leader ratified)."""
    if colosseum:
        return (
            "The boat is parked in Colosseum week: there are no boat battles and no boat "
            "defenses. Never suggest adding, repairing or checking defenses, and never cite "
            "defense fame. Practice days are for deck preparation only."
        )
    if practice:
        return (
            "Practice days are the ONLY window to add boat defenses, and full defenses is how "
            "a week can be won in three days — so this is the moment to push members to set "
            "them. The API does not report defenses placed during practice, so encourage the "
            "action without claiming a count."
        )
    if battle:
        return (
            "Defenses can no longer be added — they now pay survival fame at each day's close "
            "on top of placement fame. Boat battles are generally a poor use of an attack "
            "compared with river-race battles."
        )
    return (
        "Boat defenses are added on practice days only and pay survival fame on battle days; "
        "boat battles are generally a poor use of an attack."
    )


__all__ = [
    "COLOSSEUM_FINISH_LINE",
    "NORMAL_RIVER_RACE_FINISH_LINE",
    "RIVER_RACE_BATTLE_DAYS",
    "is_colosseum",
    "river_race_completed_from_score",
    "river_race_finish_line",
    "river_race_mechanics",
]
