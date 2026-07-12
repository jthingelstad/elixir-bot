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
# circular import between agent.tool_exec → elixir_agent → agent.chat.
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


def test_flag_member_watch_away_until_records_leave_hold(memdb):
    # `away_until` = the member told leaders they'll be away → a `Hold:` memory
    # (tagged 'leave-hold', expiring at away_until) that the kick engine honors
    # as LOA grace. A plain watch never grants that grace.
    db.snapshot_members(
        [{"tag": "#ABC123", "name": "Vijay", "role": "member"}],
    )

    raw = tool_exec._execute_tool(
        "flag_member_watch",
        {"member_tag": "Vijay", "reason": "Told us he's travelling, back after the 20th",
         "away_until": "2026-07-20"},
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["type"] == "hold"
    assert result["away_until"] == "2026-07-20"

    memory = list_memories(viewer_scope="leadership")[0]
    assert memory["title"].startswith("Hold:")
    assert memory["expires_at"].startswith("2026-07-20")
    assert "leave-hold" in (memory.get("tags") or [])
    assert "watch-list" not in (memory.get("tags") or [])


def test_flag_member_watch_rejects_missing_args(memdb):
    raw = tool_exec._execute_tool(
        "flag_member_watch",
        {"member_tag": ""},
        workflow="awareness",
    )
    result = json.loads(raw)
    assert "error" in result


def test_flag_member_watch_can_upsert_decision_case(memdb):
    db.snapshot_members(
        [{"tag": "#ABC123", "name": "Vijay", "role": "member"}],
    )

    raw = tool_exec._execute_tool(
        "flag_member_watch",
        {
            "member_tag": "Vijay",
            "reason": "Silent past the inactivity threshold; review removal.",
            "case_type": "inactivity_review",
        },
        workflow="awareness",
    )
    result = json.loads(raw)

    assert result["success"] is True
    assert result["case_id"]
    case = db.get_decision_case_by_id(result["case_id"])
    assert case["case_type"] == "inactivity_review"
    assert case["target_player_tag"] == "#ABC123"


def test_awareness_write_does_not_reopen_a_leader_closed_case(memdb):
    """QA H20/H21: an awareness write must not silently reopen a decision case
    a leader deliberately resolved/dismissed."""
    db.snapshot_members([{"tag": "#ZZZ9", "name": "Rook", "role": "member"}])
    first = json.loads(tool_exec._execute_tool(
        "flag_member_watch",
        {"member_tag": "Rook", "reason": "Silent; review removal.", "case_type": "kick_review"},
        workflow="awareness",
    ))
    case_id = first["case_id"]
    # Leader dismisses the case.
    db.resolve_decision_case(case_id, status="dismissed", resolution="Leader kept them.")
    assert db.get_decision_case_by_id(case_id)["status"] == "dismissed"

    # A later awareness write must NOT reopen it.
    json.loads(tool_exec._execute_tool(
        "flag_member_watch",
        {"member_tag": "Rook", "reason": "Still silent.", "case_type": "kick_review"},
        workflow="awareness",
    ))
    assert db.get_decision_case_by_id(case_id)["status"] == "dismissed"

    # The controlled re-nomination path (allow_reopen=True) still may.
    db.upsert_member_review_case(
        case_type="kick_review",
        member={"tag": "#ZZZ9", "name": "Rook"},
        allow_reopen=True,
    )
    assert db.get_decision_case_by_id(case_id)["status"] == "open"


# ---------------------------------------------------------------------------
# record_leadership_followup
# ---------------------------------------------------------------------------

def test_record_leadership_followup_creates_leadership_inference_memory(memdb):
    raw = tool_exec._execute_tool(
        "record_leadership_followup",
        {
            "topic": "Week 3 no-shows",
            "recommendation": "Review the three members who skipped all battle days.",
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

def _fake_tool_use(tool_id, name, arguments):
    """Simulate the shape of a native Anthropic tool_use content block."""
    from types import SimpleNamespace

    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def _fake_response(content_blocks, stop_reason="end_turn"):
    """Simulate a native Anthropic Message response."""
    from types import SimpleNamespace

    return SimpleNamespace(content=content_blocks, stop_reason=stop_reason)


def _fake_text_block(text):
    from types import SimpleNamespace

    return SimpleNamespace(type="text", text=text)


def test_awareness_write_budget_rejects_fourth_call(memdb):
    """The 4th awareness write returns the budget error without calling the executor."""
    from agent import chat as agent_chat

    db.snapshot_members(
        [{"tag": f"#M{i}", "name": f"Member{i}", "role": "member"} for i in range(5)],
    )

    # Script the LLM responses: first turn makes 4 flag_member_watch calls;
    # second turn emits the final plan as JSON.
    tool_uses_round1 = [
        _fake_tool_use(f"t{i}", "flag_member_watch", {
            "member_tag": f"#M{i}", "reason": f"Observation {i}",
        })
        for i in range(4)
    ]

    responses = iter([
        _fake_response(tool_uses_round1, stop_reason="tool_use"),
        _fake_response([_fake_text_block(json.dumps({"posts": [], "skipped_reason": "budget test"}))]),
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

@pytest.mark.xfail(reason="awareness write-budget audit (awareness_ticks) retired at v5.1; record_awareness_tick is a documented no-op", strict=False)
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


def test_save_clan_memory_awareness_is_idempotent(memdb):
    """QA M28: a repeated identical awareness observation dedups instead of
    piling up duplicate memories (the leader path already upserts)."""
    args = {"title": "Andy hot streak", "body": "Andy on a 5-win run.", "member_tag": None}
    first = json.loads(tool_exec._execute_tool("save_clan_memory", args, workflow="awareness"))
    second = json.loads(tool_exec._execute_tool("save_clan_memory", args, workflow="awareness"))
    assert first["success"] and second["success"]
    assert first["memory_id"] == second["memory_id"]  # same memory, no duplicate
