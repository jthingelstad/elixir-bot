"""Tests for the awareness loop (runtime/awareness).

The brain is the clan's sole proactive poster. These tests verify the read
assembler degrades sanely on an empty v5.1 DB, the loop persists a thought and
(when given a deliver_fn) delivers live, and the brain always runs with its full
read + write tool surface.
"""

from unittest.mock import patch

# Trigger full runtime/agent init before importing awareness internals — avoids
# a circular import between agent.tool_exec → elixir_agent → agent.chat.
import elixir  # noqa: F401

import db
from agent.workflow_registry import WRITE_TOOLS


# ---------------------------------------------------------------------------
# build_read
# ---------------------------------------------------------------------------

_EXPECTED_KEYS = {
    "time", "standing", "war_season", "signals_by_lane", "hard_post_signals",
    "decision_cases", "channel_memory", "recent_agent_writes",
    "leader_action_board", "due_revisits",
}


def test_build_read_returns_expected_keys_on_empty_db():
    from runtime.awareness.read import build_read

    read = build_read()
    assert isinstance(read, dict)
    assert _EXPECTED_KEYS <= set(read.keys())
    # Every block degrades independently — nothing raised.
    assert isinstance(read.get("_degraded"), list)
    # Grouped signal lanes always present as a dict.
    assert isinstance(read["signals_by_lane"], dict)
    assert isinstance(read["hard_post_signals"], list)
    # Empty DB → no active war.
    assert read["time"] is None
    assert read["decision_cases"] == {"due": [], "open": []}


# ---------------------------------------------------------------------------
# run_awareness_loop
# ---------------------------------------------------------------------------

