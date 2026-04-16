"""Tests for the awareness-loop write surface (PR1 of #12).

Covers:
- `flag_member_watch` creates a leadership-scoped inference memory with the
  `watch-list` tag
- `record_leadership_followup` creates a leadership-scoped inference memory
  with the `followup` tag
- `save_clan_memory` called from workflow="awareness" records as
  `elixir_inference` rather than `leader_note`
- The per-tick write budget rejects the 4th write with a structured error
- Tool policy exposes the new write tools to awareness only
"""

import json
from unittest.mock import patch

import pytest

# Trigger full runtime/agent init before importing tool_exec — avoids a
# circular import between agent.tool_exec → agent.app → agent.chat.
import elixir  # noqa: F401

import db
from agent import tool_exec
from agent.tool_policy import (
    AWARENESS_WRITE_BUDGET_PER_TICK,
    AWARENESS_WRITE_TOOL_NAMES,
    TOOLSETS_BY_WORKFLOW,
    _WRITE_TOOL_NAMES,
)
from memory_store import list_memories


@pytest.fixture
def memdb(tmp_path, monkeypatch):
    """Route every db.get_connection() call to the same temp SQLite file.

    ``managed_connection`` opens and closes a fresh connection per call, so a
    single shared ``":memory:"`` connection gets closed after the first wrapped
    call. A tempfile-backed DB lets each call open its own connection while
    sharing persistent state across calls.
    """
    db_path = str(tmp_path / "elixir_test.db")
    original_get = db.get_connection

    def _redirect(*args, **kwargs):
        return original_get(db_path)

    monkeypatch.setattr(db, "get_connection", _redirect)
    setup_conn = original_get(db_path)
    try:
        yield setup_conn
    finally:
        setup_conn.close()


# ---------------------------------------------------------------------------
# Tool policy
# ---------------------------------------------------------------------------

def test_awareness_toolset_includes_the_three_write_tools():
    tool_names = {t["name"] for t in TOOLSETS_BY_WORKFLOW["awareness"]}
    assert "save_clan_memory" in tool_names
    assert "flag_member_watch" in tool_names
    assert "record_leadership_followup" in tool_names


def test_update_member_is_not_exposed_to_awareness():
    tool_names = {t["name"] for t in TOOLSETS_BY_WORKFLOW["awareness"]}
    # update_member stays clanops-only — mutating stored member metadata is a
    # leadership action, not an awareness observation.
    assert "update_member" not in tool_names


def test_write_tool_names_include_new_tools():
    assert "flag_member_watch" in _WRITE_TOOL_NAMES
    assert "record_leadership_followup" in _WRITE_TOOL_NAMES
    assert "save_clan_memory" in _WRITE_TOOL_NAMES
    assert "flag_member_watch" in AWARENESS_WRITE_TOOL_NAMES
    assert "record_leadership_followup" in AWARENESS_WRITE_TOOL_NAMES


# ---------------------------------------------------------------------------
# flag_member_watch
# ---------------------------------------------------------------------------

