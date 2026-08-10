"""Period-level idempotency for cron slots hidden by process downtime."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import db
from runtime import activities, scheduled_catchup
from runtime import status as runtime_status

CHICAGO = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def reset_job_state(monkeypatch):
    runtime_status.flush_status_writes()
    with runtime_status._LOCK:
        runtime_status._JOB_STATUS.clear()
    scheduled_catchup._LOCKS_BY_LOOP.clear()
    from runtime import alerts

    monkeypatch.setattr(alerts, "schedule_job_failure_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(alerts, "clear_job_failure_alert", lambda *args, **kwargs: None)
    yield
    runtime_status.flush_status_writes()
    with runtime_status._LOCK:
        runtime_status._JOB_STATUS.clear()
    scheduled_catchup._LOCKS_BY_LOOP.clear()


def _resolved(
    job_callable,
    *,
    period="weekly",
    status_name="weekly_member_report",
    same_period_only=False,
):
    schedule = {"hour": 10, "minute": 0}
    if period == "weekly":
        schedule["day_of_week"] = "mon"
    return {
        "activity_key": "weekly-member-report",
        "job_callable": job_callable,
        "job_id": "weekly-member-report",
        "status_name": status_name,
        "catch_up_period": period,
        "catch_up_same_period_only": same_period_only,
        "schedule_kind": "cron",
        "schedule_config": schedule,
    }


def _install_one_activity(monkeypatch, resolved):
    definition = SimpleNamespace(
        activity_key=resolved["activity_key"],
        enabled_by_default=True,
        catch_up_period=resolved["catch_up_period"],
    )
    monkeypatch.setattr(activities, "list_registered_activities", lambda: [definition])
    monkeypatch.setattr(activities, "resolve_activity", lambda key, module: resolved)


def test_registry_opts_in_only_jobs_that_are_safe_to_run_late():
    registered = {item.activity_key: item for item in activities.list_registered_activities()}

    assert registered["weekly-member-report"].catch_up_period == "weekly"
    assert registered["weekly-member-report"].catch_up_same_period_only is True
    assert registered["action-outcome-refresh"].catch_up_period == "daily"
    assert registered["db-backup"].catch_up_period == "daily"
    # Discord communicators have no period-keyed delivery outbox. A crash after
    # posting but before job success would make a generic retry duplicate them.
    assert registered["weekly-leadership-review"].catch_up_period is None
    assert registered["weekly-recap"].catch_up_period is None
    assert registered["weekly-elder-standing"].catch_up_period is None
    assert registered["promotion-content"].catch_up_period is None
    assert registered["db-maintenance"].catch_up_period is None
    # These jobs have timing-sensitive inputs or own a stronger event/season
    # recovery contract; generic wall-clock catch-up would be wrong.
    assert registered["war-attendance-snapshot"].catch_up_period is None
    assert registered["memory-synthesis"].catch_up_period is None
    assert registered["clan-wars-intel"].catch_up_period is None
    assert registered["awareness-loop"].catch_up_period is None


def test_latest_weekly_slot_and_period_cross_at_the_scheduled_minute():
    resolved = _resolved(lambda: None)

    before = scheduled_catchup.latest_elapsed_fire_time(
        resolved, now=datetime(2026, 8, 10, 9, 59, tzinfo=CHICAGO)
    )
    at_slot = scheduled_catchup.latest_elapsed_fire_time(
        resolved, now=datetime(2026, 8, 10, 10, 0, tzinfo=CHICAGO)
    )

    assert before == datetime(2026, 8, 3, 10, 0, tzinfo=CHICAGO)
    assert scheduled_catchup.period_key("weekly", before) == "2026-W32"
    assert at_slot == datetime(2026, 8, 10, 10, 0, tzinfo=CHICAGO)
    assert scheduled_catchup.period_key("weekly", at_slot) == "2026-W33"


def test_latest_daily_slot_uses_the_local_calendar_day():
    resolved = _resolved(lambda: None, period="daily", status_name="card_catalog_sync")
    fired = scheduled_catchup.latest_elapsed_fire_time(
        resolved, now=datetime(2026, 8, 9, 16, 0, tzinfo=CHICAGO)
    )

    assert fired == datetime(2026, 8, 9, 10, 0, tzinfo=CHICAGO)
    assert scheduled_catchup.period_key("daily", fired) == "2026-08-09"


def test_normal_scheduled_success_persists_its_period_across_restart(monkeypatch):
    async def job():
        runtime_status.mark_job_start("weekly_member_report")
        runtime_status.mark_job_success("weekly_member_report", "sent")

    fired = datetime(2026, 8, 3, 10, 0, tzinfo=CHICAGO)
    monkeypatch.setattr(scheduled_catchup, "latest_elapsed_fire_time", lambda resolved: fired)
    asyncio.run(scheduled_catchup.wrap_scheduled_activity(_resolved(job))())
    runtime_status.flush_status_writes()

    with runtime_status._LOCK:
        runtime_status._JOB_STATUS.clear()
    assert runtime_status.job_state("weekly_member_report")["last_success_period"] == "2026-W32"


def test_rollout_recognizes_legacy_success_timestamp_in_the_due_period(monkeypatch):
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    resolved = _resolved(job)
    _install_one_activity(monkeypatch, resolved)
    db.save_runtime_job_status(
        "weekly_member_report",
        {"last_success_at": "2026-08-03T15:11:45", "running": False},
    )

    result = asyncio.run(
        scheduled_catchup.run_catch_up_sweep(
            object(), now=datetime(2026, 8, 9, 16, 0, tzinfo=CHICAGO)
        )
    )

    assert calls == 0
    assert result == [
        {"activity": "weekly-member-report", "period": "2026-W32", "outcome": "current"}
    ]


def test_missing_period_runs_once_and_a_restart_sees_the_success(monkeypatch):
    calls = 0

    async def job():
        nonlocal calls
        calls += 1
        runtime_status.mark_job_start("weekly_member_report")
        runtime_status.mark_job_success("weekly_member_report", "sent")

    resolved = _resolved(job)
    _install_one_activity(monkeypatch, resolved)
    now = datetime(2026, 8, 9, 16, 0, tzinfo=CHICAGO)

    async def exercise():
        first = await scheduled_catchup.run_catch_up_sweep(object(), now=now)
        runtime_status.flush_status_writes()
        with runtime_status._LOCK:
            runtime_status._JOB_STATUS.clear()
        second = await scheduled_catchup.run_catch_up_sweep(object(), now=now)
        return first, second

    first, second = asyncio.run(exercise())

    assert calls == 1
    assert first[0]["outcome"] == "succeeded"
    assert second[0]["outcome"] == "current"


def test_explicitly_skipped_period_is_durable_and_never_runs(monkeypatch):
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    resolved = _resolved(job)
    _install_one_activity(monkeypatch, resolved)
    runtime_status.mark_job_period_skipped(
        "weekly_member_report", "2026-W32", "owner chose to wait for the next slot"
    )
    runtime_status.flush_status_writes()
    with runtime_status._LOCK:
        runtime_status._JOB_STATUS.clear()

    result = asyncio.run(
        scheduled_catchup.run_catch_up_sweep(
            object(), now=datetime(2026, 8, 9, 16, 0, tzinfo=CHICAGO)
        )
    )

    assert calls == 0
    assert result == [
        {"activity": "weekly-member-report", "period": "2026-W32", "outcome": "skipped"}
    ]
    state = runtime_status.job_state("weekly_member_report")
    assert state["last_success_at"] is None
    assert state["last_skipped_period"] == "2026-W32"


def test_live_data_job_supersedes_old_period_instead_of_running_new_week_early(monkeypatch):
    calls = 0

    async def job():
        nonlocal calls
        calls += 1

    resolved = _resolved(job, same_period_only=True)
    _install_one_activity(monkeypatch, resolved)

    result = asyncio.run(
        scheduled_catchup.run_catch_up_sweep(
            object(), now=datetime(2026, 8, 10, 9, 59, tzinfo=CHICAGO)
        )
    )
    runtime_status.flush_status_writes()

    assert calls == 0
    assert result == [
        {"activity": "weekly-member-report", "period": "2026-W32", "outcome": "superseded"}
    ]
    state = runtime_status.job_state("weekly_member_report")
    assert state["last_skipped_period"] == "2026-W32"
    assert state["last_skipped_reason"] == "superseded by 2026-W33 before catch-up"


def test_failed_catch_up_backs_off_before_retrying(monkeypatch):
    calls = 0

    async def job():
        nonlocal calls
        calls += 1
        runtime_status.mark_job_start("weekly_member_report")
        runtime_status.mark_job_failure("weekly_member_report", "composer failed")

    resolved = _resolved(job)
    _install_one_activity(monkeypatch, resolved)
    now = datetime(2026, 8, 9, 16, 0, tzinfo=CHICAGO)

    async def exercise():
        first = await scheduled_catchup.run_catch_up_sweep(object(), now=now)
        runtime_status.flush_status_writes()
        with runtime_status._LOCK:
            runtime_status._JOB_STATUS.clear()
        second = await scheduled_catchup.run_catch_up_sweep(object(), now=now.replace(hour=17))
        third = await scheduled_catchup.run_catch_up_sweep(object(), now=now.replace(hour=22))
        return first, second, third

    first, second, third = asyncio.run(exercise())

    assert calls == 2
    assert first[0]["outcome"] == "failed"
    assert second[0]["outcome"] == "cooldown"
    assert third[0]["outcome"] == "failed"


def test_concurrent_sweeps_cannot_run_the_same_period_twice(monkeypatch):
    calls = 0
    entered = None
    release = None

    async def job():
        nonlocal calls
        calls += 1
        runtime_status.mark_job_start("weekly_member_report")
        entered.set()
        await release.wait()
        runtime_status.mark_job_success("weekly_member_report", "sent")

    resolved = _resolved(job)
    _install_one_activity(monkeypatch, resolved)
    now = datetime(2026, 8, 9, 16, 0, tzinfo=CHICAGO)

    async def exercise():
        nonlocal entered, release
        entered = asyncio.Event()
        release = asyncio.Event()
        first = asyncio.create_task(scheduled_catchup.run_catch_up_sweep(object(), now=now))
        await entered.wait()
        second = asyncio.create_task(scheduled_catchup.run_catch_up_sweep(object(), now=now))
        release.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(exercise())

    assert calls == 1
    assert first[0]["outcome"] == "succeeded"
    assert second[0]["outcome"] == "current"
