"""Runtime telemetry for Elixir health/status reporting."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

log = logging.getLogger("elixir")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


_LOCK = threading.Lock()
_STATUS_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="elixir-status")
_STATUS_FUTURES = set()
_STATUS_FUTURES_LOCK = threading.Lock()
_JOB_PERIOD: ContextVar[dict | None] = ContextVar("elixir_job_period", default=None)
STARTED_AT = _utcnow()

_JOB_STATUS = {}
_API_STATUS = {
    "call_count": 0,
    "success_count": 0,
    "error_count": 0,
    "consecutive_error_count": 0,
    "last_call_at": None,
    "last_endpoint": None,
    "last_entity_key": None,
    "last_ok": None,
    "last_status_code": None,
    "last_error": None,
    "last_duration_ms": None,
    "by_endpoint": {},
}
_LLM_STATUS = {
    "call_count": 0,
    "success_count": 0,
    "error_count": 0,
    "consecutive_error_count": 0,
    "last_call_at": None,
    "last_workflow": None,
    "last_model": None,
    "last_ok": None,
    "last_error": None,
    "last_duration_ms": None,
    "last_prompt_tokens": None,
    "last_completion_tokens": None,
    "last_total_tokens": None,
    "last_cache_creation_tokens": None,
    "last_cache_read_tokens": None,
    "by_workflow": {},
}


def _default_job_state() -> dict:
    return {
        "run_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "last_started_at": None,
        "last_finished_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_error": None,
        "last_summary": None,
        "last_started_period": None,
        "last_success_period": None,
        "last_skipped_period": None,
        "last_skipped_at": None,
        "last_skipped_reason": None,
        "last_catch_up_period": None,
        "last_catch_up_at": None,
        "running": False,
    }


@contextmanager
def job_period(period_key: str, *, catch_up: bool = False):
    """Attach a durable schedule period to one async job execution."""
    token = _JOB_PERIOD.set({"key": str(period_key), "catch_up": bool(catch_up)})
    try:
        yield
    finally:
        _JOB_PERIOD.reset(token)


def current_job_period() -> dict | None:
    """Return the schedule-period context for the current job execution."""
    period = _JOB_PERIOD.get()
    return copy.deepcopy(period) if period else None


def _persist_job_status(name: str, state: dict) -> None:
    def write() -> None:
        try:
            import db

            db.save_runtime_job_status(name, state)
        except Exception:
            # Runtime status should never break the job it is observing.
            return

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        write()
    else:
        # Preserve start/success/failure ordering on one worker while keeping
        # SQLite lock waits off Discord's event loop.
        future = _STATUS_WRITER.submit(write)
        with _STATUS_FUTURES_LOCK:
            _STATUS_FUTURES.add(future)
        future.add_done_callback(_discard_status_future)


def _discard_status_future(future) -> None:
    with _STATUS_FUTURES_LOCK:
        _STATUS_FUTURES.discard(future)


def flush_status_writes(timeout: float = 30.0) -> None:
    """Wait for queued status receipts (tests and graceful shutdowns)."""
    while True:
        with _STATUS_FUTURES_LOCK:
            pending = tuple(_STATUS_FUTURES)
        if not pending:
            return
        for future in pending:
            future.result(timeout=timeout)


def _state_for(name: str) -> dict:
    """In-memory state for one job, seeded from the persisted row on first touch.

    Must be called under `_LOCK`.

    The obvious `_JOB_STATUS.setdefault(name, _default_job_state())` is what this
    replaces, and it silently destroyed history. `_JOB_STATUS` is empty after a
    restart, so the first mark_job_* for a job built a FRESH default — with
    `last_success_at = None` and the counters at zero — and then persisted it
    over the real row. The record of when a job last succeeded therefore
    survived a restart but not the job's own next run.

    That is what makes this load-bearing rather than cosmetic: idempotent,
    catch-up-able jobs need a durable answer to "has this period already run?",
    and until now the answer was erased every week by the very run that should
    have been checking it. Measured on 2026-08-09: weekly_member_report last
    succeeded 2026-07-27 and silently skipped its 2026-08-03 slot, with nothing
    anywhere recording that it had not run.
    """
    state = _JOB_STATUS.get(name)
    if state is None:
        persisted = _load_persisted_job_status().get(name)
        state = _default_job_state()
        if persisted:
            # Merge onto the default so a row written by an older build, missing
            # a key added since, cannot KeyError the counters.
            state.update({k: v for k, v in persisted.items() if k in state})
        state["running"] = False
        _JOB_STATUS[name] = state
    return state


def _load_persisted_job_status() -> dict:
    try:
        import db

        statuses = db.list_runtime_job_status()
    except Exception:
        return {}
    for state in statuses.values():
        state["running"] = False
    return statuses


def clear_stale_running_jobs() -> list[str]:
    """Clear persisted running flags left behind by a previous process."""
    try:
        import db

        statuses = db.list_runtime_job_status()
    except Exception:
        return []

    now = _utcnow()
    cleared: list[str] = []
    with _LOCK:
        active_current_jobs = {name for name, state in _JOB_STATUS.items() if state.get("running")}

    for name, state in statuses.items():
        if not state.get("running") or name in active_current_jobs:
            continue
        cleaned = dict(state)
        cleaned["running"] = False
        cleaned["failure_count"] = int(cleaned.get("failure_count") or 0) + 1
        cleaned["last_finished_at"] = now
        cleaned["last_failure_at"] = now
        cleaned["last_error"] = "process restarted before job marked complete"
        db.save_runtime_job_status(name, cleaned)
        cleared.append(name)
    return cleared


def mark_job_start(name: str) -> None:
    with _LOCK:
        state = _state_for(name)
        state["run_count"] += 1
        state["last_started_at"] = _utcnow()
        period = _JOB_PERIOD.get()
        if period:
            state["last_started_period"] = period["key"]
            if period["catch_up"]:
                state["last_catch_up_period"] = period["key"]
        state["running"] = True
        snapshot_state = copy.deepcopy(state)
    _persist_job_status(name, snapshot_state)


def mark_job_success(name: str, summary: str | None = None) -> None:
    with _LOCK:
        state = _state_for(name)
        now = _utcnow()
        state["success_count"] = int(state.get("success_count", 0)) + 1
        state["last_finished_at"] = now
        state["last_success_at"] = now
        state["last_error"] = None
        state["last_summary"] = summary
        period = _JOB_PERIOD.get()
        if period:
            state["last_success_period"] = period["key"]
            if state.get("last_skipped_period") == period["key"]:
                state["last_skipped_period"] = None
                state["last_skipped_at"] = None
                state["last_skipped_reason"] = None
        state["running"] = False
        snapshot_state = copy.deepcopy(state)
    _persist_job_status(name, snapshot_state)
    # A success re-arms the failure alert so the next real failure is heard again.
    from runtime import alerts

    alerts.clear_job_failure_alert(name)


def mark_job_failure(name: str, error: str) -> None:
    # ERROR, and here rather than at the 50 call sites, because only 11 of them
    # logged anything themselves — so a job could fail, set last_error, and
    # increment failure_count while producing no line at all in
    # logs/elixir-error.log, which is the file an operator (and
    # scripts/confidence_report.py) reads to decide whether Elixir is healthy.
    # memory_synthesis failed that way on 2026-08-03 and sat unnoticed with zero
    # lifetime successes. A job that marks failure has not degraded, it has
    # stopped, so WARNING was the wrong severity wherever it was used.
    log.error("job_failed job=%s error=%s", name, error)
    with _LOCK:
        state = _state_for(name)
        now = _utcnow()
        state["failure_count"] = int(state.get("failure_count", 0)) + 1
        state["last_finished_at"] = now
        state["last_failure_at"] = now
        state["last_error"] = str(error)
        state["running"] = False
        snapshot_state = copy.deepcopy(state)
    _persist_job_status(name, snapshot_state)
    # Surface the failure in #elixir-log (deduped) — scheduled-job failures used to
    # live only in the log + status page.
    from runtime import alerts

    alerts.schedule_job_failure_alert(name, str(error))


def job_state(name: str) -> dict:
    """Return one job's current durable/in-memory state without mutating it."""
    with _LOCK:
        return copy.deepcopy(_state_for(name))


