"""Durable catch-up for elapsed cron periods.

APScheduler's in-memory job store cannot report a slot that passed while the
process was down: on boot the trigger simply computes the next future fire
time. This module supplies the missing job-level reliability contract for
activities that explicitly opt in through ``runtime.activities``.

The unit of idempotency is a schedule period, not an LLM call. Weekly jobs use
``YYYY-Www`` and daily jobs use ``YYYY-MM-DD``. A successful scheduled run
records that key in ``runtime_job_status``; a sweep at startup and hourly runs
the latest elapsed period when no success exists. Failed catch-ups retry after
six hours, so one broken job cannot loop on every hourly sweep.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from runtime import status as runtime_status

log = logging.getLogger("elixir")

CHICAGO = ZoneInfo("America/Chicago")
CATCH_UP_RETRY = timedelta(hours=6)
_CRON_TRIGGER_FIELDS = {
    "year",
    "month",
    "day",
    "week",
    "day_of_week",
    "hour",
    "minute",
    "second",
    "start_date",
    "end_date",
    "jitter",
}
_LOCKS_BY_LOOP: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _activity_lock(activity_key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _LOCKS_BY_LOOP.setdefault(loop, {})
    return locks.setdefault(activity_key, asyncio.Lock())


def _schedule_timezone(schedule_config: dict[str, Any]) -> ZoneInfo:
    value = schedule_config.get("timezone") or "America/Chicago"
    if isinstance(value, str):
        return ZoneInfo(value)
    key = getattr(value, "key", None) or getattr(value, "zone", None)
    return ZoneInfo(key or "America/Chicago")


def latest_elapsed_fire_time(
    resolved: dict[str, Any], *, now: datetime | None = None
) -> datetime | None:
    """Return the latest cron fire time at or before ``now``."""
    period_kind = resolved.get("catch_up_period")
    if period_kind not in {"daily", "weekly"} or resolved.get("schedule_kind") != "cron":
        return None

    schedule_config = resolved["schedule_config"]
    tz = _schedule_timezone(schedule_config)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)

    trigger_kwargs = {
        key: value for key, value in schedule_config.items() if key in _CRON_TRIGGER_FIELDS
    }
    trigger_kwargs["timezone"] = tz
    trigger = CronTrigger(**trigger_kwargs)
    lookback = timedelta(days=8 if period_kind == "weekly" else 2)
    cursor = current - lookback
    previous = None
    latest = None
    for _ in range(32):
        candidate = trigger.get_next_fire_time(previous, cursor)
        if candidate is None or candidate > current:
            break
        latest = candidate
        previous = candidate
        cursor = candidate
    return latest


def period_key(period_kind: str, when: datetime) -> str:
    local = when.astimezone(CHICAGO) if when.tzinfo else when.replace(tzinfo=CHICAGO)
    if period_kind == "weekly":
        iso_year, iso_week, _ = local.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if period_kind == "daily":
        return local.date().isoformat()
    raise ValueError(f"unsupported catch-up period: {period_kind}")


def _legacy_success_period(status: dict[str, Any], period_kind: str) -> str | None:
    """Map a pre-period-receipt success timestamp into its schedule period."""
    raw = status.get("last_success_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return period_key(period_kind, parsed)


def _period_resolution(status: dict[str, Any], period_kind: str, key: str) -> str | None:
    if status.get("last_success_period") == key or (
        not status.get("last_success_period") and _legacy_success_period(status, period_kind) == key
    ):
        return "current"
    if status.get("last_skipped_period") == key:
        return "skipped"
    return None


def wrap_scheduled_activity(resolved: dict[str, Any]):
    """Attach the due period to a normal APScheduler invocation."""
    job_callable = resolved["job_callable"]
    if not resolved.get("catch_up_period"):
        return job_callable

    async def run_scheduled_period():
        async with _activity_lock(resolved["activity_key"]):
            fired_at = latest_elapsed_fire_time(resolved)
            if fired_at is None:
                return await job_callable()
            key = period_key(resolved["catch_up_period"], fired_at)
            with runtime_status.job_period(key):
                result = await job_callable()
            await asyncio.to_thread(runtime_status.flush_status_writes)
            state = runtime_status.job_state(resolved["status_name"])
            if state.get("last_success_period") == key:
                await asyncio.to_thread(
                    runtime_status.persist_job_state_strict, resolved["status_name"]
                )
            return result

    return run_scheduled_period


async def run_catch_up_sweep(runtime_module: Any, *, now: datetime | None = None) -> list[dict]:
    """Run each owed activity once and return compact operator evidence."""
    from runtime.activities import list_registered_activities, resolve_activity

    results: list[dict] = []
    current = now or datetime.now(CHICAGO)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CHICAGO)
    attempted_at = (
        current.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    )
    for activity in list_registered_activities():
        if not activity.enabled_by_default or not activity.catch_up_period:
            continue
        resolved = resolve_activity(activity.activity_key, runtime_module)
        fired_at = latest_elapsed_fire_time(resolved, now=current)
        if fired_at is None:
            continue
        key = period_key(resolved["catch_up_period"], fired_at)
        status_name = resolved["status_name"]

        async with _activity_lock(resolved["activity_key"]):
            status = runtime_status.job_state(status_name)
            resolution = _period_resolution(status, resolved["catch_up_period"], key)
            if resolution:
                results.append(
                    {"activity": resolved["activity_key"], "period": key, "outcome": resolution}
                )
                continue
            current_key = period_key(resolved["catch_up_period"], current)
            if resolved.get("catch_up_same_period_only") and current_key != key:
                reason = f"superseded by {current_key} before catch-up"
                await asyncio.to_thread(
                    runtime_status.mark_job_period_skipped, status_name, key, reason
                )
                log.warning(
                    "scheduled catch-up skipped activity=%s period=%s reason=%s",
                    resolved["activity_key"],
                    key,
                    reason,
                )
                results.append(
                    {"activity": resolved["activity_key"], "period": key, "outcome": "superseded"}
                )
                continue
            claim = runtime_status.claim_catch_up_period(
                status_name,
                key,
                attempted_at=attempted_at,
                retry_after=CATCH_UP_RETRY,
            )
            if claim != "claimed":
                results.append(
                    {"activity": resolved["activity_key"], "period": key, "outcome": claim}
                )
                continue

            # Persist before executing member-facing or destructive work. A
            # crash must not turn the next hourly sweep into a blind duplicate.
            await asyncio.to_thread(runtime_status.flush_status_writes)
            await asyncio.to_thread(runtime_status.persist_job_state_strict, status_name)
            log.warning(
                "scheduled catch-up running activity=%s period=%s scheduled_at=%s",
                resolved["activity_key"],
                key,
                fired_at.isoformat(),
            )
            try:
                with runtime_status.job_period(key, catch_up=True):
                    await resolved["job_callable"]()
            except Exception as exc:  # noqa: BLE001 - one job must not stop the sweep
                log.exception(
                    "scheduled catch-up raised activity=%s period=%s",
                    resolved["activity_key"],
                    key,
                )
                runtime_status.mark_job_failure(status_name, f"catch-up raised: {exc}")

            await asyncio.to_thread(runtime_status.flush_status_writes)
            await asyncio.to_thread(runtime_status.persist_job_state_strict, status_name)
            after = runtime_status.job_state(status_name)
            outcome = "succeeded" if after.get("last_success_period") == key else "failed"
            results.append(
                {"activity": resolved["activity_key"], "period": key, "outcome": outcome}
            )
    return results


__all__ = [
    "latest_elapsed_fire_time",
    "period_key",
    "run_catch_up_sweep",
    "wrap_scheduled_activity",
]
