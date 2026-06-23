"""Tests for the weekly memory-synthesis job (PR3 of #12)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Trigger full runtime init before importing runtime.jobs (which depends on
# the elixir_agent facade) to avoid circular-import surprises.
import elixir  # noqa: F401

import db
from agent.tool_policy import (
    RESPONSE_SCHEMAS_BY_WORKFLOW,
    TOOLSETS_BY_WORKFLOW,
)
from memory_store import SOURCE_TYPES, create_memory, list_memories
from runtime.jobs._memory import (
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
    _memory_synthesis_cycle,
)
import runtime.jobs._memory as memory_job


@pytest.fixture
def memdb(tmp_path, monkeypatch):
    """Route every db.get_connection() call to the same temp SQLite file."""
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
# source_type + tool policy
# ---------------------------------------------------------------------------

def test_elixir_synthesis_source_type_is_allowed():
    assert "elixir_synthesis" in SOURCE_TYPES


def test_memory_synthesis_workflow_has_empty_toolset_and_strict_schema():
    # The agent reasons from the prompt payload alone; no tool chain.
    assert TOOLSETS_BY_WORKFLOW["memory_synthesis"] == []
    schema = RESPONSE_SCHEMAS_BY_WORKFLOW["memory_synthesis"]
    required = set(schema["required"])
    assert required == {"arc_memories", "stale_memory_ids", "contradictions", "digest"}


# ---------------------------------------------------------------------------
# _apply_memory_synthesis_plan
# ---------------------------------------------------------------------------

def test_apply_plan_writes_arc_memories_with_elixir_synthesis_source(memdb):
    plan = {
        "arc_memories": [
            {
                "title": "Week 5 colosseum: the Gareth push",
                "body": "Gareth closed out colosseum week with back-to-back 1st-place finishes.",
                "scope": "leadership",
                "tags": ["arc", "colosseum"],
                "war_week_id": "131:5",
                "war_season_id": "131",
            }
        ],
        "stale_memory_ids": [],
        "contradictions": [],
        "digest": "short digest",
    }
    stats = _apply_memory_synthesis_plan(plan, week_id="131:5", dry_run=False)
    assert stats["arcs_written"] == 1
    assert stats["stale_expired"] == 0

    memories = list_memories(viewer_scope="leadership")
    assert len(memories) == 1
    arc = memories[0]
    assert arc["source_type"] == "elixir_synthesis"
    assert arc["is_inference"] == 0
    assert arc["confidence"] == 1.0
    assert arc["war_week_id"] == "131:5"
    assert "arc" in (arc.get("tags") or [])


def test_apply_plan_expires_stale_memory_ids(memdb):
    # Seed two existing memories — one we'll mark stale, one we won't.
    keeper = create_memory(
        title="Keep me",
        body="Still relevant.",
        source_type="leader_note",
        is_inference=False,
        confidence=1.0,
        created_by="leader",
        scope="leadership",
    )
    stale = create_memory(
        title="Retire me",
        body="No longer accurate.",
        source_type="leader_note",
        is_inference=False,
        confidence=1.0,
        created_by="leader",
        scope="leadership",
    )

    plan = {
        "arc_memories": [],
        "stale_memory_ids": [stale["memory_id"]],
        "contradictions": [],
        "digest": "",
    }
    stats = _apply_memory_synthesis_plan(plan, week_id=None, dry_run=False)
    assert stats["stale_expired"] == 1

    visible = {m["memory_id"] for m in list_memories(viewer_scope="leadership")}
    # The stale memory is expired and should not surface in active reads.
    assert stale["memory_id"] not in visible
    assert keeper["memory_id"] in visible


def test_apply_plan_dry_run_persists_nothing(memdb):
    plan = {
        "arc_memories": [
            {"title": "Would-be arc", "body": "body", "scope": "leadership", "tags": []}
        ],
        "stale_memory_ids": [],
        "contradictions": [],
        "digest": "dry",
    }
    stats = _apply_memory_synthesis_plan(plan, week_id="131:5", dry_run=True)
    assert stats["dry_run"] is True
    assert stats["arcs_requested"] == 1
    assert stats["arcs_written"] == 0
    assert list_memories(viewer_scope="leadership") == []


def test_apply_plan_auto_expires_non_leader_contradictions(memdb):
    metric_memory = create_memory(
        title="donation snapshot",
        body="TDuck led donations with 527.",
        source_type="elixir_inference",
        is_inference=True,
        confidence=0.7,
        created_by="elixir",
        scope="leadership",
    )
    human_memory = create_memory(
        title="availability note",
        body="Fullboat is a member camping through war week.",
        source_type="elixir_inference",
        is_inference=True,
        confidence=0.7,
        created_by="elixir",
        scope="leadership",
    )
    plan = {
        "arc_memories": [],
        "stale_memory_ids": [],
        "contradictions": [
            {
                "memory_id": metric_memory["memory_id"],
                "stored": "TDuck led donations with 527.",
                "live": "Donation leaderboard changed.",
                "suggested_action": "retire",
                "category": "metric_snapshot",
                "needs_leader_review": False,
            },
            {
                "memory_id": human_memory["memory_id"],
                "stored": "Fullboat is a member camping through war week.",
                "live": "Leader note may mean they are back now.",
                "suggested_action": "escalate",
                "category": "human_context",
                "needs_leader_review": True,
            },
        ],
        "digest": "flagged",
    }
    stats = _apply_memory_synthesis_plan(plan, week_id=None, dry_run=False)
    assert stats["contradictions_flagged"] == 2
    assert stats["contradictions_auto_expired"] == 1
    assert stats["contradictions_leader_review"] == 1
    visible = {m["memory_id"] for m in list_memories(viewer_scope="leadership")}
    assert metric_memory["memory_id"] not in visible
    assert human_memory["memory_id"] in visible


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def test_build_context_returns_expected_keys(memdb):
    # Seed one recent memory so week_memories isn't empty.
    create_memory(
        title="recent",
        body="a recent leadership observation",
        source_type="elixir_inference",
        is_inference=True,
        confidence=0.7,
        created_by="elixir",
        scope="leadership",
    )
    context = _build_memory_synthesis_context()
    assert set(context.keys()) >= {
        "week_window",
        "week_memories",
        "prior_arcs",
        "week_posts",
    }
    assert isinstance(context["week_memories"], list)
    # Recent memory should appear in the week window.
    titles = {m.get("title") for m in context["week_memories"]}
    assert "recent" in titles


def test_build_context_includes_operations_context(memdb, monkeypatch):
    monkeypatch.setattr(memory_job.event_facades, "summarize_event_windows", lambda **kwargs: {
        "7d": {"total": 2, "by_type": {"member_join": 1}},
    })
    monkeypatch.setattr(memory_job.event_facades, "list_recent_events", lambda **kwargs: [
        {
            "event_key": "game_event:join",
            "event_type": "member_join",
            "scope": "public",
            "subject_key": "#ABC",
            "source_signal_key": "join:#ABC",
            "observed_at": "2026-06-19T12:00:00",
        }
    ])
    monkeypatch.setattr(memory_job.db, "get_war_season_snapshot", lambda: {
        "season_id": 133,
        "summary": "Season 133; rank 1",
    })
    monkeypatch.setattr(memory_job.event_facades, "summarize_battle_modes", lambda **kwargs: {
        "7d": {"modes": {"ranked": {"battles": 12}}},
    })
    monkeypatch.setattr(memory_job.db, "get_season_window", lambda: {
        "season_id": 133, "weeks_recorded": 2,
    })
    monkeypatch.setattr(memory_job.db, "decision_case_snapshot", lambda **kwargs: {
        "due": [{"case_id": 1, "case_type": "inactivity_review"}],
        "open": [],
    })
    monkeypatch.setattr(memory_job.db, "list_recent_communication_intents", lambda **kwargs: [
        {
            "intent_id": 5,
            "workflow": "arena-relay",
            "intent_type": "action_card",
            "status": "delivered",
            "target_channel_key": "arena-relay",
            "source_signal_key": "join:#ABC",
            "updated_at": "2026-06-19T12:01:00",
        }
    ])

    context = _build_memory_synthesis_context()

    operations = context["operations_context"]
    assert operations["event_windows"]["7d"]["total"] == 2
    assert operations["recent_events"][0]["event_key"] == "game_event:join"
    assert operations["war_season"]["season_id"] == 133
    assert operations["game_modes"]["7d"]["modes"]["ranked"]["battles"] == 12
    assert operations["season_window"]["weeks_recorded"] == 2
    assert operations["decision_cases"]["due"][0]["case_id"] == 1
    assert operations["recent_intents"][0]["intent_id"] == 5


def test_build_context_bounds_memory_count_and_text_size(memdb, monkeypatch):
    monkeypatch.setattr(memory_job, "MEMORY_SYNTHESIS_MEMORY_LIMIT", 2)
    monkeypatch.setattr(memory_job, "MEMORY_SYNTHESIS_MEMORY_BODY_CHARS", 24)
    for idx in range(4):
        create_memory(
            title=f"recent {idx}",
            body="x" * 80,
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.7,
            created_by="elixir",
            scope="leadership",
        )

    context = _build_memory_synthesis_context()

    assert len(context["week_memories"]) == 2
    assert all(len(item["body"]) <= 24 for item in context["week_memories"])
    assert all(item["body"].endswith("…") for item in context["week_memories"])


def test_memory_synthesis_cycle_posts_only_leader_review_contradiction_cards():
    """The weekly synthesis keeps its memory writes but ships no digest
    report. Derived-state contradictions are auto-expired/logged; only
    human-judgment contradictions become arena-relay action cards."""
    from types import SimpleNamespace

    channel = MagicMock()
    channel.name = "leader-actions"
    channel.type = "text"
    plan = {
        "digest": "This week the clan pushed hard.",
        "arc_memories": [],
        "stale_memory_ids": [],
        "contradictions": [
            {
                "memory_id": 41,
                "stored": "TDuck led donations with 527.",
                "live": "Donation leaderboard changed.",
                "suggested_action": "retire",
                "category": "metric_snapshot",
                "needs_leader_review": False,
            },
            {
                "memory_id": 42,
                "stored": "Fullboat is a member camping through war week.",
                "live": "Leader note may mean they are back now.",
                "suggested_action": "escalate",
                "category": "human_context",
                "needs_leader_review": True,
            },
        ],
    }
    created = {"action_id": 9, "source_message_id": None}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._memory.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._memory._build_memory_synthesis_context", return_value={"week_window": {"war_week_id": "131:2"}}),
        patch("runtime.jobs._memory.elixir_agent.run_memory_synthesis", return_value=plan),
        patch("runtime.jobs._memory._apply_memory_synthesis_plan", return_value={
            "arcs_written": 0, "stale_expired": 1, "contradictions_flagged": 2,
            "contradictions_auto_expired": 1, "contradictions_leader_review": 1,
            "arcs_requested": 0, "stale_requested": 0, "dry_run": False,
        }),
        patch("runtime.jobs._memory.MEMORY_SYNTHESIS_DRY_RUN", False),
        patch("runtime.jobs._memory.upsert_weekly_summary_memory") as mock_memory,
        patch("runtime.jobs._memory.elixir_log.post_event_async", new=AsyncMock()) as mock_elixir_log,
        patch("runtime.jobs._memory.prompts.discord_singleton_lane", return_value={"id": 900, "name": "#leader-actions"}),
        patch("runtime.jobs._memory.bot.get_channel", return_value=channel),
        patch("runtime.jobs._memory.db.create_leader_action_recommendation", return_value=created) as mock_create,
        patch("runtime.jobs._memory.post_leader_action_card", new=AsyncMock(return_value=[SimpleNamespace(id=1)])) as mock_card,
        patch("runtime.jobs._memory.db.save_message") as mock_save,
        patch("runtime.jobs._memory.runtime_status.mark_job_start"),
        patch("runtime.jobs._memory.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(_memory_synthesis_cycle())

    # Digest persists as durable memory, not as a Discord post.
    mock_memory.assert_called_once()
    assert mock_memory.call_args.kwargs["event_type"] == "weekly_memory_synthesis"
    # One action card for the leader-judgment contradiction only.
    assert mock_create.call_args.kwargs["action_type"] == "memory_review"
    assert mock_create.call_args.kwargs["source_signal_key"] == "memory_contradiction:42"
    assert "Fullboat is a member camping" in mock_create.call_args.kwargs["prompt_text"]
    mock_card.assert_awaited_once()
    assert mock_save.call_args.kwargs["event_type"] == "memory_contradiction"
    mock_elixir_log.assert_awaited_once()
    assert "Auto-expired metric/current-state memories: 1" in mock_elixir_log.call_args.args[0]
    assert "contradiction_cards=1" in mock_success.call_args.args[1]


def test_memory_synthesis_cycle_quiet_week_posts_nothing():
    """No contradictions → no Discord output at all."""
    plan = {"digest": "", "arc_memories": [], "stale_memory_ids": [], "contradictions": []}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._memory.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._memory._build_memory_synthesis_context", return_value={"week_window": {}}),
        patch("runtime.jobs._memory.elixir_agent.run_memory_synthesis", return_value=plan),
        patch("runtime.jobs._memory._apply_memory_synthesis_plan", return_value={
            "arcs_written": 0, "stale_expired": 0, "contradictions_flagged": 0,
            "arcs_requested": 0, "stale_requested": 0, "dry_run": False,
        }),
        patch("runtime.jobs._memory.MEMORY_SYNTHESIS_DRY_RUN", False),
        patch("runtime.jobs._memory.upsert_weekly_summary_memory") as mock_memory,
        patch("runtime.jobs._memory.elixir_log.post_event_async", new=AsyncMock()) as mock_elixir_log,
        patch("runtime.jobs._memory.post_leader_action_card", new=AsyncMock()) as mock_card,
        patch("runtime.jobs._memory.runtime_status.mark_job_start"),
        patch("runtime.jobs._memory.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(_memory_synthesis_cycle())

    mock_memory.assert_not_called()
    mock_card.assert_not_awaited()
    mock_elixir_log.assert_not_awaited()
    assert "contradiction_cards=0" in mock_success.call_args.args[1]


def test_memory_synthesis_cycle_marks_structured_agent_error_as_failure():
    channel = MagicMock()
    with (
        patch("runtime.jobs._memory.prompts.discord_channels_by_workflow", return_value=[{"id": 42}]),
        patch("runtime.jobs._memory.bot.get_channel", return_value=channel),
        patch("runtime.jobs._memory._build_memory_synthesis_context", return_value={"week_window": {}}),
        patch(
            "runtime.jobs._memory.elixir_agent.run_memory_synthesis",
            return_value={
                "_error": {
                    "kind": "truncation",
                    "phase": "initial_response",
                    "detail": "LLM response truncated by max_tokens=3000",
                }
            },
        ),
        patch("runtime.jobs._memory.runtime_status.mark_job_start") as mock_start,
        patch("runtime.jobs._memory.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(_memory_synthesis_cycle())

    mock_start.assert_called_once_with("memory_synthesis")
    mock_failure.assert_called_once()
    assert mock_failure.call_args.args[0] == "memory_synthesis"
    assert "truncation" in mock_failure.call_args.args[1]
