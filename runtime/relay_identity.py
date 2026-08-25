"""Stable identities for member-visible relay topics."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_WAR_WEEK_SIGNAL_RE = re.compile(r"^(?:race_finished|week_finished):(\d+):(\d+)$")
_WAR_DAY_SIGNAL_RE = re.compile(r"^war_day_opened:(\d+):(\d+):\d+$")
_WAR_SEASON_BOUNDARY_RE = re.compile(r"^(?:season_closed|clan_league_changed):(\d+)(?::.*)?$")


def war_week_relay_key(signal_keys: Iterable[object]) -> str | None:
    """Collapse the early-finish and final-close signals for one war week.

    ``race_finished`` can arrive before ``week_finished``, but both describe the
    same completed Clash Royale week.  A relay identity must follow that domain
    moment rather than the wording or producer that happened to narrate it.
    """
    weeks = set()
    day_context = set()
    season_context = set()
    for value in signal_keys:
        key = str(value or "").strip()
        match = _WAR_WEEK_SIGNAL_RE.fullmatch(key)
        if match:
            weeks.add((int(match.group(1)), int(match.group(2))))
            continue
        match = _WAR_DAY_SIGNAL_RE.fullmatch(key)
        if match:
            day_context.add((int(match.group(1)), int(match.group(2))))
            continue
        match = _WAR_SEASON_BOUNDARY_RE.fullmatch(key)
        if match:
            season_context.add(int(match.group(1)))
            continue
        if key:
            return None
    if len(weeks) != 1:
        return None
    season_id, section_index = next(iter(weeks))
    if day_context and day_context != {(season_id, section_index)}:
        return None
    if season_context and season_context != {season_id}:
        return None
    return f"war-week:{season_id}:{section_index}"


def awareness_relay_identity(key_material: str) -> tuple[str, str]:
    """Return the objective and action key used by awareness relay cards."""
    key_hash = hashlib.sha1(str(key_material).encode("utf-8")).hexdigest()[:12]
    return f"awareness_relay:{key_hash}", f"awareness-relay:{key_hash}"
