"""The one isolated timezone place (§7).

The v5 data layer is UTC-only: events store UTC `observed_at`, projections store
UTC instants. The single exception is *calendar-day rollups* (e.g.
clan_daily_metrics) whose key is an America/Chicago calendar day. That conversion
— UTC instant -> Chicago `YYYY-MM-DD` — happens ONLY here, only at projection /
read time, and never on an event.

Mirrors legacy `db.chicago_date_for_utc_timestamp` semantics so rebuilt
metric_date values bucket identically to the frozen legacy table. Legacy parsed
naive `%Y-%m-%dT%H:%M:%S` and treated it as UTC; the raw archive's `fetched_at`
carries a trailing `Z`, so we accept both forms (and any ISO 8601 offset),
normalizing to UTC before the Chicago conversion.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CHICAGO_TZ = ZoneInfo("America/Chicago")


def _parse_utc(value: str | None) -> datetime | None:
    """Parse a UTC instant from the formats we store/observe.

    Accepts: naive `2026-06-07T07:07:28` (legacy observed_at, assumed UTC),
    `2026-06-07T07:07:28Z` (raw_api_payloads.fetched_at), and any ISO 8601 with
    an explicit offset. Returns a tz-aware UTC datetime, or None.
    """
    if not value:
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def chicago_day_for_utc(value: str | None) -> str | None:
    """UTC instant -> America/Chicago calendar day (`YYYY-MM-DD`).

    The day-bucketing key for daily rollups. Equivalent to legacy
    `chicago_date_for_utc_timestamp`.
    """
    dt = _parse_utc(value)
    if dt is None:
        return None
    return dt.astimezone(CHICAGO_TZ).date().isoformat()
