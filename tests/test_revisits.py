"""Tests for self-scheduled revisits (PR2 of #12)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import db

# Trigger full agent init before importing tool_exec to avoid a circular import.
import elixir  # noqa: F401
from agent import tool_exec
from agent.tool_policy import (
    _WRITE_TOOL_NAMES,
    AWARENESS_WRITE_TOOL_NAMES,
    TOOLSETS_BY_WORKFLOW,
)
from storage.revisits import (
    list_due_revisits,
    list_pending_revisits,
    mark_revisited,
    schedule_revisit,
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


def _iso(*, minutes: int) -> str:
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Tool policy
# ---------------------------------------------------------------------------


def test_revisit_is_folded_into_followup_tool():
    tool_names = {t["name"] for t in TOOLSETS_BY_WORKFLOW["awareness"]}
    assert "schedule_revisit" not in tool_names
    assert "schedule_revisit" not in _WRITE_TOOL_NAMES
    assert "schedule_revisit" not in AWARENESS_WRITE_TOOL_NAMES
    followup = next(
        t for t in TOOLSETS_BY_WORKFLOW["awareness"] if t["name"] == "record_leadership_followup"
    )
    assert "revisit_at" in followup["input_schema"]["properties"]


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


def test_schedule_revisit_persists_and_is_idempotent(memdb):
    first = schedule_revisit(
        signal_key="arena_change::#ABC",
        due_at=_iso(minutes=-10),
        rationale="Check if Vijay held Spirit Square through the week.",
    )
    assert first["signal_key"] == "arena_change::#ABC"
    assert first["rationale"].startswith("Check if Vijay")

    # Same (signal_key, due_at) returns the same row without duplicating.
    second = schedule_revisit(
        signal_key="arena_change::#ABC",
        due_at=first["due_at"],
        rationale="ignored — insert-or-ignore",
    )
    assert second["revisit_id"] == first["revisit_id"]

    # Different due_at creates a new revisit for the same signal_key.
    later = schedule_revisit(
        signal_key="arena_change::#ABC",
        due_at=_iso(minutes=60),
        rationale="Second look a bit later.",
    )
    assert later["revisit_id"] != first["revisit_id"]


def test_list_due_revisits_respects_now_filter(memdb):
    schedule_revisit(
        signal_key="past-due",
        due_at=_iso(minutes=-30),
        rationale="already due",
    )
    schedule_revisit(
        signal_key="future",
        due_at=_iso(minutes=60),
        rationale="not yet",
    )

    due = list_due_revisits()
    keys = {r["signal_key"] for r in due}
    assert "past-due" in keys
    assert "future" not in keys


def test_mark_revisited_updates_only_matching_pending_rows(memdb):
    schedule_revisit(signal_key="k-a", due_at=_iso(minutes=-5), rationale="a")
    schedule_revisit(signal_key="k-b", due_at=_iso(minutes=-5), rationale="b")
    schedule_revisit(signal_key="k-c", due_at=_iso(minutes=-5), rationale="c")

    updated = mark_revisited(["k-a", "k-c", "not-present"])
    assert updated == 2

    pending = {r["signal_key"] for r in list_pending_revisits()}
    assert pending == {"k-b"}


def test_schedule_revisit_rejects_bad_due_at(memdb):
    with pytest.raises(ValueError):
        schedule_revisit(signal_key="x", due_at="not-a-date", rationale="")
    with pytest.raises(ValueError):
        schedule_revisit(signal_key="", due_at=_iso(minutes=10), rationale="")


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


def test_followup_tool_can_schedule_revisit(memdb):
    raw = tool_exec._execute_tool(
        "record_leadership_followup",
        {
            "topic": "Battle hot streak",
            "recommendation": "Check if streak survives battle day.",
            "signal_key": "battle_hot_streak::#ABC",
            "revisit_at": _iso(minutes=180),
        },
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["revisit"]["signal_key"] == "battle_hot_streak::#ABC"
    assert result["revisit"]["revisit_id"]

    pending = list_pending_revisits()
    assert len(pending) == 1
    assert pending[0]["signal_key"] == "battle_hot_streak::#ABC"


def test_followup_revisit_rejects_missing_signal(memdb):
    raw = tool_exec._execute_tool(
        "record_leadership_followup",
        {
            "topic": "Later check",
            "recommendation": "Check this later.",
            "revisit_at": _iso(minutes=10),
        },
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["error"] == "revisit_requires_time_and_signal"


# ---------------------------------------------------------------------------
# Situation integration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Delivery layer: covered revisits get marked
# ---------------------------------------------------------------------------


def test_mark_revisited_clears_covered_and_skipped_revisits(memdb):
    schedule_revisit(signal_key="covered-key", due_at=_iso(minutes=-5), rationale="x")
    schedule_revisit(signal_key="skipped-key", due_at=_iso(minutes=-5), rationale="y")
    schedule_revisit(signal_key="untouched", due_at=_iso(minutes=-5), rationale="z")

    # Simulate what the reactive tick does after a successful pass: call
    # mark_revisited with everything the agent saw (covered + skipped + fallback).
    mark_revisited(["covered-key", "skipped-key"])

    pending_keys = {r["signal_key"] for r in list_pending_revisits()}
    assert pending_keys == {"untouched"}


def test_awareness_loop_clears_surfaced_revisits(monkeypatch):
    """QA H22: a non-failed live tick marks the revisits it surfaced as done, so
    they don't nag the read every tick forever."""
    from unittest.mock import patch

    from runtime.awareness import loop as loop_mod

    read = {
        "due_revisits": [{"signal_key": "war:demo", "rationale": "look again"}],
        "_degraded": [],
    }
    silence_plan = {"posts": [], "skipped_reason": "nothing new"}

    with (
        patch("runtime.awareness.read.build_read", return_value=read),
        patch("agent.workflows.run_awareness_tick", return_value=silence_plan),
        patch(
            "runtime.awareness.store.persist_thought",
            return_value={"thought_id": "t", "loop_number": 1},
        ),
        patch(
            "runtime.awareness.diagnostic.build_diagnostic_render",
            return_value={"outcome": "silence"},
        ),
        patch("storage.revisits.mark_revisited", return_value=1) as mark,
    ):
        counters = loop_mod.run_awareness_loop()

    mark.assert_called_once_with(["war:demo"])
    assert counters.get("revisits_cleared") == 1
