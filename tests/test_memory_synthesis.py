"""Tests for the weekly memory-synthesis job (PR3 of #12)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import db

# Trigger full runtime init before importing runtime.jobs (which depends on
# the elixir_agent facade) to avoid circular-import surprises.
import elixir  # noqa: F401
import prompts
import runtime.jobs._memory as memory_job
from agent.tool_policy import (
    RESPONSE_SCHEMAS_BY_WORKFLOW,
    TOOLSETS_BY_WORKFLOW,
)
from memory_store import SOURCE_TYPES, create_memory, list_memories
from runtime.jobs._memory import (
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
    _memory_synthesis_cycle,
    _reduce_memory_synthesis_context_for_retry,
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


def test_memory_synthesis_prompt_excludes_temporal_progression_from_contradictions():
    prompt = prompts.agent_prompt("memory-synthesis")
    assert "Later events are not contradictions" in prompt
    assert "cannot both be true at the time each claim describes" in prompt
    assert "one concrete yes/no `leader_question`" in prompt


def test_memory_synthesis_bot_proxy_delegates_to_runtime_bot():
    """The production proxy must reach Discord instead of recursing into itself."""
    from types import SimpleNamespace

    runtime_bot = MagicMock()
    channel = object()
    runtime_bot.get_channel.return_value = channel

    with patch(
        "runtime.jobs._memory._runtime_app",
        return_value=SimpleNamespace(bot=runtime_bot),
    ):
        assert memory_job.bot.get_channel(900) is channel

    runtime_bot.get_channel.assert_called_once_with(900)


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
    stats = _apply_memory_synthesis_plan(plan, week_id="131:5")
    assert stats["arcs_written"] == 1
    assert stats["stale_expired"] == 0

    memories = list_memories(viewer_scope="leadership")
    assert len(memories) == 1
    arc = memories[0]
    assert arc["source_type"] == "elixir_synthesis"
    assert arc["is_inference"] == 0
    assert arc["confidence"] == 1.0
    assert arc["source_event_key"] == "131:131:5"
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
    stats = _apply_memory_synthesis_plan(plan, week_id=None)
    assert stats["stale_expired"] == 1

    visible = {m["memory_id"] for m in list_memories(viewer_scope="leadership")}
    # The stale memory is expired and should not surface in active reads.
    assert stale["memory_id"] not in visible
    assert keeper["memory_id"] in visible


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
        body="A leader says Fullboat is away through September 15.",
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
                "stored": "A leader says Fullboat is away through September 15.",
                "live": "A leader says Fullboat is available now on September 10.",
                "conflict_basis": "The availability claims overlap on September 10.",
                "suggested_action": "revise the availability note to say Fullboat is back",
                "leader_question": "Should Elixir revise the note to say Fullboat is back?",
                "category": "human_context",
                "needs_leader_review": True,
            },
        ],
        "digest": "flagged",
    }
    stats = _apply_memory_synthesis_plan(plan, week_id=None)
    assert stats["contradictions_flagged"] == 2
    assert stats["contradictions_auto_expired"] == 1
    assert stats["contradictions_leader_review"] == 1
    visible = {m["memory_id"] for m in list_memories(viewer_scope="leadership")}
    assert metric_memory["memory_id"] not in visible
    assert human_memory["memory_id"] in visible


def test_apply_plan_preserves_temporal_progression_misclassified_as_human_conflict(memdb):
    history = create_memory(
        title="roster history",
        body="A member left the clan on Sunday.",
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
                "memory_id": history["memory_id"],
                "stored": "A member left the clan on Sunday.",
                "live": "The member rejoined on Monday.",
                "suggested_action": "escalate",
                "category": "human_context",
                "needs_leader_review": True,
            }
        ],
        "digest": "",
    }

    stats = _apply_memory_synthesis_plan(plan, week_id=None)

    assert stats["contradictions_auto_expired"] == 0
    assert stats["contradictions_leader_review"] == 0
    visible = {m["memory_id"] for m in list_memories(viewer_scope="leadership")}
    assert history["memory_id"] in visible


def test_explicit_human_conflict_proof_is_not_demoted_by_derived_state_words():
    item = {
        "stored": "A leader says the role change is paused through September 15.",
        "live": "A leader says the same role change should happen on September 10.",
        "conflict_basis": "The two instructions overlap for the same role decision.",
        "leader_question": "Should Elixir keep the role change paused?",
        "category": "policy_or_preference",
        "needs_leader_review": True,
    }

    assert memory_job._requires_leader_memory_review(item) is True


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
    monkeypatch.setattr(
        memory_job.event_facades,
        "summarize_event_windows",
        lambda **kwargs: {
            "windows": {
                "7d": {
                    "total_events": 2,
                    "battles_mirrored": 0,
                    "by_type": {"member_join": 1},
                }
            },
        },
    )
    monkeypatch.setattr(
        memory_job.event_facades,
        "list_recent_events",
        lambda **kwargs: [
            {
                "event_key": "game_event:join",
                "event_type": "member_join",
                "scope": "public",
                "subject_key": "#ABC",
                "source_signal_key": "join:#ABC",
                "observed_at": "2026-06-19T12:00:00",
            }
        ],
    )
    monkeypatch.setattr(
        memory_job.db,
        "get_war_season_snapshot",
        lambda **kwargs: {
            "season_id": 133,
            "summary": "Season 133; rank 1",
        },
    )
    monkeypatch.setattr(
        memory_job.game_mode_capability,
        "get_clan_game_mode_windows",
        lambda **kwargs: {
            "capability": "clan_game_modes",
            "contract_version": 1,
            "windows": {"7d": {"modes": {"ranked": {"battles": 12}}}},
        },
    )
    monkeypatch.setattr(
        memory_job.db,
        "get_season_window",
        lambda: {
            "season_id": 133,
            "weeks_recorded": 2,
        },
    )
    monkeypatch.setattr(
        memory_job.db,
        "list_leader_actions",
        lambda **kwargs: [
            {"action_id": 1, "action_type": "kick_recommendation", "status": "proposed"}
        ],
    )
    monkeypatch.setattr(
        memory_job.db,
        "get_awareness_activity",
        lambda **kwargs: {
            "thoughts": [{"loop_number": 5, "skipped_reason": None}],
            "posts": [{"post_id": 7, "lane": "elixir"}],
        },
    )

    context = _build_memory_synthesis_context()

    operations = context["operations_context"]
    assert operations["event_windows"]["windows"]["7d"]["total_events"] == 2
    assert operations["recent_events"][0]["event_key"] == "game_event:join"
    assert operations["war_season"]["season_id"] == 133
    assert operations["game_modes"]["windows"]["7d"]["modes"]["ranked"]["battles"] == 12
    assert operations["season_window"]["weeks_recorded"] == 2
    assert operations["leader_actions"][0]["action_id"] == 1
    assert operations["awareness_activity"]["thoughts"][0]["loop_number"] == 5


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


def test_reduce_memory_synthesis_context_for_retry_bounds_large_payload():
    context = {
        "week_window": {"war_week_id": "131:4"},
        "week_memories": [
            {
                "memory_id": idx,
                "title": "x" * 240,
                "body": "b" * 1000,
                "summary": "s" * 500,
            }
            for idx in range(40)
        ],
        "prior_arcs": [
            {
                "memory_id": idx,
                "title": "arc " + ("x" * 240),
                "body": "a" * 1000,
                "summary": "s" * 500,
            }
            for idx in range(10)
        ],
        "week_posts": {
            "leader-lounge": [
                {
                    "content": "p" * 1000,
                    "summary": "s" * 500,
                    "created_at": "2026-06-20",
                }
                for _ in range(10)
            ]
        },
        "live_clan_state": {"roster": {"member_count": 50}},
        "operations_context": {
            "event_windows": {"7d": {"total": 5}},
            "recent_events": [
                {"event_key": f"event:{idx}", "event_type": "join"} for idx in range(40)
            ],
            "war_season": {
                "season_id": 131,
                "summary": "war " * 200,
                "state": {
                    "week": 4,
                    "phase": "battle",
                    "race": {
                        "rank": 1,
                        "clan_score": 12345,
                        "standings": [
                            {"rank": idx, "name": f"Clan {idx}", "score": idx * 100}
                            for idx in range(8)
                        ],
                    },
                    "participation_health": {
                        "total_members": 50,
                        "complete_members": 30,
                        "partial_members": 12,
                        "zero_attack_members": 8,
                        "time_left_text": "left " * 80,
                    },
                },
            },
            "game_modes": {
                "capability": "clan_game_modes",
                "contract_version": 1,
                "windows": {
                    "7d": {
                        "modes": {
                            "ranked": {
                                "label": "Ranked",
                                "battles": 12,
                                "top_members": [
                                    {
                                        "member_ref": f"Player {idx}",
                                        "player_tag": f"#{idx}",
                                        "battles": idx,
                                    }
                                    for idx in range(6)
                                ],
                            }
                        }
                    }
                },
            },
            "season_window": {"season_id": 131},
            "leader_actions": [
                {
                    "action_id": idx,
                    "action_type": "kick_recommendation",
                    "objective": "d" * 500,
                }
                for idx in range(12)
            ],
            "awareness_activity": {
                "thoughts": [
                    {"loop_number": idx, "skipped_reason": "r" * 500} for idx in range(20)
                ],
                "posts": [{"post_id": idx, "content_preview": "i" * 500} for idx in range(20)],
            },
        },
    }

    reduced = _reduce_memory_synthesis_context_for_retry(context)

    assert reduced["retry_context"]["reason"] == "initial_response_truncated"
    assert len(reduced["week_memories"]) == memory_job.MEMORY_SYNTHESIS_RETRY_MEMORY_LIMIT
    assert len(reduced["prior_arcs"]) == memory_job.MEMORY_SYNTHESIS_RETRY_PRIOR_ARC_LIMIT
    assert (
        len(reduced["week_posts"]["leader-lounge"])
        == memory_job.MEMORY_SYNTHESIS_RETRY_POSTS_PER_CHANNEL
    )
    assert (
        len(reduced["week_memories"][0]["body"])
        <= memory_job.MEMORY_SYNTHESIS_RETRY_MEMORY_BODY_CHARS
    )
    assert (
        len(reduced["week_posts"]["leader-lounge"][0]["content"])
        <= memory_job.MEMORY_SYNTHESIS_RETRY_POST_CHARS
    )
    operations = reduced["operations_context"]
    assert len(operations["recent_events"]) == memory_job.MEMORY_SYNTHESIS_RETRY_RECENT_EVENTS_LIMIT
    assert len(operations["awareness_activity"]["thoughts"]) == (
        memory_job.MEMORY_SYNTHESIS_RETRY_AWARENESS_LIMIT
    )
    assert len(operations["awareness_activity"]["posts"]) == (
        memory_job.MEMORY_SYNTHESIS_RETRY_AWARENESS_LIMIT
    )
    assert (
        len(operations["leader_actions"]) == memory_job.MEMORY_SYNTHESIS_RETRY_LEADER_ACTION_LIMIT
    )
    assert len(operations["war_season"]["state"]["race"]["standings"]) == 5
    assert len(operations["game_modes"]["windows"]["7d"]["modes"]["ranked"]["top_members"]) == 3


def test_memory_synthesis_cycle_posts_only_leader_review_contradiction_cards():
    """The weekly synthesis keeps its memory writes but ships no digest
    report. Derived-state contradictions are auto-expired/logged; only
    human-judgment contradictions become actions action cards."""
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
                "stored": "A leader says Fullboat is away through September 15.",
                "live": "A leader says Fullboat is available now on September 10.",
                "conflict_basis": "The availability claims overlap on September 10.",
                "suggested_action": "revise the availability note to say Fullboat is back",
                "leader_question": "Should Elixir revise the note to say Fullboat is back?",
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
        patch(
            "runtime.jobs._memory._build_memory_synthesis_context",
            return_value={"week_window": {"war_week_id": "131:2"}},
        ),
        patch("runtime.jobs._memory.elixir_agent.run_memory_synthesis", return_value=plan),
        patch(
            "runtime.jobs._memory._apply_memory_synthesis_plan",
            return_value={
                "arcs_written": 0,
                "stale_expired": 1,
                "contradictions_flagged": 2,
                "contradictions_auto_expired": 1,
                "contradictions_leader_review": 1,
                "arcs_requested": 0,
                "stale_requested": 0,
            },
        ),
        patch("runtime.jobs._memory.upsert_weekly_summary_memory") as mock_memory,
        patch(
            "runtime.jobs._memory.elixir_log.post_event_async", new=AsyncMock()
        ) as mock_elixir_log,
        patch(
            "runtime.jobs._memory.prompts.discord_singleton_lane",
            return_value={"id": 900, "name": "#leader-actions"},
        ),
        patch("runtime.jobs._memory.bot.get_channel", return_value=channel),
        patch(
            "runtime.jobs._memory.db.create_leader_action_recommendation",
            return_value=created,
        ) as mock_create,
        patch(
            "runtime.jobs._memory.post_leader_action_card",
            new=AsyncMock(return_value=[SimpleNamespace(id=1)]),
        ) as mock_card,
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
    assert (
        "Conflict: The availability claims overlap" in mock_create.call_args.kwargs["prompt_text"]
    )
    assert "Decision: Should Elixir revise the note" in mock_create.call_args.kwargs["prompt_text"]
    assert (
        "Suggested resolution: revise the availability note"
        in mock_create.call_args.kwargs["rationale"]
    )
    mock_card.assert_awaited_once()
    assert mock_save.call_args.kwargs["event_type"] == "memory_contradiction"
    mock_elixir_log.assert_awaited_once()
    assert "Auto-expired metric/current-state memories: 1" in mock_elixir_log.call_args.args[0]
    assert "contradiction_cards=1" in mock_success.call_args.args[1]


def test_memory_synthesis_cycle_keeps_success_when_actions_is_unconfigured():
    """An optional contradiction card must not discard a completed synthesis."""
    plan = {
        "digest": "This week the clan pushed hard.",
        "arc_memories": [],
        "stale_memory_ids": [],
        "contradictions": [
            {
                "memory_id": 42,
                "stored": "A leader says two Discord accounts belong to different members.",
                "live": "A newer leader note says both accounts belong to one member.",
                "conflict_basis": "The accounts cannot represent both one and two people.",
                "suggested_action": "confirm that both accounts belong to one member",
                "leader_question": "Should Elixir treat both accounts as one member?",
                "category": "identity_ambiguity",
                "needs_leader_review": True,
            }
        ],
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._memory.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "runtime.jobs._memory._build_memory_synthesis_context",
            return_value={"week_window": {"war_week_id": "131:2"}},
        ),
        patch("runtime.jobs._memory.elixir_agent.run_memory_synthesis", return_value=plan),
        patch(
            "runtime.jobs._memory._apply_memory_synthesis_plan",
            return_value={
                "arcs_written": 0,
                "stale_expired": 0,
                "contradictions_flagged": 1,
                "contradictions_auto_expired": 0,
                "contradictions_leader_review": 1,
                "arcs_requested": 0,
                "stale_requested": 0,
            },
        ),
        patch("runtime.jobs._memory.upsert_weekly_summary_memory") as mock_memory,
        patch("runtime.jobs._memory.prompts.discord_singleton_lane", return_value=None),
        patch("runtime.jobs._memory.bot.get_channel") as mock_channel,
        patch("runtime.jobs._memory.runtime_status.mark_job_start"),
        patch("runtime.jobs._memory.runtime_status.mark_job_success") as mock_success,
        # Without this the fake to_thread above runs post_event for REAL, and
        # this test's fixture text — "1 not delivered — those contradictions
        # reach nobody" — goes to the live #elixir-log. It did, 400 times
        # between 2026-07-30 and 2026-08-05, on every full test run.
        patch(
            "runtime.jobs._memory.elixir_log.post_event_async", new=AsyncMock()
        ) as mock_elixir_log,
    ):
        asyncio.run(_memory_synthesis_cycle())

    mock_memory.assert_called_once()
    mock_channel.assert_not_called()
    assert "contradiction_cards=0" in mock_success.call_args.args[1]
    # The hygiene warning is the whole point of the run — assert it was composed
    # rather than merely not-crashing.
    assert "reach nobody" in mock_elixir_log.call_args.args[0]


def test_memory_synthesis_cycle_quiet_week_posts_nothing():
    """No contradictions → no Discord output at all."""
    plan = {
        "digest": "",
        "arc_memories": [],
        "stale_memory_ids": [],
        "contradictions": [],
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._memory.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "runtime.jobs._memory._build_memory_synthesis_context",
            return_value={"week_window": {}},
        ),
        patch("runtime.jobs._memory.elixir_agent.run_memory_synthesis", return_value=plan),
        patch(
            "runtime.jobs._memory._apply_memory_synthesis_plan",
            return_value={
                "arcs_written": 0,
                "stale_expired": 0,
                "contradictions_flagged": 0,
                "arcs_requested": 0,
                "stale_requested": 0,
            },
        ),
        patch("runtime.jobs._memory.upsert_weekly_summary_memory") as mock_memory,
        patch(
            "runtime.jobs._memory.elixir_log.post_event_async", new=AsyncMock()
        ) as mock_elixir_log,
        patch("runtime.jobs._memory.post_leader_action_card", new=AsyncMock()) as mock_card,
        patch("runtime.jobs._memory.runtime_status.mark_job_start"),
        patch("runtime.jobs._memory.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(_memory_synthesis_cycle())

    mock_memory.assert_not_called()
    mock_card.assert_not_awaited()
    mock_elixir_log.assert_not_awaited()
    assert "contradiction_cards=0" in mock_success.call_args.args[1]


def test_memory_synthesis_cycle_retries_truncated_agent_with_reduced_context():
    context = {
        "week_window": {"war_week_id": "131:4"},
        "week_memories": [{"memory_id": idx, "body": "b" * 1000} for idx in range(40)],
        "prior_arcs": [{"memory_id": idx, "body": "a" * 1000} for idx in range(10)],
        "week_posts": {"leader-lounge": [{"content": "p" * 1000} for _ in range(10)]},
        "operations_context": {
            "recent_events": [{"event_key": f"event:{idx}"} for idx in range(40)],
            "awareness_activity": {
                "thoughts": [{"loop_number": idx} for idx in range(20)],
                "posts": [{"post_id": idx, "content_preview": "i" * 500} for idx in range(20)],
            },
        },
    }
    truncation = {
        "_error": {
            "kind": "truncation",
            "phase": "initial_response",
            "detail": "LLM response truncated by max_tokens=3000",
        }
    }
    plan = {
        "digest": "",
        "arc_memories": [],
        "stale_memory_ids": [],
        "contradictions": [],
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._memory.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._memory._build_memory_synthesis_context", return_value=context),
        patch(
            "runtime.jobs._memory.elixir_agent.run_memory_synthesis",
            side_effect=[truncation, plan],
        ) as mock_agent,
        patch(
            "runtime.jobs._memory._apply_memory_synthesis_plan",
            return_value={
                "arcs_written": 0,
                "stale_expired": 0,
                "contradictions_flagged": 0,
                "arcs_requested": 0,
                "stale_requested": 0,
            },
        ) as mock_apply,
        patch("runtime.jobs._memory.runtime_status.mark_job_start"),
        patch("runtime.jobs._memory.runtime_status.mark_job_success") as mock_success,
        patch("runtime.jobs._memory.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(_memory_synthesis_cycle())

    assert mock_agent.call_count == 2
    retry_context = mock_agent.call_args_list[1].args[0]
    assert retry_context["retry_context"]["reason"] == "initial_response_truncated"
    assert len(retry_context["week_memories"]) == memory_job.MEMORY_SYNTHESIS_RETRY_MEMORY_LIMIT
    assert (
        len(retry_context["week_memories"][0]["body"])
        <= memory_job.MEMORY_SYNTHESIS_RETRY_MEMORY_BODY_CHARS
    )
    assert mock_apply.call_args.kwargs["week_id"] == "131:4"
    mock_failure.assert_not_called()
    mock_success.assert_called_once()


def test_memory_synthesis_cycle_marks_structured_agent_error_as_failure():
    channel = MagicMock()
    with (
        patch(
            "runtime.jobs._memory.prompts.discord_channels_by_workflow",
            return_value=[{"id": 42}],
        ),
        patch("runtime.jobs._memory.bot.get_channel", return_value=channel),
        patch(
            "runtime.jobs._memory._build_memory_synthesis_context",
            return_value={"week_window": {}},
        ),
        patch(
            "runtime.jobs._memory.elixir_agent.run_memory_synthesis",
            return_value={
                "_error": {
                    "kind": "schema_error",
                    "phase": "initial_response",
                    "detail": "missing required digest field",
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
    assert "schema_error" in mock_failure.call_args.args[1]


def test_hygiene_report_never_prints_a_zero_that_hides_a_dropped_card():
    """The 0/0 report Jamie saw in #elixir-log.

    The guard fired on `contradictions_leader_review` but the message printed
    `cards_posted` — a different quantity. A run where three contradictions
    needed a leader and no card posted rendered as "Auto-expired: 0 /
    Leader-review cards: 0", which reads as "nothing happened" while actually
    announcing that leadership judgment was dropped on the floor.
    """
    import re

    source = open("runtime/jobs/_memory.py", encoding="utf-8").read()
    block = source[source.index("cards_posted = await _post_memory_contradiction_cards") :][:2400]

    # The count that explains WHY the post exists must appear in the post.
    assert "Contradictions needing leader review" in block

    # A shortfall must be loud, not a quiet zero.
    assert "undelivered" in block
    assert re.search(r"log\.error\(", block), "a dropped leader review must reach the error log"

    # And the report must not fire when there is genuinely nothing to say.
    assert "if auto_expired or needs_review:" in block
