"""`battle_time` is CR-compact, and an ISO cutoff against it is silently true.

    '20260418T153949.000Z' >= '2026-04-18T15:39:49'   ->  True

Char 4 is '0' (48) against '-' (45). SQLite compares these as TEXT, so an ISO
bound is `>=` every row in the table. The failure has no symptom: nothing
raises, nothing returns empty — a "last 7 days" query returns the entire
history and the resulting numbers look completely plausible.

Every row in production is compact (13,208 of 13,208 at the time of writing),
so this is not a mixed-format problem to be tolerated; it is one format with a
trap next to it. The repo had already learned this lesson FOUR separate times
in four local workarounds — `runtime/member_report.py` and
`storage/events_read.py` both carry a comment explaining it — and centralized
it zero times.

These tests are the centralization. `engine.clock.battle_cutoff()` states the
format once, and the grep gate below stops a new caller from inventing an ISO
bound the next time someone adds an activity or streak feature.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine.clock import BATTLE_TIME_FORMAT, battle_cutoff

ROOT = Path(__file__).resolve().parents[1]
SEARCHED = ("engine", "storage", "runtime", "capabilities", "agent")


def test_iso_bound_admits_the_whole_year():
    """The trap, asserted precisely — the mechanism is the year prefix.

    The four year digits compare equal, and then the compact string's next
    character is a DIGIT ('0'-'9', 48-57) against the ISO string's '-' (45).
    Digit wins. So every row from the bound's year onward is `>=` the bound,
    whatever its month and day — a "last 7 days" window silently becomes
    "everything this year". Rows from an earlier year do fall out, which is why
    this reads as working when you spot-check old data.

    In this database that distinction is academic: all 13,208 rows are 2026, so
    a 2026 ISO bound returns 100% of the table.
    """
    iso_bound = "2026-04-18T15:39:49"

    # Same year, months either side of the bound — all admitted.
    assert "20260418T153949.000Z" >= iso_bound
    assert "20260101T000000.000Z" >= iso_bound, "January admitted by an April bound"
    assert "20261231T235959.000Z" >= iso_bound

    # Earlier years do fall out — the reason this looks correct in spot checks.
    assert not ("20250101T000000.000Z" >= iso_bound)

    # A compact bound behaves the way a bound is supposed to.
    compact_bound = "20260418T153949.000Z"
    assert "20260418T153949.000Z" >= compact_bound
    assert not ("20260101T000000.000Z" >= compact_bound)


def test_battle_cutoff_is_compact_and_bounds_correctly():
    now = datetime(2026, 4, 25, 15, 39, 49, tzinfo=timezone.utc)
    cutoff = battle_cutoff(7, now=now)

    assert cutoff == "20260418T153949.000Z"
    assert re.fullmatch(r"\d{8}T\d{6}\.000Z", cutoff), "must be CR-compact, never ISO"

    seven_days_ago = "20260418T153949.000Z"
    eight_days_ago = "20260417T153949.000Z"
    assert seven_days_ago >= cutoff
    assert not (eight_days_ago >= cutoff)


def test_battle_cutoff_normalizes_a_naive_or_offset_now():
    """A caller passing local time must not shift the window."""
    utc = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
    offset = utc.astimezone(timezone(timedelta(hours=-5)))
    assert battle_cutoff(7, now=offset) == battle_cutoff(7, now=utc)


def _sources():
    for package in SEARCHED:
        yield from (ROOT / package).rglob("*.py")


def test_no_source_compares_battle_time_to_an_iso_bound():
    """The gate. A new ISO bound must fail here rather than in production.

    Matches a `battle_time` comparison on the same line as an ISO-shaped
    format or `.isoformat()` — the two ways this gets written by accident.
    """
    iso_shapes = re.compile(r"%Y-%m-%d|\.isoformat\(")
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "battle_time" in line and iso_shapes.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:90]}")
    assert offenders == [], (
        "battle_time compared against an ISO bound — that is silently true for "
        "every row. Use engine.clock.battle_cutoff() or "
        f"strftime('{BATTLE_TIME_FORMAT}', ...).\n" + "\n".join(offenders)
    )


def test_the_format_is_stated_in_exactly_one_place():
    """Guards the centralization, not just today's correctness.

    Python-side cutoffs must come from `battle_cutoff`. SQL-side bounds legitimately
    inline the strftime format inside a query string, so those are allowed — what is
    not allowed is a NEW Python `.strftime("%Y%m%dT...")` building a battle bound
    outside `engine/clock.py`.
    """
    pattern = re.compile(r"\.strftime\(\s*[\"']%Y%m%dT")
    offenders = []
    for path in _sources():
        if path.name == "clock.py" and path.parent.name == "engine":
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")

    # These predate the helper and are correct as written; the assertion pins the
    # set so a NEW one has to be added deliberately (and should call battle_cutoff).
    known = {
        "storage/cards.py:198",
        "storage/cards.py:1091",
        "storage/player.py:870",
        "storage/events_read.py:133",
        "storage/tournament.py:41",
        "storage/war_status.py:995",
        "runtime/member_report.py:53",
    }
    new = sorted(set(offenders) - known)
    assert new == [], (
        "new hand-rolled battle-time format(s). Call engine.clock.battle_cutoff() "
        "instead:\n  " + "\n  ".join(new)
    )


@pytest.mark.parametrize("days", [1, 7, 28, 90])
def test_cutoff_round_trips_through_the_parser(days):
    """What we emit must be readable by the one parser."""
    from engine.clock import parse_battle_time

    parsed = parse_battle_time(battle_cutoff(days))
    assert parsed is not None, f"battle_cutoff({days}) is not parseable by parse_battle_time"