def mark_job_period_skipped(name: str, period_key: str, reason: str) -> None:
    """Durably satisfy a period without pretending the job succeeded.

    This is an operator decision for a deliverable that should no longer run,
    or an automatic supersession when live-data semantics would otherwise run
    a newer member-facing period early. It is deliberately distinct from a job
    success so health counters and last-success timestamps stay truthful.
    """
    with _LOCK:
        state = _state_for(name)
        if state.get("last_success_period") == str(period_key):
            raise ValueError(f"period already succeeded: {name}/{period_key}")
        now = _utcnow()
        state["last_skipped_period"] = str(period_key)
        state["last_skipped_at"] = now
        state["last_skipped_reason"] = str(reason)
        snapshot_state = copy.deepcopy(state)
    _persist_job_status(name, snapshot_state)


def claim_catch_up_period(
    name: str,
    period_key: str,
    *,
    attempted_at: str | None = None,
    retry_after: timedelta = timedelta(hours=6),
) -> str:
    """Atomically reserve a catch-up attempt, with bounded failure retries."""
    key = str(period_key)
    attempted_at = attempted_at or _utcnow()
    with _LOCK:
        state = _state_for(name)
        if state.get("running"):
            return "busy"
        if state.get("last_success_period") == key:
            return "current"
        if state.get("last_skipped_period") == key:
            return "skipped"
        if state.get("last_catch_up_period") == key and state.get("last_catch_up_at"):
            try:
                previous = datetime.fromisoformat(str(state["last_catch_up_at"]))
                current = datetime.fromisoformat(str(attempted_at))
            except ValueError:
                pass
            else:
                if current - previous < retry_after:
                    return "cooldown"
        state["last_catch_up_period"] = key
        state["last_catch_up_at"] = attempted_at
        snapshot_state = copy.deepcopy(state)
    _persist_job_status(name, snapshot_state)
    return "claimed"


