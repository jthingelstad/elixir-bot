"""The war clock — architecture.md §16.2.

A pure, code-computed view of "what moment is it in the war", derived from the
live `currentriverrace` baseline. The war recognizer and composer READ this;
they never infer war time themselves.

Facts grounded in the archive's payloads (2026-07):
- `periodIndex // 7 == sectionIndex`; `periodIndex % 7` is the day within the
  section (0–2 training, 3–6 war/colosseum days).
- `periodType` is exactly 'training' | 'warDay' | 'colosseum'.
- `seasonId` was absent in ALL 259 captured payloads — the season is always
  inferred (port of event_core/ingest/war.py infer_live_season_id).
- Finish lines are game constants, not payload fields (§16.4): 10,000 fame in
  normal weeks, 5,000 in Colosseum.
- CR war days roll at ~10:00 UTC (observed finishTime ≈ 09:37Z); the clock uses
  10:00 UTC as the period boundary. Approximation, documented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from engine.normalize import (  # single source for war-week structure
    PERIODS_PER_SECTION,
    TRAINING_DAYS,
    WAR_DAYS,
    parse_cr_time,
)

FAME_FINISH_LINE = 10_000
# Colosseum has NO finish line — fame accrues across all four battle days
# (verified live 2026-07-03: 20,600 fame on Colosseum day 2, race still on).
# The spec's 5,000 constant (§16.4) was wrong; feedback.md New-1 had flagged
# it as unverified. race_finished can therefore only be true in normal weeks.
PERIOD_BOUNDARY_HOUR_UTC = 10

_PHASE = {"training": "training", "warDay": "war_day", "colosseum": "colosseum"}


@dataclass(frozen=True)
class WarClock:
    phase: str  # 'training' | 'war_day' | 'colosseum'
    season_id: int | None
    section_index: int | None
    period_index: int | None
    day_index: int | None  # 0-6 within the section (0-2 training, 3-6 battle)
    war_day_index: int | None  # 0-3 on battle days, None during training
    is_colosseum_week: bool
    battle_days_remaining: int
    hours_left_in_period: float
    our_fame: int
    finish_line: int | None  # None during Colosseum (no line)
    race_finished: bool
    pace_status: str  # 'training' | 'ahead' | 'behind' | 'won'


def infer_season_id(conn, race_payload: dict) -> int | None:
    """Port of infer_live_season_id: live seasonId if present, else the latest
    logged (season, section) from war_weeks — live section < logged section
    means the season rolled over."""
    live = (race_payload or {}).get("seasonId")
    if live is not None:
        return int(live)
    row = conn.execute(
        "SELECT season_id, section_index FROM war_weeks ORDER BY season_id DESC, section_index DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT season_id, NULL FROM war_seasons ORDER BY season_id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None
    logged_season, logged_section = int(row[0]), row[1]
    live_section = (race_payload or {}).get("sectionIndex")
    if (
        live_section is not None
        and logged_section is not None
        and int(live_section) < int(logged_section)
    ):
        return logged_season + 1
    return logged_season


def war_clock(
    race_payload: dict,
    now: datetime,
    season_id: int | None = None,
    period_anchor: datetime | None = None,
) -> WarClock:
    """Compute the clock from a currentriverrace payload. Pure — no DB.

    `season_id` comes from infer_season_id (the payload almost never carries it).
    `period_anchor` is when the current period was first observed (see
    period_anchor_from_events) — the drift-adaptive day boundary.
    """
    payload = race_payload or {}
    period_type = payload.get("periodType") or ""
    phase = _PHASE.get(period_type, "training")
    period_index = payload.get("periodIndex")
    section_index = payload.get("sectionIndex")
    day_index = period_index % PERIODS_PER_SECTION if isinstance(period_index, int) else None
    war_day_index = (
        day_index - TRAINING_DAYS
        if isinstance(day_index, int) and day_index >= TRAINING_DAYS
        else None
    )
    is_colosseum = phase == "colosseum"

    clan = payload.get("clan") or {}
    our_fame = int(clan.get("fame") or 0)
    finish_line = None if is_colosseum else FAME_FINISH_LINE
    race_finished = (
        not is_colosseum and phase != "training" and our_fame >= FAME_FINISH_LINE
    )

    if phase == "training":
        battle_days_remaining = WAR_DAYS
    elif war_day_index is None:
        battle_days_remaining = WAR_DAYS
    else:
        battle_days_remaining = max(0, WAR_DAYS - 1 - war_day_index)

    # Carried learning (pre-v5.1 heartbeat, issue #20): CR seasons skew off
    # the nominal 10:00Z reset, and the skew drifts season to season — the
    # observed period-start beats any hardcoded hour. When the caller supplies
    # the period anchor (the war_day_opened event's observed_at — the first
    # tick that saw this period), the day ends anchor+24h; the fixed hour is
    # only the no-anchor fallback.
    if period_anchor is not None:
        anchor = period_anchor.astimezone(timezone.utc)
        boundary = anchor + timedelta(days=1)
        if boundary <= now.astimezone(timezone.utc):
            # anchor is stale (we're past its day, e.g. missed polls);
            # fall back to the nominal boundary rather than negative time
            boundary = None
    else:
        boundary = None
    if boundary is None:
        boundary = now.astimezone(timezone.utc).replace(
            hour=PERIOD_BOUNDARY_HOUR_UTC, minute=0, second=0, microsecond=0
        )
        if boundary <= now.astimezone(timezone.utc):
            boundary += timedelta(days=1)
    hours_left = (boundary - now.astimezone(timezone.utc)).total_seconds() / 3600.0

    if phase == "training":
        pace = "training"
    elif race_finished:
        pace = "won"
    elif is_colosseum:
        # No finish line to pace against; urgency runs all four days and the
        # composer leans on standings, not a line.
        pace = "colosseum"
    else:
        # Linear pace across the 4 battle days: after day k of 4, expect
        # (k+1)/4 of the finish line.
        elapsed = (war_day_index or 0) + 1
        expected = finish_line * elapsed / WAR_DAYS
        pace = "ahead" if our_fame >= expected else "behind"

    return WarClock(
        phase=phase,
        season_id=season_id,
        section_index=section_index,
        period_index=period_index,
        day_index=day_index,
        war_day_index=war_day_index,
        is_colosseum_week=is_colosseum,
        battle_days_remaining=battle_days_remaining,
        hours_left_in_period=round(hours_left, 2),
        our_fame=our_fame,
        finish_line=finish_line,
        race_finished=race_finished,
        pace_status=pace,
    )


def _effective_war_date(dt: datetime) -> datetime:
    """CR war periods roll at ~10:00 UTC — shift so a period maps to one date."""
    return dt.astimezone(timezone.utc) - timedelta(hours=PERIOD_BOUNDARY_HOUR_UTC)


def parse_battle_time(value: str) -> datetime | None:
    """Parse CR compact battle time — delegates to the normalizer's single
    parser (engine/normalize.py); name kept for its importers."""
    return parse_cr_time(value)


def resolve_war_keys(
    battle_time: str,
    clock: WarClock | None,
    now: datetime,
    war_weeks_rows=None,
) -> tuple[int | None, int | None, int | None]:
    """Resolve (season_id, section_index, war_day_index) from the battle's OWN
    time (runtime.md §2 step 2): the battlelog returns battles hours-to-days
    old, and a late-mirrored battle from a previous war day must land in that
    day — never stamped with the tick-time clock.

    Method: each period is one war-date (10:00 UTC boundary); walk periodIndex
    back by the battle's age in war-dates. Battles older than the current
    season's period 0 return (None, None, None) — `war_weeks_rows` is accepted
    for a future historical lookup but the arithmetic covers the battlelog's
    ~30-battle window in practice.
    """
    if clock is None or not isinstance(clock.period_index, int):
        return (None, None, None)
    bt = parse_battle_time(battle_time)
    if bt is None:
        return (None, None, None)
    days_back = (_effective_war_date(now).date() - _effective_war_date(bt).date()).days
    if days_back < 0:
        days_back = 0
    period = clock.period_index - days_back
    if period < 0:
        return (None, None, None)  # previous season — out of arithmetic range
    section = period // PERIODS_PER_SECTION
    day = period % PERIODS_PER_SECTION
    war_day = day - TRAINING_DAYS if day >= TRAINING_DAYS else None
    return (clock.season_id, section, war_day)


def period_anchor_from_events(conn, season_id, section_index, day_index) -> datetime | None:
    """When the current period was first observed — the war_day_opened event's
    observed_at (carried learning: CR's reset hour skews per season; the
    observed period-start is the accurate 24h anchor). None when the event
    doesn't exist (e.g. training days, or the engine started mid-period)."""
    if season_id is None or section_index is None or day_index is None:
        return None
    row = conn.execute(
        "SELECT observed_at FROM war_events WHERE dedup_key = ?",
        (f"war_day_opened:{season_id}:{section_index}:{day_index}",),
    ).fetchone()
    return parse_battle_time(str(row[0])) if row else None
