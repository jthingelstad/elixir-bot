"""Deterministic Clash-Royale game-knowledge checker (confidence plan Pillar 3).

The inline Editor gate checks copy against the intent's *facts JSON*; it never
checks the facts themselves are game-accurate. This module closes that gap: given
a post's copy + the facts that drove it, it cross-checks the CR claims against
the real game-knowledge sources (the card catalog, the arena/league/war
normalizers) and returns findings where the post contradicts game reality.

CONSERVATIVE by contract: the facts JSON is the source of truth; we only flag a
CLEAR contradiction with known game knowledge. Missing / loose / unrecognized
data yields NO finding (never false-positive). Pure — no LLM, no network; the
only I/O is a read of the card_catalog table.

`check_post(copy, facts, conn=None) -> list[dict]` — each finding is
`{"claim": str, "issue": str, "severity": "warn"|"error"}`.

The classes it catches map to live incidents:
- fabricated / wrong-level card ("maxed X to level 15" when X maxes at 16;
  a card name not in the catalog) — the 2026-07-03 "signal lacks card names".
- seasonal arena narrated as an arena-up (the PANCAKES! false arena-up).
- a league name that contradicts the facts' league number/era.
- an impossible war day ("battle day 5 of 4").
- a Colosseum finish line / "remaining battles do not count" claim.
"""

from __future__ import annotations

import re

from engine.normalize import (
    arena_kind,
    card_display_max_level,
    ranked_league_name,
)

RANKED_LEAGUE_NAMES = {
    "master 1",
    "master 2",
    "master 3",
    "champion",
    "grand champion",
    "royal champion",
    "ultimate champion",
}
_LEVEL_RE = re.compile(r"\blevel\s+(\d{1,2})\b", re.IGNORECASE)
_DAY_OF_RE = re.compile(r"\bday\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
_ARENA_UP_RE = re.compile(
    r"arena[- ]?up|moved up|climbed (?:up |into )|punched into|broke into",
    re.IGNORECASE,
)
_FINISH_LINE_RE = re.compile(r"\bfinish\s+line\b", re.IGNORECASE)
_NO_FINISH_LINE_RE = re.compile(
    r"\b(?:no|without)\s+(?:a\s+|the\s+)?finish\s+line\b|"
    r"\bdoes(?:\s+not|n't)\s+have\s+(?:a\s+|the\s+)?finish\s+line\b",
    re.IGNORECASE,
)
_BATTLES_DO_NOT_COUNT_RE = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\b(?:remaining|final[- ]day|those|these|any)\s+(?:war\s+)?(?:battles|decks).{0,45}\b(?:do\s+not|don't|won't|no\s+longer)\s+(?:add|count|matter)",
        r"\bnone\s+of\s+(?:those|these|the\s+remaining).{0,35}\b(?:battles|decks).{0,45}\b(?:add|count|matter)",
        r"\b(?:purely|only|just)\s+(?:about|for)\s+(?:personal\s+)?(?:war\s+)?chest\s+rewards\b",
    )
)
# Boat talk that is impossible in a Colosseum week (the boat is parked). Matches
# the affirmative forms — "set your boat defenses", "defenses are full", "boat
# battle" — while allowing a negation ("no boat defenses this week") through.
_BOAT_IN_COLOSSEUM_RE = re.compile(
    r"(?<!no )(?<!not )(?<!never )\b(?:boat\s+defen[cs]e|boat\s+battle|defen[cs]es?\s+remaining"
    r"|repair\s+point|top\s+up\s+(?:the\s+|our\s+|your\s+)?defen[cs]e)",
    re.IGNORECASE,
)


def _finding(claim, issue, severity="warn"):
    return {"claim": claim, "issue": issue, "severity": severity}


def _card(name, conn):
    from storage.card_catalog import catalog_count, get_card_by_name

    try:
        if catalog_count(conn=conn) == 0:
            return "empty"  # catalog not synced — can't judge card names
        return get_card_by_name(name, conn=conn)
    except Exception:
        return "empty"


