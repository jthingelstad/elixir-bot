"""Tests for the Phase 4 awareness-loop architecture."""

import asyncio
from unittest.mock import AsyncMock, patch

# Import elixir first so the full runtime.app + runtime.jobs module graph
# loads cleanly. Importing runtime.jobs._signals directly first creates a
# partial-load circular import (runtime.app → runtime.jobs → _core → _signals).
import elixir  # noqa: F401
import heartbeat
import runtime.jobs._signals as signals_module
from runtime.situation import (
    CHANNEL_LANES,
    HARD_POST_SIGNAL_TYPES,
    build_situation,
    classify_signal_lane,
    situation_is_quiet,
)


# ---------------------------------------------------------------------------
# Lane classification
# ---------------------------------------------------------------------------

def test_classify_signal_lane_war():
    assert classify_signal_lane({"type": "war_battle_rank_change"}) == "war"
    assert classify_signal_lane({"type": "war_week_complete"}) == "war"


def test_classify_signal_lane_battle_mode():
    assert classify_signal_lane({"type": "battle_hot_streak"}) == "battle_mode"
    assert classify_signal_lane({"type": "battle_trophy_push"}) == "battle_mode"
    assert classify_signal_lane({"type": "path_of_legend_promotion"}) == "battle_mode"


def test_classify_signal_lane_milestone():
    assert classify_signal_lane({"type": "arena_change"}) == "milestone"
    assert classify_signal_lane({"type": "new_card_unlocked"}) == "milestone"
    # path_of_legend_promotion was moved to battle_mode in Phase 3 — should NOT
    # land in milestone any more.
    assert classify_signal_lane({"type": "path_of_legend_promotion"}) != "milestone"


def test_classify_signal_lane_clan_event_and_system():
    assert classify_signal_lane({"type": "member_join"}) == "clan_event"
    assert classify_signal_lane({"type": "capability_unlock"}) == "system"


def test_classify_signal_lane_leadership_audience():
    assert classify_signal_lane({"type": "inactive_members"}) == "leadership"
    assert classify_signal_lane({"type": "anything", "audience": "leadership"}) == "leadership"


def test_classify_signal_lane_unknown_falls_through():
    assert classify_signal_lane({"type": "thoroughly_unknown"}) == "unknown"


# ---------------------------------------------------------------------------
# Situation assembler
# ---------------------------------------------------------------------------

def _bundle(signals=None, war=None, clan=None):
    return heartbeat.HeartbeatTickResult(
        signals=signals or [],
        clan=clan or {},
        war=war or {},
    )


def test_build_situation_groups_signals_by_lane():
    bundle = _bundle(signals=[
        {"type": "war_battle_rank_change", "signal_key": "war:rank:1"},
        {"type": "battle_hot_streak", "tag": "#A", "signal_key": "streak:A"},
        {"type": "arena_change", "tag": "#B", "signal_key": "arena:B"},
        {"type": "member_join", "tag": "#C", "signal_key": "join:C"},
    ])
    with patch.object(signals_module.db, "list_channel_messages", return_value=[]), \
         patch("runtime.situation.db.list_channel_messages", return_value=[]), \
         patch("runtime.situation.build_situation_time", return_value={"phase": "battle"}):
        situation = build_situation(bundle)

    grouped = situation["signals_by_lane"]
    assert {sig["type"] for sig in grouped["war"]} == {"war_battle_rank_change"}
    assert {sig["type"] for sig in grouped["battle_mode"]} == {"battle_hot_streak"}
    assert {sig["type"] for sig in grouped["milestone"]} == {"arena_change"}
    assert {sig["type"] for sig in grouped["clan_event"]} == {"member_join"}


def test_build_situation_lists_hard_post_signals():
    bundle = _bundle(signals=[
        {"type": "member_join", "signal_key": "join:1", "tag": "#A", "name": "Alice"},
        {"type": "battle_hot_streak", "signal_key": "streak:1", "tag": "#A", "name": "Alice"},
    ])
    with patch("runtime.situation.db.list_channel_messages", return_value=[]), \
         patch("runtime.situation.build_situation_time", return_value=None):
        situation = build_situation(bundle)
    types = [hp["type"] for hp in situation["hard_post_signals"]]
    assert types == ["member_join"]
    # battle_hot_streak is NOT a hard floor — agent may choose silence on it.
    assert "battle_hot_streak" not in types


