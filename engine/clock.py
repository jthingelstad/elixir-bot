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

FAME_FINISH_LINE = 10_000
FAME_FINISH_LINE_COLOSSEUM = 5_000
PERIODS_PER_SECTION = 7
TRAINING_DAYS = 3
WAR_DAYS = 4
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
    finish_line: int
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


def war_clock(race_payload: dict, now: datetime, season_id: int | None = None) -> WarClock:
    """Compute the clock from a currentriverrace payload. Pure — no DB.

    `season_id` comes from infer_season_id (the payload almost never carries it).
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
    finish_line = FAME_FINISH_LINE_COLOSSEUM if is_colosseum else FAME_FINISH_LINE
    race_finished = phase != "training" and our_fame >= finish_line

    if phase == "training":
        battle_days_remaining = WAR_DAYS
    elif war_day_index is None:
        battle_days_remaining = WAR_DAYS
    else:
        battle_days_remaining = max(0, WAR_DAYS - 1 - war_day_index)

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
    """Parse CR compact battle time ('20260703T211500.000Z')."""
    if not value:
        return None
    raw = str(value).replace(".000Z", "").replace("Z", "")
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None


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
