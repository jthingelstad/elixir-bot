"""A truncated LLM answer must reach logs/elixir-error.log, and so must a failed job.

Both halves of this file exist because of the same 2026-08-06 finding: the
telemetry database had recorded 39 `stop_reason == "max_tokens"` responses
between 2026-07-28 and 2026-08-06 while the log held 16 lines about them, all at
WARNING. Nothing reached the ERROR log, so nothing was ever acted on.

Two separate gaps produced that:

1. `agent/chat.py` catches truncation, but only for the workflows that pass
   `return_errors=True`, and it logs at WARNING. Direct `agent.core.chat()`
   callers — memory_distill, member_report, release_notes — had NO detection at
   all: the API reports a truncated answer as a successful call, so nothing
   raised and each caller got a plausible short string.
2. `runtime_status.mark_job_failure` recorded the failure and alerted, but did
   not log. Only 11 of its 50 call sites logged for themselves, so a job could
   fail silently. memory_synthesis did exactly that on 2026-08-03 and sat for
   three days with zero lifetime successes and no ERROR line.

The assertions are about SEVERITY, not just the presence of a message. WARNING
was the bug — `logs/elixir-error.log` filters at ERROR, and that file is what an
operator and `scripts/confidence_report.py` read to decide Elixir is healthy.
"""

from __future__ import annotations

import logging

import agent.core as core
import runtime.status as runtime_status


class _Usage:
    input_tokens = 120
    output_tokens = 64
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Block:
    def __init__(self, **k):
        self.__dict__.update(k)


class _Resp:
    usage = _Usage()

    def __init__(self, text, stop_reason):
        self.content = [_Block(type="text", text=text)]
        self.stop_reason = stop_reason


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    @property
    def messages(self):
        resp = self._resp

        class _Messages:
            def create(self, **kw):
                return resp

        return _Messages()


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_truncated_response_logs_at_error(monkeypatch, caplog):
    monkeypatch.setattr(
        core, "_get_client", lambda: _FakeClient(_Resp("half an ans", "max_tokens"))
    )
    with caplog.at_level(logging.WARNING, logger="elixir"):
        core._create_chat_completion(
            workflow="memory_distill",
            messages=[{"role": "user", "content": "distill this"}],
            max_tokens=100,
        )

    errors = _errors(caplog)
    assert errors, "a max_tokens truncation must be logged at ERROR so it reaches the error log"
    message = errors[0].getMessage()
    assert "llm_truncated" in message
    # The workflow and the ceiling are the two things needed to act on it.
    assert "memory_distill" in message
    assert "100" in message


def test_untruncated_response_logs_no_error(monkeypatch, caplog):
    monkeypatch.setattr(
        core, "_get_client", lambda: _FakeClient(_Resp("a full answer", "end_turn"))
    )
    with caplog.at_level(logging.WARNING, logger="elixir"):
        core._create_chat_completion(
            workflow="memory_distill",
            messages=[{"role": "user", "content": "distill this"}],
            max_tokens=100,
        )

    assert not _errors(caplog), "a normal completion must not be reported as an error"


def test_truncation_is_caught_for_direct_callers_not_only_chat_with_tools(monkeypatch, caplog):
    """The regression that motivated moving detection to the funnel.

    memory_distill, member_report and release_notes never pass through
    `_chat_with_tools`, so a ceiling raise for those workflows was invisible.
    Detection has to sit where every call lands, not in the wrapper some
    workflows happen to use.
    """
    monkeypatch.setattr(core, "_get_client", lambda: _FakeClient(_Resp("cut off", "max_tokens")))
    seen = []
    for workflow in ("memory_distill", "member_report", "release_notes"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="elixir"):
            core._create_chat_completion(
                workflow=workflow,
                messages=[{"role": "user", "content": "go"}],
                max_tokens=256,
            )
        if _errors(caplog):
            seen.append(workflow)
    assert seen == ["memory_distill", "member_report", "release_notes"]


def test_mark_job_failure_logs_at_error(monkeypatch, caplog):
    # The persistence and Discord-alert side-effects are not what this asserts;
    # stub them so the test is about severity alone.
    from runtime import alerts

    monkeypatch.setattr(runtime_status, "_persist_job_status", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "schedule_job_failure_alert", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger="elixir"):
        runtime_status.mark_job_failure("memory_synthesis", "retry agent truncation")

    errors = _errors(caplog)
    assert errors, "a job failure must be logged at ERROR — WARNING never reaches the error log"
    message = errors[0].getMessage()
    assert "memory_synthesis" in message
    assert "retry agent truncation" in message