def test_run_awareness_loop_without_deliver_fn_posts_nothing_member_facing(monkeypatch):
    """With no deliver_fn, the loop persists a thought and hands a render to the
    #thinking diagnostic stream — and makes NO member-facing send (delivery only
    happens when a deliver_fn is supplied)."""
    from runtime.awareness import loop as loop_mod

    plan = {
        "posts": [
            {
                "channel": "river-race",
                "content": "Practice day 3 — deficit math here.",
                "covers_signal_keys": ["war:demo"],
            }
        ],
        "skipped_reason": None,
    }

    events = []

    with patch("agent.workflows.run_awareness_tick", return_value=plan) as brain:
        counters = loop_mod.run_awareness_loop(progress_fn=events.append)

    brain.assert_called_once()

    # (a) The ONLY delivery is the diagnostic stream — a `start` then an `end`,
    # not a member-facing send. The end carries the outcome render.
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "end"
    render = events[-1]["render"]
    assert render["outcome"] == "posted"
    assert set(render["fields"]) == {"Read", "Thinking", "Decision"}
    assert render["thread_chunks"]  # full detail present

    assert counters["posts_planned"] == 1
    assert counters["chose_silence"] is False
    # No deliver_fn → nothing was delivered.
    assert counters.get("posts_delivered", 0) == 0

    # (b) A row was written to awareness_thoughts (shadow column is legacy → 0).
    conn = db.get_connection()
    try:
        from runtime.awareness.store import ensure_awareness_schema

        ensure_awareness_schema(conn)
        rows = conn.execute(
            "SELECT thought_id, post_count, shadow, chose_silence FROM awareness_thoughts"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["post_count"] == 1
    assert rows[0]["shadow"] == 0
    assert rows[0]["chose_silence"] == 0


def test_run_awareness_loop_live_delivers_the_plan(monkeypatch):
    """With a deliver_fn, the loop delivers the plan's posts live and records the
    delivery count."""
    from runtime.awareness import loop as loop_mod

    plan = {
        "posts": [{"channel": "elixir", "content": "live post", "covers_signal_keys": []}],
        "skipped_reason": None,
    }
    delivered = {}

    def _deliver(read, plan_arg):
        delivered["called"] = True
        return {"delivered": 1, "failed": False}

    with patch("agent.workflows.run_awareness_tick", return_value=plan) as brain:
        counters = loop_mod.run_awareness_loop(deliver_fn=_deliver)

    brain.assert_called_once()
    assert delivered.get("called") is True
    assert counters["posts_delivered"] == 1


def test_run_awareness_loop_records_silence(monkeypatch):
    from runtime.awareness import loop as loop_mod

    plan = {"posts": [], "skipped_reason": "nothing material changed"}

    with patch("agent.workflows.run_awareness_tick", return_value=plan):
        counters = loop_mod.run_awareness_loop()

    assert counters["chose_silence"] is True
    assert counters["posts_planned"] == 0

    from runtime.awareness.store import list_recent_thoughts

    thoughts = list_recent_thoughts(limit=5)
    assert len(thoughts) == 1
    assert thoughts[0]["chose_silence"] == 1
    assert thoughts[0]["skipped_reason"] == "nothing material changed"


def test_classify_plan_distinguishes_failure_from_silence():
    """A None / _error / posts-less result is a FAILURE, never silence."""
    from runtime.awareness.store import classify_plan

    assert classify_plan({"posts": [{"channel": "x"}]})[0] == "posted"
    assert classify_plan({"posts": [], "skipped_reason": "quiet"}) == ("silence", "quiet")
    # Failures — the harness must not read any of these as deliberate silence.
    assert classify_plan(None)[0] == "failed"
    assert classify_plan({})[0] == "failed"  # None coerced to {} upstream
    outcome, reason = classify_plan(
        {"_error": {"kind": "schema_error", "phase": "initial_response", "detail": "bad"}}
    )
    assert outcome == "failed"
    assert "schema_error" in reason and "bad" in reason


def test_run_awareness_loop_records_tick_failure_not_silence(monkeypatch):
    """A tick that returns None (truncation/timeout/schema) is recorded as a
    failure — chose_silence stays False and the thought carries a ⚠️ marker."""
    from runtime.awareness import loop as loop_mod
    from runtime.awareness.store import list_recent_thoughts


    with patch("agent.workflows.run_awareness_tick", return_value=None):
        counters = loop_mod.run_awareness_loop()

    assert counters["tick_failed"] is True
    assert counters["chose_silence"] is False
    assert counters["posts_planned"] == 0
    assert counters["error"] and counters["error"].startswith("tick_failed:")

    thoughts = list_recent_thoughts(limit=5)
    assert len(thoughts) == 1
    assert thoughts[0]["chose_silence"] == 0  # NOT painted as silence
    assert (thoughts[0]["skipped_reason"] or "").startswith("⚠️ tick failed")


def test_run_awareness_loop_numbers_loops_and_captures_tool_trace():
    """Each loop gets a stable, incrementing number, and the tools the brain
    reached for are captured into the render (header + threaded detail)."""
    from runtime.awareness import loop as loop_mod

    def _tick(read, *, tool_stats, on_event=None):
        # Simulate the brain drilling into battle detail via the tool surface —
        # the real tick both records the trace and streams a live tool event.
        entry = {
            "tool": "get_elixir_state", "args": "section=battle",
            "round": 0, "allowed": True, "result": "ok · 12 keys · 3200B",
        }
        tool_stats.setdefault("tool_trace", []).append(entry)
        if on_event is not None:
            on_event({"type": "tool", **entry})
        return {"posts": [], "skipped_reason": "quiet"}

    events1, events2 = [], []

    with patch("agent.workflows.run_awareness_tick", side_effect=_tick):
        c1 = loop_mod.run_awareness_loop(progress_fn=events1.append)
        c2 = loop_mod.run_awareness_loop(progress_fn=events2.append)

    assert c1["loop_number"] == 1 and c2["loop_number"] == 2
    assert c1["tool_calls"] == 1
    # The tool call streamed live as its own event during the tick.
    tool_events = [e for e in events1 if e["type"] == "tool"]
    assert tool_events and tool_events[0]["tool"] == "get_elixir_state"
    # The end event carries the loop number in its render header.
    end1 = events1[-1]
    assert end1["type"] == "end" and end1["loop_number"] == 1
    assert "Loop #1" in end1["render"]["header"]

    # And both were persisted with the thought.
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT loop_number, tool_trace_json FROM awareness_thoughts "
            "ORDER BY loop_number DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["loop_number"] == 2
    assert "get_elixir_state" in (row["tool_trace_json"] or "")


# ---------------------------------------------------------------------------
# run_awareness_tick — always the full read + write tool surface
# ---------------------------------------------------------------------------

def test_run_awareness_tick_uses_the_write_surface():
    """The brain always runs with the awareness toolset (read + write). Shadow
    mode was removed — there is no read-only variant."""
    import agent.workflows as workflows
    from agent.tool_policy import TOOLSETS_BY_WORKFLOW

    captured = {}

    def _capture(*args, **kwargs):
        captured["allowed_tools"] = kwargs.get("allowed_tools")
        return {"posts": []}

    with patch.object(workflows, "_chat_with_tools", side_effect=_capture):
        workflows.run_awareness_tick({"time": None})

    assert captured["allowed_tools"] is TOOLSETS_BY_WORKFLOW["awareness"]
    # The write surface is actually present (not the read-only set).
    allowed_names = {t["name"] for t in captured["allowed_tools"]}
    write_names = {t["name"] for t in WRITE_TOOLS}
    assert write_names, "expected write tools to exist"
    assert "save_clan_memory" in allowed_names
    assert allowed_names & write_names


# ---------------------------------------------------------------------------
# run_awareness_tick — truncation is transient over-generation, retry once
# ---------------------------------------------------------------------------

_TRUNCATION = {"_error": {"kind": "truncation", "phase": "initial_response",
                          "detail": "LLM response truncated by max_tokens=8192"}}


def test_run_awareness_tick_retries_once_on_truncation():
    """A truncated first pass is retried with more headroom and an economy
    nudge; the retry's clean plan is what the caller gets."""
    import agent.workflows as workflows

    calls = []
    plan = {"posts": [{"channel": "clan-events", "content": "hi"}]}

    def _fake(system, user_msg, **kwargs):
        calls.append({"user_msg": user_msg, "max_tokens": kwargs.get("max_tokens")})
        return _TRUNCATION if len(calls) == 1 else plan

    with patch.object(workflows, "_chat_with_tools", side_effect=_fake):
        result = workflows.run_awareness_tick({"time": None})

    assert result == plan
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 8192
    assert calls[1]["max_tokens"] == 16384  # retry gets real headroom
    assert "economical" in calls[1]["user_msg"]  # nudge appended
    assert "economical" not in calls[0]["user_msg"]


def test_run_awareness_tick_no_retry_when_first_pass_succeeds():
    import agent.workflows as workflows

    calls = []

    def _fake(system, user_msg, **kwargs):
        calls.append(1)
        return {"posts": []}

    with patch.object(workflows, "_chat_with_tools", side_effect=_fake):
        workflows.run_awareness_tick({"time": None})

    assert len(calls) == 1  # a clean first pass is never retried


def test_run_awareness_tick_persistent_truncation_surfaces_error():
    """If both passes truncate, the tick surfaces the {_error} payload so the
    loop classifies the tick as FAILED (with detail), never as silence."""
    import agent.workflows as workflows

    with patch.object(workflows, "_chat_with_tools", return_value=_TRUNCATION):
        result = workflows.run_awareness_tick({"time": None})

    assert result.get("_error", {}).get("kind") == "truncation"


# ---------------------------------------------------------------------------
# Diagnostic render — the full posted content must reach #elixir-log, never a
# 200-char preview (the diagnostic is the observability record of the tick).
# ---------------------------------------------------------------------------

def test_diagnostic_render_full_content_chunked_not_truncated():
    """The full posted content must survive into the thread detail, split across
    Discord-sized chunks — never truncated to a preview."""
    from runtime.awareness import diagnostic as diag_mod

    # A realistic long post — well over one Discord message.
    body = "\n".join(f"- line {i}: something worth reading in full" for i in range(120))
    plan = {"posts": [{"channel": "leader-lounge", "content": body,
                       "covers_signal_keys": ["k1"]}]}

    render = diag_mod.build_diagnostic_render({"time": None}, plan, tool_trace=[], loop_number=12)
    chunks = render["thread_chunks"]
    joined = "\n".join(chunks)

    assert render["outcome"] == "posted"
    assert "Loop #12" in render["header"]
    assert len(chunks) > 1, "a long post should be split across chunks"
    assert all(len(c) <= diag_mod._MAX_LEN for c in chunks)
    # Every source line survives somewhere — nothing was silently dropped.
    assert "- line 0:" in joined and "- line 119:" in joined
    assert "…" not in joined  # no ellipsis-truncation of the decision
