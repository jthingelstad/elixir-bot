import asyncio
import threading
import time

import pytest

import db
from runtime import status as runtime_status


def _reset_runtime_jobs():
    with runtime_status._LOCK:
        runtime_status._JOB_STATUS.clear()


@pytest.fixture(autouse=True)
def reset_runtime_jobs():
    _reset_runtime_jobs()
    yield
    _reset_runtime_jobs()


def test_clear_stale_running_jobs_marks_previous_process_job_failed(monkeypatch):
    saved = []
    persisted = {
        "site_data_refresh": {
            "run_count": 1,
            "success_count": 0,
            "failure_count": 0,
            "last_started_at": "2026-06-19T14:02:22",
            "last_finished_at": None,
            "last_failure_at": None,
            "last_error": None,
            "running": True,
        },
        "war_poll": {
            "run_count": 3,
            "success_count": 3,
            "failure_count": 0,
            "running": False,
        },
    }

    monkeypatch.setattr(db, "list_runtime_job_status", lambda: persisted)
    monkeypatch.setattr(
        db,
        "save_runtime_job_status",
        lambda name, state: saved.append((name, dict(state))),
    )
    monkeypatch.setattr(runtime_status, "_utcnow", lambda: "2026-06-20T18:00:00")

    assert runtime_status.clear_stale_running_jobs() == ["site_data_refresh"]
    assert saved == [
        (
            "site_data_refresh",
            {
                "run_count": 1,
                "success_count": 0,
                "failure_count": 1,
                "last_started_at": "2026-06-19T14:02:22",
                "last_finished_at": "2026-06-20T18:00:00",
                "last_failure_at": "2026-06-20T18:00:00",
                "last_error": "process restarted before job marked complete",
                "running": False,
            },
        )
    ]


def test_clear_stale_running_jobs_keeps_current_process_job_running(monkeypatch):
    saved = []
    with runtime_status._LOCK:
        runtime_status._JOB_STATUS["site_data_refresh"] = {"running": True}
    persisted = {
        "site_data_refresh": {
            "run_count": 1,
            "failure_count": 0,
            "running": True,
        }
    }

    monkeypatch.setattr(db, "list_runtime_job_status", lambda: persisted)
    monkeypatch.setattr(
        db,
        "save_runtime_job_status",
        lambda name, state: saved.append((name, dict(state))),
    )

    assert runtime_status.clear_stale_running_jobs() == []
    assert saved == []


def test_async_job_status_persistence_never_blocks_event_loop(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_save(name, state):
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(db, "save_runtime_job_status", slow_save)

    async def exercise():
        before = time.monotonic()
        runtime_status.mark_job_start("nonblocking")
        elapsed = time.monotonic() - before
        assert elapsed < 0.1
        assert await asyncio.to_thread(started.wait, 1)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        runtime_status.flush_status_writes()