def test_build_situation_includes_channel_memory_for_each_lane_channel():
    bundle = _bundle(signals=[])
    with patch("runtime.situation.db.list_channel_messages", return_value=[]), \
         patch("runtime.situation.build_situation_time", return_value=None):
        situation = build_situation(bundle)
    assert set(situation["channel_memory"].keys()) >= set(CHANNEL_LANES.keys())


# ---------------------------------------------------------------------------
# Quiet-tick fast path
# ---------------------------------------------------------------------------

def test_situation_is_quiet_when_no_signals_and_no_clock_pressure():
    situation = {
        "_raw_signal_count": 0,
        "hard_post_signals": [],
        "time": {"phase": "practice", "hours_remaining_in_day": 18},
    }
    assert situation_is_quiet(situation) is True


def test_situation_is_not_quiet_when_signals_present():
    situation = {
        "_raw_signal_count": 1,
        "_noisy_signal_count": 1,
        "hard_post_signals": [],
        "time": {"phase": "practice", "hours_remaining_in_day": 18},
    }
    assert situation_is_quiet(situation) is False


def test_situation_is_quiet_when_only_optional_progression_signals():
    """A tick with nothing but badge_level_milestone signals is quiet — the
    agent virtually always skips them, so spending an LLM call is waste."""
    situation = {
        "_raw_signal_count": 3,
        "_noisy_signal_count": 0,
        "hard_post_signals": [],
        "time": {"phase": "practice", "hours_remaining_in_day": 18},
    }
    assert situation_is_quiet(situation) is True


def test_situation_is_not_quiet_with_hard_post_floor():
    situation = {
        "_raw_signal_count": 0,
        "hard_post_signals": [{"signal_key": "join:1", "type": "member_join"}],
        "time": None,
    }
    assert situation_is_quiet(situation) is False


def test_situation_is_not_quiet_within_one_hour_of_battle_deadline():
    situation = {
        "_raw_signal_count": 0,
        "hard_post_signals": [],
        "time": {"phase": "battle", "hours_remaining_in_day": 1},
    }
    assert situation_is_quiet(situation) is False


# ---------------------------------------------------------------------------
# Post-plan delivery: lane validation + hard-post fallback
# ---------------------------------------------------------------------------

def test_deliver_awareness_post_rejects_unknown_channel():
    post = {"channel": "ghost-channel", "leads_with": "war", "content": "x"}
    delivered = asyncio.run(signals_module._deliver_awareness_post(post, []))
    assert delivered is False


def test_deliver_awareness_post_rejects_lane_mismatch():
    # river-race lane only allows leads_with="war"
    post = {"channel": "river-race", "leads_with": "milestone", "content": "x"}
    delivered = asyncio.run(signals_module._deliver_awareness_post(post, []))
    assert delivered is False


def test_deliver_signal_group_via_awareness_falls_back_for_uncovered_hard_floor():
    """When the agent omits a hard-post-floor signal, we fall back to the
    legacy per-signal delivery path so coverage is guaranteed."""
    member_join = {
        "type": "member_join",
        "signal_key": "join:abc",
        "tag": "#ABC",
        "name": "Alice",
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # Agent emits an empty post plan — nothing covered.
    empty_plan = {"posts": [], "skipped_reason": "agent felt quiet"}

    with (
        patch("runtime.jobs._signals.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.situation.db.list_channel_messages", return_value=[]),
        patch("runtime.situation.build_situation_time", return_value=None),
        patch("runtime.situation.db.get_members_on_hot_streak", return_value=[]),
        patch("runtime.jobs._signals.elixir_agent.run_awareness_tick", return_value=empty_plan),
        patch("runtime.jobs._signals.db.record_awareness_tick"),
        patch(
            "runtime.jobs._signals._deliver_signal_group",
            new=AsyncMock(return_value=True),
        ) as mock_legacy,
    ):
        ok = asyncio.run(
            signals_module._deliver_signal_group_via_awareness([member_join], {}, {})
        )

    assert ok is True
    # Legacy path was invoked with just the uncovered hard-floor signal.
    mock_legacy.assert_awaited_once()
    args, _ = mock_legacy.await_args
    assert args[0] == [member_join]


def test_deliver_signal_group_via_awareness_skips_quiet_tick():
    """When situation is quiet, the agent is never called."""

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._signals.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.situation.db.list_channel_messages", return_value=[]),
        patch("runtime.situation.build_situation_time", return_value={"phase": "practice", "hours_remaining_in_day": 18}),
        patch("runtime.situation.db.get_members_on_hot_streak", return_value=[]),
        patch(
            "runtime.jobs._signals.elixir_agent.run_awareness_tick",
        ) as mock_agent,
    ):
        ok = asyncio.run(signals_module._deliver_signal_group_via_awareness([], {}, {}))

    assert ok is True
    mock_agent.assert_not_called()