def record_api_call(
    endpoint: str,
    entity_key: str | None = None,
    *,
    ok: bool,
    status_code=None,
    error=None,
    duration_ms=None,
) -> None:
    with _LOCK:
        now = _utcnow()
        _API_STATUS["call_count"] += 1
        if ok:
            _API_STATUS["success_count"] += 1
            _API_STATUS["consecutive_error_count"] = 0
        else:
            _API_STATUS["error_count"] += 1
            _API_STATUS["consecutive_error_count"] += 1
        _API_STATUS["last_call_at"] = now
        _API_STATUS["last_endpoint"] = endpoint
        _API_STATUS["last_entity_key"] = entity_key
        _API_STATUS["last_ok"] = bool(ok)
        _API_STATUS["last_status_code"] = status_code
        _API_STATUS["last_error"] = str(error) if error else None
        _API_STATUS["last_duration_ms"] = duration_ms

        per_endpoint = _API_STATUS["by_endpoint"].setdefault(
            endpoint,
            {
                "call_count": 0,
                "success_count": 0,
                "error_count": 0,
                "last_call_at": None,
                "last_ok": None,
                "last_status_code": None,
                "last_error": None,
                "last_duration_ms": None,
            },
        )
        per_endpoint["call_count"] += 1
        if ok:
            per_endpoint["success_count"] += 1
        else:
            per_endpoint["error_count"] += 1
        per_endpoint["last_call_at"] = now
        per_endpoint["last_ok"] = bool(ok)
        per_endpoint["last_status_code"] = status_code
        per_endpoint["last_error"] = str(error) if error else None
        per_endpoint["last_duration_ms"] = duration_ms


