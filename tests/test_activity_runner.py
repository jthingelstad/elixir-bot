from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from runtime.activity_runner import (
    ManualActivityNotAllowed,
    UnknownActivityError,
    run_activity_once,
    run_shell_activity,
)


def test_run_activity_once_calls_registered_async_activity():
    calls = []

    async def fake_tick():
        calls.append("tick")
        return {"posted": 0, "failed": 0}

    runtime = SimpleNamespace(
        HEARTBEAT_INTERVAL_MINUTES=60,
        _v5_reactive_tick=fake_tick,
    )

    result = asyncio.run(run_activity_once("v5-reactive-tick", runtime_module=runtime))

    assert calls == ["tick"]
    assert result.activity_key == "v5-reactive-tick"
    assert result.job_function == "_v5_reactive_tick"
    assert result.result == {"posted": 0, "failed": 0}


def test_run_activity_once_rejects_unknown_activity():
    with pytest.raises(UnknownActivityError):
        asyncio.run(run_activity_once("not-real", runtime_module=SimpleNamespace()))


def test_run_activity_once_rejects_non_manual_activity_before_calling_job():
    runtime = SimpleNamespace(_war_poll_tick=lambda: pytest.fail("should not run"))

    with pytest.raises(ManualActivityNotAllowed):
        asyncio.run(run_activity_once("war-poll", runtime_module=runtime))


def test_run_shell_activity_rejects_non_manual_activity_before_rest_setup(monkeypatch):
    async def fail_rest_setup(_runtime_module):
        pytest.fail("REST setup should not run")

    monkeypatch.setattr("runtime.activity_runner._build_rest_channel_lookup", fail_rest_setup)

    with pytest.raises(ManualActivityNotAllowed):
        asyncio.run(run_shell_activity("war-poll"))


def test_run_shell_activity_rejects_unknown_activity_before_rest_setup(monkeypatch):
    async def fail_rest_setup(_runtime_module):
        pytest.fail("REST setup should not run")

    monkeypatch.setattr("runtime.activity_runner._build_rest_channel_lookup", fail_rest_setup)

    with pytest.raises(UnknownActivityError):
        asyncio.run(run_shell_activity("not-real"))
