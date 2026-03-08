"""Runtime telemetry for Elixir health/status reporting."""

from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, timezone


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


_LOCK = threading.Lock()
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
_OPENAI_STATUS = {
    "call_count": 0,
    "success_count": 0,
    "error_count": 0,
    "last_call_at": None,
    "last_workflow": None,
    "last_model": None,
    "last_ok": None,
    "last_error": None,
    "last_duration_ms": None,
    "last_prompt_tokens": None,
    "last_completion_tokens": None,
    "last_total_tokens": None,
    "by_workflow": {},
}


def mark_job_start(name: str) -> None:
    with _LOCK:
        state = _JOB_STATUS.setdefault(
            name,
            {
                "run_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "last_started_at": None,
                "last_finished_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "last_error": None,
                "last_summary": None,
                "running": False,
            },
        )
        state["run_count"] += 1
        state["last_started_at"] = _utcnow()
        state["running"] = True


def mark_job_success(name: str, summary: str | None = None) -> None:
    with _LOCK:
        state = _JOB_STATUS.setdefault(name, {})
        now = _utcnow()
        state["success_count"] = int(state.get("success_count", 0)) + 1
        state["last_finished_at"] = now
        state["last_success_at"] = now
        state["last_error"] = None
        state["last_summary"] = summary
        state["running"] = False


def mark_job_failure(name: str, error: str) -> None:
    with _LOCK:
        state = _JOB_STATUS.setdefault(name, {})
        now = _utcnow()
        state["failure_count"] = int(state.get("failure_count", 0)) + 1
        state["last_finished_at"] = now
        state["last_failure_at"] = now
        state["last_error"] = str(error)
        state["running"] = False


def record_api_call(endpoint: str, entity_key: str | None = None, *, ok: bool, status_code=None, error=None, duration_ms=None) -> None:
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


def record_openai_call(workflow: str, *, ok: bool, model=None, error=None, duration_ms=None, prompt_tokens=None, completion_tokens=None, total_tokens=None) -> None:
    with _LOCK:
        now = _utcnow()
        _OPENAI_STATUS["call_count"] += 1
        if ok:
            _OPENAI_STATUS["success_count"] += 1
        else:
            _OPENAI_STATUS["error_count"] += 1
        _OPENAI_STATUS["last_call_at"] = now
        _OPENAI_STATUS["last_workflow"] = workflow
        _OPENAI_STATUS["last_model"] = model
        _OPENAI_STATUS["last_ok"] = bool(ok)
        _OPENAI_STATUS["last_error"] = str(error) if error else None
        _OPENAI_STATUS["last_duration_ms"] = duration_ms
        _OPENAI_STATUS["last_prompt_tokens"] = prompt_tokens
        _OPENAI_STATUS["last_completion_tokens"] = completion_tokens
        _OPENAI_STATUS["last_total_tokens"] = total_tokens

        per_workflow = _OPENAI_STATUS["by_workflow"].setdefault(
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


def snapshot() -> dict:
    with _LOCK:
        return {
            "started_at": STARTED_AT,
            "env": {
                "has_discord_token": bool(os.getenv("DISCORD_TOKEN")),
                "has_openai_api_key": bool(os.getenv("OPENAI_API_KEY")),
                "has_cr_api_key": bool(os.getenv("CR_API_KEY")),
            },
            "jobs": copy.deepcopy(_JOB_STATUS),
            "api": copy.deepcopy(_API_STATUS),
            "openai": copy.deepcopy(_OPENAI_STATUS),
        }