def test_deliver_awareness_post_rejects_empty_covers_when_signals_present():
    """Empty covers_signal_keys is illegal when the tick had signals — the
    post must tie back to something the agent considered."""
    post = {"channel": "clan-events", "leads_with": "clan_event", "content": "x",
            "covers_signal_keys": []}
    delivered = asyncio.run(
        signals_module._deliver_awareness_post(post, [{"type": "member_join", "signal_key": "join:A"}])
    )
    assert delivered is False


def test_deliver_awareness_post_allows_empty_covers_when_no_signals():
    """Deadline-driven ticks with no signals may emit a post with empty covers."""
    post = {"channel": "clan-events", "leads_with": "clan_event", "content": "x",
            "covers_signal_keys": []}
    # This should not be rejected by the covers/audience gate. It may still be
    # rejected by later gates (channel not configured), but empty-covers alone
    # isn't the reason.
    with patch.object(signals_module, "_channel_config_by_key", side_effect=RuntimeError):
        delivered = asyncio.run(signals_module._deliver_awareness_post(post, []))
    assert delivered is False


def test_deliver_awareness_post_rejects_leadership_signal_on_public_channel():
    """A post that covers a leadership-only signal (e.g., inactive_members)
    must not ship to a public channel."""
    leadership_signal = {
        "type": "inactive_members",
        "signal_key": "inactive:2026-04-17",
        "members": [],
    }
    post = {
        "channel": "clan-events",
        "leads_with": "clan_event",
        "content": "leaking roster",
        "covers_signal_keys": ["inactive:2026-04-17"],
    }
    delivered = asyncio.run(
        signals_module._deliver_awareness_post(post, [leadership_signal])
    )
    assert delivered is False


def test_deliver_signal_group_via_awareness_marks_considered_skipped_non_hard_signals():
    """Non-hard signals the agent consciously skipped must be marked so they
    don't re-fire every tick (C1 in the v4.5 review)."""
    # arena_change is a non-hard milestone signal. The agent chooses silence.
    arena_signal = {
        "type": "arena_change",
        "signal_key": "arena:#ABC",
        "tag": "#ABC",
    }
    empty_plan = {"posts": [], "skipped_reason": "not worth a post"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._signals.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.situation.db.list_channel_messages", return_value=[]),
        patch("runtime.situation.build_situation_time", return_value=None),
        patch("runtime.situation.db.get_members_on_hot_streak", return_value=[]),
        patch("runtime.jobs._signals.elixir_agent.run_awareness_tick", return_value=empty_plan),
        patch("runtime.jobs._signals.db.record_awareness_tick"),
        patch(
            "runtime.jobs._signals._mark_signal_group_completed",
            new=AsyncMock(),
        ) as mock_mark,
    ):
        ok = asyncio.run(
            signals_module._deliver_signal_group_via_awareness([arena_signal], {}, {})
        )

    assert ok is True
    # The arena_change signal should have been marked as considered-skipped.
    mock_mark.assert_awaited()
    marked_signals = mock_mark.await_args_list[-1].args[0]
    assert arena_signal in marked_signals


def test_hard_post_signal_types_includes_join_and_capability():
    # Sanity: the floor set the agent contract relies on actually contains
    # the signals the rest of the codebase considers "must-post."
    assert "member_join" in HARD_POST_SIGNAL_TYPES
    assert "member_leave" in HARD_POST_SIGNAL_TYPES
    assert "capability_unlock" in HARD_POST_SIGNAL_TYPES
    assert "war_battle_rank_change" in HARD_POST_SIGNAL_TYPES
