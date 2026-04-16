"""Tests for self-scheduled revisits (PR2 of #12)."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Trigger full agent init before importing tool_exec to avoid a circular import.
import elixir  # noqa: F401

import db
from agent import tool_exec
from agent.tool_policy import (
    AWARENESS_WRITE_TOOL_NAMES,
    TOOLSETS_BY_WORKFLOW,
    _WRITE_TOOL_NAMES,
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

def test_schedule_revisit_is_in_awareness_toolset_and_write_names():
    tool_names = {t["name"] for t in TOOLSETS_BY_WORKFLOW["awareness"]}
    assert "schedule_revisit" in tool_names
    assert "schedule_revisit" in _WRITE_TOOL_NAMES
    assert "schedule_revisit" in AWARENESS_WRITE_TOOL_NAMES


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

def test_schedule_revisit_tool_persists_to_db(memdb):
    raw = tool_exec._execute_tool(
        "schedule_revisit",
        {
            "signal_key": "battle_hot_streak::#ABC",
            "at": _iso(minutes=180),
            "rationale": "Check if streak survives battle day.",
        },
        workflow="awareness",
    )
    result = json.loads(raw)
    assert result["success"] is True
    assert result["signal_key"] == "battle_hot_streak::#ABC"
    assert result["revisit_id"]

    pending = list_pending_revisits()
    assert len(pending) == 1
    assert pending[0]["signal_key"] == "battle_hot_streak::#ABC"


def test_schedule_revisit_tool_rejects_missing_args(memdb):
    raw = tool_exec._execute_tool(
        "schedule_revisit",
        {"signal_key": "", "at": _iso(minutes=10), "rationale": ""},
        workflow="awareness",
    )
    result = json.loads(raw)
    assert "error" in result


# ---------------------------------------------------------------------------
# Situation integration
# ---------------------------------------------------------------------------

def test_build_situation_surfaces_due_revisits(memdb):
    import heartbeat
    from runtime.situation import build_situation, situation_is_quiet

    schedule_revisit(
        signal_key="war_battle_rank_change::s131:w2:p008::rank2",
        due_at=_iso(minutes=-15),
        rationale="Re-check rank at +4h.",
    )
    schedule_revisit(
        signal_key="future::still-upcoming",
        due_at=_iso(minutes=120),
        rationale="Not due yet.",
    )

    bundle = heartbeat.HeartbeatTickResult(signals=[], clan={}, war={})
    situation = build_situation(bundle)
    due = situation.get("due_revisits") or []
    keys = {r["signal_key"] for r in due}
    assert "war_battle_rank_change::s131:w2:p008::rank2" in keys
    assert "future::still-upcoming" not in keys

    # A due revisit should wake the agent even with no raw signals.
    assert not situation_is_quiet(situation)


def test_build_situation_quiet_when_no_signals_and_no_due_revisits(memdb):
    import heartbeat
    from runtime.situation import build_situation, situation_is_quiet

    bundle = heartbeat.HeartbeatTickResult(signals=[], clan={}, war={})
    situation = build_situation(bundle)
    assert situation_is_quiet(situation)


# ---------------------------------------------------------------------------
# Delivery layer: covered revisits get marked
# ---------------------------------------------------------------------------

def test_mark_revisited_clears_covered_and_skipped_revisits(memdb):
    schedule_revisit(signal_key="covered-key", due_at=_iso(minutes=-5), rationale="x")
    schedule_revisit(signal_key="skipped-key", due_at=_iso(minutes=-5), rationale="y")
    schedule_revisit(signal_key="untouched", due_at=_iso(minutes=-5), rationale="z")

    # Simulate what runtime/jobs/_signals does after a successful tick: call
    # mark_revisited with everything the agent saw (covered + skipped + fallback).
    mark_revisited(["covered-key", "skipped-key"])

    pending_keys = {r["signal_key"] for r in list_pending_revisits()}
    assert pending_keys == {"untouched"}