def _check_card(copy, facts, conn, out):
    name = facts.get("card_name")
    if not name:
        return
    milestone = facts.get("milestone")

    # Facts-only checks (no catalog needed) — run always.
    if isinstance(milestone, int) and milestone > 16:
        out.append(
            _finding(
                f"{name} level {milestone}",
                f"level {milestone} is impossible — cards top out at display 16",
                "error",
            )
        )
    if isinstance(milestone, int):
        m = _LEVEL_RE.search(copy or "")
        if m and int(m.group(1)) != milestone:
            out.append(
                _finding(
                    f"copy says level {m.group(1)}",
                    f"the facts say {name} reached level {milestone}",
                    "error",
                )
            )

    # Catalog cross-checks — only when the catalog is available.
    card = _card(name, conn)
    if card == "empty":
        return
    if card is None:
        out.append(_finding(name, f"'{name}' is not a card in the catalog", "error"))
        return
    display_max = card_display_max_level(card.get("max_level"))
    if isinstance(milestone, int) and isinstance(display_max, int) and milestone > display_max:
        out.append(
            _finding(
                f"{name} level {milestone}",
                f"{name} maxes at display level {display_max}",
                "error",
            )
        )


def _check_arena(copy, facts, out):
    arena_id = facts.get("arena_id")
    if not isinstance(arena_id, int):
        return
    if arena_kind(arena_id) == "seasonal" and _ARENA_UP_RE.search(copy or ""):
        name = facts.get("arena_name") or f"arena {arena_id}"
        out.append(
            _finding(
                name,
                f"{name} is a SEASONAL-league arena (id {arena_id}) — not a Trophy-Road arena-up",
                "error",
            )
        )


def _check_league(copy, facts, out):
    league = facts.get("ranked_league") or facts.get("league") or facts.get("new_league")
    if not isinstance(league, int):
        return
    correct = ranked_league_name(league)
    if not correct:
        return
    low = (copy or "").lower()
    # only flag if the copy names a DIFFERENT current-scheme league than the facts
    for other in RANKED_LEAGUE_NAMES:
        if other in low and other != correct.lower() and correct.lower() not in low:
            out.append(
                _finding(
                    f"copy names '{other}'",
                    f"the facts say league {league} = {correct}",
                    "error",
                )
            )
            break


def _check_war_day(copy, facts, out):
    m = _DAY_OF_RE.search(copy or "")
    if not m:
        return
    day, total = int(m.group(1)), int(m.group(2))
    if day > total or day < 1 or total not in (3, 4):
        out.append(
            _finding(
                f"'day {day} of {total}'",
                "impossible war day — training is 3 days, battle is 4",
                "error",
            )
        )
        return
    # cross-check against the facts' own human label if present
    human = facts.get("war_day_human")
    if isinstance(human, str):
        hm = _DAY_OF_RE.search(human)
        if hm and (int(hm.group(1)), int(hm.group(2))) != (day, total):
            out.append(
                _finding(
                    f"copy says 'day {day} of {total}'",
                    f"the facts say '{human}'",
                    "warn",
                )
            )


def _check_colosseum(copy, facts, out):
    if not facts.get("is_colosseum_week"):
        return
    text = copy or ""
    if _FINISH_LINE_RE.search(text) and not _NO_FINISH_LINE_RE.search(text):
        out.append(
            _finding(
                "Colosseum finish line",
                "Colosseum has no finish line; standings continue through all four battle days",
                "error",
            )
        )
    for pattern in _BATTLES_DO_NOT_COUNT_RE:
        match = pattern.search(text)
        if match:
            prefix = text[max(0, match.start() - 16) : match.start()].lower()
            if re.search(r"(?:not|n't)\s+$", prefix):
                continue
            out.append(
                _finding(
                    match.group(0)[:120],
                    "every Colosseum battle continues to count toward clan and member standings",
                    "error",
                )
            )
            break
    # The boat is parked in Colosseum week — there is no boat, no boat battle and
    # nothing to defend or repair. This is the class that escaped on 2026-07-27:
    # the copy told the clan to top up boat defenses during Colosseum week.
    boat_match = _BOAT_IN_COLOSSEUM_RE.search(text)
    if boat_match:
        out.append(
            _finding(
                boat_match.group(0)[:120],
                "Colosseum week has no boat: no boat battles, no boat defenses, no repairs",
                "error",
            )
        )


def check_post(copy: str, facts: dict, conn=None) -> list[dict]:
    """Cross-check a post's CR claims against game knowledge. Returns a list of
    findings (empty = clean). Conservative: only clear contradictions."""
    if not isinstance(facts, dict):
        facts = {}
    out: list[dict] = []
    _check_card(copy, facts, conn, out)
    _check_arena(copy, facts, out)
    _check_league(copy, facts, out)
    _check_war_day(copy, facts, out)
    _check_colosseum(copy, facts, out)
    return out
