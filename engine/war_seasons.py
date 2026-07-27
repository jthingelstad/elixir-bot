"""War-season shape — how many weeks a season has, and which one is Colosseum.

A Clash Royale war season runs **first-Monday-of-month → first-Monday-of-the-next-
month**, so it is 4 or 5 sections (weeks) long, and **Colosseum is always the final
section**. That is the same first-Monday cadence ranked seasons use
(:mod:`engine.pol_seasons`), so ``first_monday`` is reused rather than re-derived.

Why this module exists
----------------------
The CR API cannot tell you it is Colosseum week during that week's PRACTICE days:
``periodType`` only flips to ``colosseum`` once battle days begin, and a
Colosseum-week practice day is byte-for-byte indistinguishable from a normal-week
practice day (verified by payload diff; see ``docs/cr-api-docs/clans.md``). On
2026-07-27 that gap produced clan-war advice to add boat defenses during a
Colosseum week, when the boat is parked and no defenses exist.

The season's *shape*, however, is deterministic from the calendar. Validated
against every season on record (`poapkings.com/data/clash-royale.sqlite`,
`river_race_weeks`, which carries the authoritative `is_colosseum` flag):

    season 129  2026-02-02 → 03-02   4 weeks   colosseum = section 3  ✓
    season 130  2026-03-02 → 04-06   5 weeks   colosseum = section 4  ✓
    season 131  2026-04-06 → 05-04   4 weeks   colosseum = section 3  ✓
    season 132  2026-05-04 → 06-01   4 weeks   colosseum = section 3  ✓
    season 133  2026-06-01 → 07-06   5 weeks   colosseum = section 4  ✓

This also explains the API docs' hedge that Supercell "varies the war season
length": the variation is only whether 4 or 5 Mondays fall between first-Mondays.

This module is deliberately pure date math — no DB, no payload parsing — so the
engine, storage and read layers can all share one answer.
"""

from __future__ import annotations

from datetime import date, timedelta

from engine.normalize import PERIODS_PER_SECTION
from engine.pol_seasons import first_monday

__all__ = [
    "final_section_index",
    "is_final_section",
    "season_bounds",
    "section_index_for",
    "total_sections",
]


def _next_month(on: date) -> tuple[int, int]:
    return (on.year, on.month + 1) if on.month < 12 else (on.year + 1, 1)


def _prev_month(on: date) -> tuple[int, int]:
    return (on.year, on.month - 1) if on.month > 1 else (on.year - 1, 12)


def season_bounds(on: date) -> tuple[date, date]:
    """``(start, next_start)`` of the war season containing ``on``.

    Handles the month-boundary case correctly: season 133's Colosseum week ran
    2026-06-29 → 07-06, so a date of 2026-07-02 belongs to the season that STARTED
    on 2026-06-01, not to July's season.
    """
    fm = first_monday(on.year, on.month)
    if on >= fm:
        return fm, first_monday(*_next_month(on))
    return first_monday(*_prev_month(on)), fm


def total_sections(on: date) -> int:
    """Number of war weeks in the season containing ``on`` (4 or 5)."""
    start, nxt = season_bounds(on)
    return (nxt - start).days // PERIODS_PER_SECTION


def final_section_index(on: date) -> int:
    """0-based index of the season's last section — always Colosseum week."""
    return total_sections(on) - 1


def section_index_for(on: date) -> int:
    """0-based section (week) index of ``on`` within its season."""
    start, _ = season_bounds(on)
    return (on - start).days // PERIODS_PER_SECTION


def is_final_section(on: date, section_index: int | None = None) -> bool:
    """Whether ``section_index`` (default: the one ``on`` falls in) is the season's
    last — i.e. Colosseum week.

    Callers should pass the API's ``sectionIndex`` when they have it: it is the
    authority on where we are, while the calendar is the authority on how long the
    season is.
    """
    if section_index is None:
        section_index = section_index_for(on)
    return section_index == final_section_index(on)


def season_shape(on: date, section_index: int | None = None) -> dict:
    """One bundle of the season's shape, for reads and prompts.

    ``week``/``total_weeks`` are 1-based for humans ("week 3 of 4");
    ``section_index``/``final_section_index`` stay 0-based like the API.
    """
    if section_index is None:
        section_index = section_index_for(on)
    total = total_sections(on)
    final_index = total - 1
    start, nxt = season_bounds(on)
    return {
        "season_start": start.isoformat(),
        "season_end": nxt.isoformat(),
        "section_index": section_index,
        "final_section_index": final_index,
        "week": section_index + 1,
        "total_weeks": total,
        "weeks_remaining": max(0, final_index - section_index),
        "is_final_week": section_index == final_index,
    }


def war_date(on_utc) -> date:
    """The war-calendar date for a UTC datetime.

    CR periods roll at ~10:00 UTC, so the war day that is "today" before the reset
    belongs to the previous calendar date. Mirrors ``engine.clock._effective_war_date``.
    """
    return (on_utc - timedelta(hours=10)).date()
