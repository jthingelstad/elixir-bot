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
    except (TypeError, ValueError):
        return False


def river_race_mechanics(period_type: str | None) -> dict:
    """Audience-neutral mechanics contract for composition and tools."""
    colosseum = is_colosseum(period_type)
    return {
        "period_type": "colosseum" if colosseum else "normal",
        "is_colosseum_week": colosseum,
        "battle_days": RIVER_RACE_BATTLE_DAYS,
        "finish_line": river_race_finish_line(period_type),
        "finish_line_metric": None if colosseum else "fame",
        "every_battle_counts_for_standings": True,
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
    }


__all__ = [
    "COLOSSEUM_FINISH_LINE",
    "NORMAL_RIVER_RACE_FINISH_LINE",
    "RIVER_RACE_BATTLE_DAYS",
    "is_colosseum",
    "river_race_completed_from_score",
    "river_race_finish_line",
    "river_race_mechanics",
]
