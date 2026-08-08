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
from unittest.mock import patch

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


# --------------------------------------------------------- thinking is bounded (2026-08-08)


def _kwargs_for(monkeypatch, workflow):
    """Capture the kwargs core sends to the API for one workflow."""
    seen = {}

    class _Client:
        @property
        def messages(self):
            class _M:
                def create(self, **kw):
                    seen.update(kw)
                    return _Resp("ok", "end_turn")

            return _M()

    monkeypatch.setattr(core, "_get_client", lambda: _Client())
    core._create_chat_completion(
        workflow=workflow, messages=[{"role": "user", "content": "x"}], max_tokens=512
    )
    return seen


def test_effort_bounds_thinking_on_models_that_support_it(monkeypatch):
    """`budget_tokens` was REMOVED and 400s on claude-sonnet-5 / claude-opus-5, so
    effort is the only way to bound thinking. Nothing set it until 2026-08-08,
    which is why one ask_elixir_daily call spent its whole 4096-token ceiling on
    thinking and emitted no text and no tool call at all."""
    kwargs = _kwargs_for(monkeypatch, "awareness")
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["output_config"] == {"effort": "high"}
    assert "budget_tokens" not in str(kwargs), "budget_tokens is a 400 on this model"


def test_effort_is_per_workflow(monkeypatch):
    """A short daily post does not need the brain's reasoning depth."""
    assert _kwargs_for(monkeypatch, "ask_elixir_daily")["output_config"] == {"effort": "medium"}
    assert _kwargs_for(monkeypatch, "awareness")["output_config"] == {"effort": "high"}


def test_unlisted_workflow_gets_the_default_effort():
    """The effort lookup falls back rather than raising on an unknown workflow."""
    assert core._effort_for_workflow("some_workflow_with_no_entry") == core.DEFAULT_EFFORT


def test_an_unregistered_workflow_routes_to_haiku_and_so_sends_no_effort(monkeypatch):
    """Worth pinning because it is a two-step fallback that is easy to misread:
    an unknown workflow resolves to the LIGHTWEIGHT family, and that model rejects
    effort — so the default effort above is never actually sent for one."""
    kwargs = _kwargs_for(monkeypatch, "some_workflow_with_no_entry")
    assert kwargs["model"].startswith("claude-haiku-4-5")
    assert "output_config" not in kwargs


def test_effort_is_never_sent_to_haiku(monkeypatch):
    """`output_config.effort` is rejected outright on Haiku 4.5 — sending it would
    turn every working lightweight call into a 400. Those models also do not think
    by default, so there is nothing to bound."""
    kwargs = _kwargs_for(monkeypatch, "memory_distill")
    assert kwargs["model"].startswith("claude-haiku-4-5")
    assert "output_config" not in kwargs


def test_supports_effort_guard():
    assert core._supports_effort("claude-sonnet-5")
    assert core._supports_effort("claude-opus-5")
    assert not core._supports_effort("claude-haiku-4-5-20251001")


def test_temperature_is_not_sent_to_models_that_reject_it(monkeypatch):
    """Sending it cost a guaranteed failed round trip on EVERY call.

    `temperature` was removed on the Claude 5 generation, so the API 400s and the
    code retried without it — two round trips per call, one of them certain to
    fail. Measured over the 7 days to 2026-08-08: 249 such calls.
    """
    for workflow in ("awareness", "recruiting_copy"):
        kwargs = _kwargs_for(monkeypatch, workflow)
        assert not kwargs["model"].startswith("claude-haiku")
        assert "temperature" not in kwargs, f"{workflow} still sends a rejected parameter"


def test_temperature_is_still_sent_where_it_works(monkeypatch):
    """Not a blanket removal — it remains live on Haiku 4.5, so dropping it
    everywhere would silently change lightweight behaviour."""
    kwargs = _kwargs_for(monkeypatch, "memory_distill")
    assert kwargs["model"].startswith("claude-haiku-4-5")
    assert "temperature" in kwargs


def test_supports_sampling_guard():
    for rejects in ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-opus-4-8"):
        assert not core._supports_sampling(rejects), rejects
    for accepts in ("claude-haiku-4-5-20251001", "claude-sonnet-4-6"):
        assert core._supports_sampling(accepts), accepts


def test_every_configured_model_family_is_covered_by_both_gates():
    """The gates are keyed on id prefixes, so a model id change silently falls
    through to the wrong branch. Pin the live families against both."""
    families = {
        "chat": core._chat_model_name(),
        "creative": core._creative_model_name(),
        "intensive": core._intensive_model_name(),
        "lightweight": core._lightweight_model_name(),
    }
    # Claude 5 tiers: effort yes, sampling no. Haiku: the exact inverse.
    for name in ("chat", "creative", "intensive"):
        model = families[name]
        assert core._supports_effort(model), f"{name}={model} lost effort"
        assert not core._supports_sampling(model), f"{name}={model} would 400 on temperature"
    light = families["lightweight"]
    assert not core._supports_effort(light), f"lightweight={light} would 400 on effort"
    assert core._supports_sampling(light)