def test_flag_member_watch_creates_leadership_inference_memory(memdb):
    db.snapshot_members(
        [{"tag": "#ABC123", "name": "Vijay", "role": "member"}],
    )

    raw = tool_exec._execute_tool(
        "flag_member_watch",
        {"member_tag": "Vijay", "reason": "Silent for 5 days, last-seen drift"},
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["type"] == "watch"
    assert result["member_tag"] == "#ABC123"

    memories = list_memories(viewer_scope="leadership")
    assert len(memories) == 1
    memory = memories[0]
    assert memory["source_type"] == "elixir_inference"
    assert memory["is_inference"] == 1
    assert memory["scope"] == "leadership"
    assert memory["member_tag"] == "#ABC123"
    assert "watch-list" in (memory.get("tags") or [])


def test_flag_member_watch_rejects_missing_args(memdb):
    raw = tool_exec._execute_tool(
        "flag_member_watch",
        {"member_tag": ""},
        workflow="awareness",
    )
    result = json.loads(raw)
    assert "error" in result


# ---------------------------------------------------------------------------
# record_leadership_followup
# ---------------------------------------------------------------------------

def test_record_leadership_followup_creates_leadership_inference_memory(memdb):
    raw = tool_exec._execute_tool(
        "record_leadership_followup",
        {
            "topic": "Week 3 no-shows",
            "recommendation": "Consider nudging the three members who skipped all battle days.",
        },
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["type"] == "followup"

    memories = list_memories(viewer_scope="leadership")
    assert len(memories) == 1
    memory = memories[0]
    assert memory["source_type"] == "elixir_inference"
    assert memory["scope"] == "leadership"
    assert "followup" in (memory.get("tags") or [])
    assert memory["title"] == "Followup: Week 3 no-shows"


def test_record_leadership_followup_can_scope_to_member(memdb):
    db.snapshot_members(
        [{"tag": "#XYZ789", "name": "Gareth", "role": "elder"}],
    )
    raw = tool_exec._execute_tool(
        "record_leadership_followup",
        {
            "topic": "Promotion review",
            "recommendation": "Two weeks at rank 2–3 with 4/4 decks; consider coLeader.",
            "member_tag": "Gareth",
        },
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["member_tag"] == "#XYZ789"

    memories = list_memories(viewer_scope="leadership")
    assert memories[0]["member_tag"] == "#XYZ789"


# ---------------------------------------------------------------------------
# save_clan_memory branching for awareness
# ---------------------------------------------------------------------------

def test_save_clan_memory_from_awareness_uses_elixir_inference(memdb):
    raw = tool_exec._execute_tool(
        "save_clan_memory",
        {
            "title": "Gareth ladder push",
            "body": "Gareth's push started after the log-bait rework in week 4.",
        },
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["type"] == "elixir_inference"

    memories = list_memories(viewer_scope="leadership")
    assert len(memories) == 1
    memory = memories[0]
    assert memory["source_type"] == "elixir_inference"
    assert memory["is_inference"] == 1
    assert memory["confidence"] < 1.0


def test_save_clan_memory_from_clanops_still_uses_leader_note(memdb):
    raw = tool_exec._execute_tool(
        "save_clan_memory",
        {
            "title": "Promotion freeze",
            "body": "Leadership decided to freeze promotions until next season.",
        },
        workflow="clanops",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["type"] == "leader_note"

    memories = list_memories(viewer_scope="leadership")
    assert memories[0]["source_type"] == "leader_note"
    assert memories[0]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# Write-budget enforcement in chat.py tool-call loop
# ---------------------------------------------------------------------------

def _fake_tool_call(tool_id, name, arguments):
    """Simulate the shape of a `choice.message.tool_calls[*]` entry."""
    from types import SimpleNamespace

    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(id=tool_id, function=fn, type="function")


def test_awareness_write_budget_rejects_fourth_call(memdb):
    """The 4th awareness write returns the budget error without calling the executor."""
    from agent import chat as agent_chat

    db.snapshot_members(
        [{"tag": f"#M{i}", "name": f"Member{i}", "role": "member"} for i in range(5)],
    )

    # Script the LLM responses: first turn makes 4 flag_member_watch calls;
    # second turn emits the final plan as JSON.
    tool_calls_round1 = [
        _fake_tool_call(f"t{i}", "flag_member_watch", {
            "member_tag": f"#M{i}", "reason": f"Observation {i}",
        })
        for i in range(4)
    ]

    class _Msg:
        def __init__(self, content, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls or []

    class _Choice:
        def __init__(self, content, tool_calls=None, stop_reason="stop"):
            self.message = _Msg(content, tool_calls=tool_calls)
            self.stop_reason = stop_reason

    class _Resp:
        def __init__(self, choice):
            self.choices = [choice]

    responses = iter([
        _Resp(_Choice("", tool_calls=tool_calls_round1)),
        _Resp(_Choice(json.dumps({"posts": [], "skipped_reason": "budget test"}))),
    ])

    def _fake_completion(**kwargs):
        return next(responses)

    tool_stats: dict = {}
    with patch.object(agent_chat, "_create_chat_completion", side_effect=_fake_completion):
        result = agent_chat._chat_with_tools(
            "system", "user",
            workflow="awareness",
            allowed_tools=TOOLSETS_BY_WORKFLOW["awareness"],
            response_schema={"required": ["posts"]},
            strict_json=True,
            tool_stats=tool_stats,
        )

    assert result == {"posts": [], "skipped_reason": "budget test"}
    assert tool_stats["write_calls_issued"] == AWARENESS_WRITE_BUDGET_PER_TICK
    assert tool_stats["write_calls_denied"] == 4 - AWARENESS_WRITE_BUDGET_PER_TICK
    assert tool_stats["write_calls_succeeded"] == AWARENESS_WRITE_BUDGET_PER_TICK

    # Only 3 memories got created — the 4th write hit the budget wall.
    memories = list_memories(viewer_scope="leadership")
    assert len(memories) == AWARENESS_WRITE_BUDGET_PER_TICK


# ---------------------------------------------------------------------------
# Budget counters flow into record_awareness_tick
# ---------------------------------------------------------------------------

def test_record_awareness_tick_persists_write_counts(memdb):
    from storage.messages import record_awareness_tick

    tick_id = record_awareness_tick(
        workflow="clan_awareness",
        signals_in=2,
        posts_delivered=1,
        write_calls_issued=2,
        write_calls_succeeded=2,
        write_calls_denied=0,
    )
    row = memdb.execute(
        "SELECT write_calls_issued, write_calls_succeeded, write_calls_denied "
        "FROM awareness_ticks WHERE tick_id = ?",
        (tick_id,),
    ).fetchone()
    assert row["write_calls_issued"] == 2
    assert row["write_calls_succeeded"] == 2
    assert row["write_calls_denied"] == 0
