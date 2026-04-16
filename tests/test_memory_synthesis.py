"""Tests for the weekly memory-synthesis job (PR3 of #12)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Trigger full runtime init before importing runtime.jobs (which depends on
# agent.app) to avoid circular-import surprises.
import elixir  # noqa: F401

import db
from agent.tool_policy import (
    RESPONSE_SCHEMAS_BY_WORKFLOW,
    TOOLSETS_BY_WORKFLOW,
)
from memory_store import SOURCE_TYPES, create_memory, list_memories
from runtime.jobs._core import (
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
)


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


def test_apply_plan_counts_contradictions_without_mutating(memdb):
    plan = {
        "arc_memories": [],
        "stale_memory_ids": [],
        "contradictions": [
            {"memory_id": 42, "stored": "A", "live": "B", "suggested_action": "escalate"},
            {"memory_id": 99, "stored": "C", "live": "D", "suggested_action": "retire"},
        ],
        "digest": "flagged",
    }
    stats = _apply_memory_synthesis_plan(plan, week_id=None, dry_run=False)
    assert stats["contradictions_flagged"] == 2


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