def record_llm_call(
    workflow: str,
    *,
    ok: bool,
    model=None,
    error=None,
    duration_ms=None,
    prompt_tokens=None,
    completion_tokens=None,
    total_tokens=None,
    cache_creation_tokens=None,
    cache_read_tokens=None,
) -> None:
    with _LOCK:
        now = _utcnow()
        _LLM_STATUS["call_count"] += 1
        if ok:
            _LLM_STATUS["success_count"] += 1
            _LLM_STATUS["consecutive_error_count"] = 0
        else:
            _LLM_STATUS["error_count"] += 1
            _LLM_STATUS["consecutive_error_count"] += 1
        _LLM_STATUS["last_call_at"] = now
        _LLM_STATUS["last_workflow"] = workflow
        _LLM_STATUS["last_model"] = model
        _LLM_STATUS["last_ok"] = bool(ok)
        _LLM_STATUS["last_error"] = str(error) if error else None
        _LLM_STATUS["last_duration_ms"] = duration_ms
        _LLM_STATUS["last_prompt_tokens"] = prompt_tokens
        _LLM_STATUS["last_completion_tokens"] = completion_tokens
        _LLM_STATUS["last_total_tokens"] = total_tokens
        _LLM_STATUS["last_cache_creation_tokens"] = cache_creation_tokens
        _LLM_STATUS["last_cache_read_tokens"] = cache_read_tokens

        per_workflow = _LLM_STATUS["by_workflow"].setdefault(
            workflow,
            {
                "call_count": 0,
                "success_count": 0,
                "error_count": 0,
                "last_call_at": None,
                "last_model": None,
                "last_ok": None,
                "last_error": None,
                "last_duration_ms": None,
                "last_prompt_tokens": None,
                "last_completion_tokens": None,
                "last_total_tokens": None,
            },
        )
        per_workflow["call_count"] += 1
        if ok:
            per_workflow["success_count"] += 1
        else:
            per_workflow["error_count"] += 1
        per_workflow["last_call_at"] = now
        per_workflow["last_model"] = model
        per_workflow["last_ok"] = bool(ok)
        per_workflow["last_error"] = str(error) if error else None
        per_workflow["last_duration_ms"] = duration_ms
        per_workflow["last_prompt_tokens"] = prompt_tokens
        per_workflow["last_completion_tokens"] = completion_tokens
        per_workflow["last_total_tokens"] = total_tokens


# The environment Elixir needs, named once. Elixir deliberately has no central
# config module (each module reads os.getenv directly — see agent/mail), so this
# is only a manifest of the critical names, not a config layer: REQUIRED_SECRETS
# are fatal at startup (runtime.app.main), OPTIONAL_ENV is reported but never
# blocks boot. The status keys are spelled out to keep the snapshot API stable.
REQUIRED_SECRETS = ("DISCORD_TOKEN", "CLAUDE_API_KEY", "CR_API_KEY")
OPTIONAL_ENV = ("ELIXIR_LOG_WEBHOOK_URL",)
_STATUS_ENV_KEYS = {
    "DISCORD_TOKEN": "has_discord_token",
    "CLAUDE_API_KEY": "has_claude_api_key",
    "CR_API_KEY": "has_cr_api_key",
    "ELIXIR_LOG_WEBHOOK_URL": "has_elixir_log_webhook",
}


def _env_present(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def missing_required_secrets() -> list[str]:
    """Required env vars that are unset/blank, in declaration order.

    Without this, a missing DISCORD_TOKEN or CLAUDE_API_KEY surfaces late and
    obscurely — as a None token deep inside discord.py, or an auth error on the
    first LLM call hours after boot.
    """
    return [name for name in REQUIRED_SECRETS if not _env_present(name)]


def snapshot() -> dict:
    persisted_jobs = _load_persisted_job_status()
    with _LOCK:
        jobs = copy.deepcopy(persisted_jobs)
        jobs.update(copy.deepcopy(_JOB_STATUS))
        return {
            "started_at": STARTED_AT,
            "env": {
                _STATUS_ENV_KEYS[name]: _env_present(name)
                for name in REQUIRED_SECRETS + OPTIONAL_ENV
            },
            "jobs": jobs,
            "api": copy.deepcopy(_API_STATUS),
            "llm": copy.deepcopy(_LLM_STATUS),
        }
