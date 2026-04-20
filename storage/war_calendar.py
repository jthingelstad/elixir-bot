from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

PERIODS_PER_WEEK = 7
FIRST_BATTLE_PERIOD_OFFSET = 3
FINAL_BATTLE_PERIOD_OFFSET = 6
FINAL_PRACTICE_PERIOD_OFFSET = FIRST_BATTLE_PERIOD_OFFSET - 1
WAR_RESET_HOUR_UTC = 10
PRACTICE_PERIOD_TYPES = {"training", "trainingday", "practice"}
BATTLE_PERIOD_TYPES = {"warday", "battle", "battleday", "colosseum"}


def _parse_utc_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_cr_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        clean = value.split(".")[0]
        return datetime.strptime(clean, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def coerce_utc_datetime(value: datetime | str | None) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = _parse_utc_iso(value) or _parse_cr_utc(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    current = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return current.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def normalize_period_type(period_type: Optional[str]) -> Optional[str]:
    if period_type is None:
        return None
    return str(period_type).strip().lower()


def period_offset(period_index: Optional[int]) -> Optional[int]:
    if period_index is None:
        return None
    return period_index % PERIODS_PER_WEEK


def resolve_phase(period_type: Optional[str], period_index: Optional[int]) -> Optional[str]:
    normalized = normalize_period_type(period_type)
    if normalized in BATTLE_PERIOD_TYPES:
        return "battle"
    if normalized in PRACTICE_PERIOD_TYPES:
        return "practice"
    offset = period_offset(period_index)
    if offset is None:
        return None
    if FIRST_BATTLE_PERIOD_OFFSET <= offset <= FINAL_BATTLE_PERIOD_OFFSET:
        return "battle"
    return "practice"


def phase_day_number(phase: Optional[str], period_index: Optional[int]) -> Optional[int]:
    offset = period_offset(period_index)
    if offset is None or phase not in {"battle", "practice"}:
        return None
    if phase == "battle":
        return offset - FIRST_BATTLE_PERIOD_OFFSET + 1
    return offset + 1


def war_week_day(period_index: Optional[int]) -> Optional[int]:
    """1-indexed day number within the war week (1-7), combining practice and
    battle days. Days 1-3 are practice; days 4-7 are battle. Returns None if
    period_index is missing or out of range.
    """
    offset = period_offset(period_index)
    if offset is None:
        return None
    return offset + 1


def is_colosseum_week(period_type: Optional[str] = None) -> bool:
    """True when the current war week is the colosseum (final) week.

    The last week of every River Race season is colosseum week, regardless of
    whether the season is 4 or 5 weeks. The API sends periodType "colosseum"
    on battle days; practice days still show "training", so this can only be
    detected once battle days begin.
    """
    return normalize_period_type(period_type) == "colosseum"


def war_day_key(
    season_id: Optional[int],
    section_index: Optional[int],
    period_index: Optional[int],
    observed_at: Optional[str] = None,
) -> Optional[str]:
    if section_index is None or period_index is None:
        return observed_at[:10] if observed_at else None
    season_token = f"s{season_id:05d}" if season_id is not None else "slive"
    return f"{season_token}-w{section_index:02d}-p{period_index:03d}"


def war_reset_window_utc(value: datetime | str | None) -> tuple[Optional[datetime], Optional[datetime]]:
    current = coerce_utc_datetime(value)
    if current is None:
        return None, None
    reset_start = current.replace(
        hour=WAR_RESET_HOUR_UTC,
        minute=0,
        second=0,
        microsecond=0,
    )
    if current < reset_start:
        reset_start -= timedelta(days=1)
    return reset_start, reset_start + timedelta(days=1)


def war_signal_date(value: datetime | str | None) -> Optional[str]:
    reset_start, _ = war_reset_window_utc(value)
    return reset_start.date().isoformat() if reset_start else None
