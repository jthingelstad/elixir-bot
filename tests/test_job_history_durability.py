"""A job's history must survive its own next run after a restart.

This is the prerequisite for making scheduled jobs reliable by idempotency
rather than by retry. An idempotent, catch-up-able job asks "has this period
already run?" — and that question needs a durable answer.

It did not have one. `_JOB_STATUS` is empty after a restart, so the first
`mark_job_*` for a job built a fresh default state — `last_success_at = None`,
counters at zero — and then persisted it over the real row. History survived the
restart and was destroyed by the very next run.

Measured on 2026-08-09: `weekly_member_report` last succeeded 2026-07-27 and
made zero LLM calls on its scheduled 2026-08-03 Monday. Nothing anywhere
recorded that it had not run, because a scheduled fire time that passes while
the process is down is not "missed" by APScheduler — with a memory jobstore the
next fire time is recomputed from boot, so the skipped run never existed.
"""

from __future__ import annotations

from runtime import status as runtime_status


def _reset_process_memory():
    """Simulate a restart: in-memory state gone, the persisted row intact."""
    runtime_status._JOB_STATUS.clear()


def test_last_success_survives_the_next_run_after_a_restart(tmp_path):
    runtime_status.mark_job_start("weekly_member_report")
    runtime_status.mark_job_success("weekly_member_report", "sent 18 emails")
    original = runtime_status.snapshot()["jobs"]["weekly_member_report"]["last_success_at"]
    assert original is not None

    _reset_process_memory()
    runtime_status.mark_job_start("weekly_member_report")

    after = runtime_status.snapshot()["jobs"]["weekly_member_report"]
    assert after["last_success_at"] == original, (
        "the first run after a restart overwrote the persisted success time; "
        "without it there is no durable answer to 'has this period already run?'"
    )


def test_counters_continue_rather_than_reset(tmp_path):
    for _ in range(2):
        runtime_status.mark_job_start("weekly_recap")
        runtime_status.mark_job_success("weekly_recap")
    before = runtime_status.snapshot()["jobs"]["weekly_recap"]["run_count"]
    assert before == 2

    _reset_process_memory()
    runtime_status.mark_job_start("weekly_recap")

    after = runtime_status.snapshot()["jobs"]["weekly_recap"]["run_count"]
    assert after == before + 1, f"run_count reset instead of continuing ({after})"


def test_a_failure_after_a_restart_keeps_the_earlier_success(tmp_path, monkeypatch):
    """The case that matters for catch-up: a job that succeeded last week and
    fails this week must still show when it last worked."""
    from runtime import alerts

    # The Discord alert is not what this asserts, and it needs a live loop.
    monkeypatch.setattr(alerts, "schedule_job_failure_alert", lambda *a, **k: None)

    runtime_status.mark_job_start("memory_synthesis")
    runtime_status.mark_job_success("memory_synthesis")
    good = runtime_status.snapshot()["jobs"]["memory_synthesis"]["last_success_at"]

    _reset_process_memory()
    runtime_status.mark_job_start("memory_synthesis")
    runtime_status.mark_job_failure("memory_synthesis", "truncation")

    state = runtime_status.snapshot()["jobs"]["memory_synthesis"]
    assert state["last_success_at"] == good
    assert state["last_error"] == "truncation"
    assert state["failure_count"] >= 1


def test_a_row_from_an_older_build_cannot_break_the_counters(tmp_path):
    """Persisted rows predate keys added later; merging onto the default keeps a
    partial row from KeyError-ing the very job it describes."""
    import db

    db.save_runtime_job_status("legacy_job", {"success_count": 4})
    _reset_process_memory()

    runtime_status.mark_job_start("legacy_job")
    state = runtime_status.snapshot()["jobs"]["legacy_job"]
    assert state["success_count"] == 4
    assert state["run_count"] == 1
    assert state["failure_count"] == 0