def test_capture_records_thinking_blocks_that_the_text_extractor_drops(monkeypatch):
    """A response that is ALL thinking must not serialize as "the model returned
    nothing".

    These models emit `thinking` blocks unasked — nothing in this codebase sets
    the `thinking` parameter — and response_text/response_tool_uses filter by
    exact type. On 2026-08-08 an ask_elixir_daily call burned its entire 4096
    ceiling on thinking and the stored response showed empty text and zero tool
    calls, which read as an impossible result and cost a full diagnosis cycle to
    explain.
    """
    import json as _json

    class _Thinking:
        type = "thinking"
        thinking = "x" * 3117

    class _AllThinking:
        stop_reason = "max_tokens"
        content = [_Thinking()]

    captured = _json.loads(core._serialize_response(_AllThinking()))

    assert captured["text"] in (None, ""), "premise: the text extractor sees nothing"
    assert captured["tool_uses"] == []
    census = captured["block_census"]
    assert census["thinking"]["blocks"] == 1
    assert census["thinking"]["chars"] == 3117, "the tokens must be accounted for somewhere"


def test_capture_census_covers_text_and_tool_use_too(monkeypatch):
    import json as _json

    class _Text:
        type = "text"
        text = "hello"

    class _Tool:
        type = "tool_use"
        name = "get_clan_roster"
        input = {"aspect": "list"}

    class _Mixed:
        stop_reason = "tool_use"
        content = [_Text(), _Tool()]

    census = _json.loads(core._serialize_response(_Mixed()))["block_census"]
    assert census["text"]["blocks"] == 1
    assert census["tool_use"]["blocks"] == 1


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


# ------------------------------------------------- ceilings raised on evidence (2026-08-06)


def test_distill_summary_discards_a_truncated_summary(monkeypatch):
    """A cut-off summary reads as complete and becomes the durable record of what
    a member said. 12 of 71 calls truncated against max_tokens=100 while the
    successful ones averaged 71 tokens and peaked at 99 — finishing flush against
    the wall. The caller treats None as "no summary" and falls back safely, so
    discarding is strictly better than storing half a sentence."""
    from agent import memory_tasks

    monkeypatch.setattr(
        memory_tasks,
        "_create_chat_completion",
        lambda **kw: _Resp("The member asked about their ranked deck and whether", "max_tokens"),
    )
    assert memory_tasks.distill_summary("x" * 400) is None


def test_distill_summary_keeps_a_complete_summary(monkeypatch):
    from agent import memory_tasks

    monkeypatch.setattr(
        memory_tasks,
        "_create_chat_completion",
        lambda **kw: _Resp("The member asked about their ranked deck.", "end_turn"),
    )
    assert memory_tasks.distill_summary("x" * 400) == "The member asked about their ranked deck."


def test_distill_ceiling_is_not_flush_against_typical_output(monkeypatch):
    """Floor, not an exact value — dropping back toward 100 is the regression."""
    from agent import memory_tasks

    seen = {}

    def capture(**kw):
        seen.update(kw)
        return _Resp("summary.", "end_turn")

    monkeypatch.setattr(memory_tasks, "_create_chat_completion", capture)
    memory_tasks.distill_summary("x" * 400)
    assert seen["max_tokens"] >= 256


def test_recruiting_copy_ceiling_covers_five_channels_plus_thinking(monkeypatch):
    """One call writes copy for all five channels, so 1500 was ~300 each.

    It had no margin even when it worked: the 2026-07-31 run on claude-opus-4-8
    finished at 1495 tokens, five under the ceiling. This is a `creative`
    workflow (claude-opus-5), which draws extended thinking from max_tokens, and
    the 2026-08-07 run truncated at exactly 1500 and failed
    promotion_content_cycle. Floor, not an exact value.
    """
    import agent.workflows as workflows

    seen = {}

    def capture(**kw):
        seen.update(kw)
        return _Resp('{"discord": "copy"}', "end_turn")

    monkeypatch.setattr(workflows, "_create_chat_completion", capture)
    monkeypatch.setattr(workflows, "_promote_system", lambda **kw: "sys")
    monkeypatch.setattr(workflows, "_clan_context", lambda *a, **k: "clan")
    monkeypatch.setattr(workflows, "_promotion_context", lambda *a, **k: "promo")

    workflows.generate_promote_content({"requiredTrophies": 2000})
    assert seen["max_tokens"] >= 8192


def test_member_report_ceiling_clears_its_largest_observed_output():
    """The largest successful member_report was 1367 output tokens against a
    1400 ceiling. A 2% margin is not a margin."""
    import agent.workflows as workflows

    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return ""

    with (
        patch.object(workflows, "_generate_simple_message", side_effect=capture),
        patch.object(workflows, "_member_report_system", return_value="sys"),
    ):
        workflows.generate_member_report("facts about one member")

    assert seen["max_tokens"] >= 2048
