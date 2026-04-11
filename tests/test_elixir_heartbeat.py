"""Tests for elixir heartbeat orchestration."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db
import heartbeat
import discord
import elixir
from runtime.channel_subagents import plan_signal_outcomes


def _publish_result(*content_types, sha="abc123def456", repo="jthingelstad/poapkings.com", branch="main"):
    return {
        "changed": True,
        "commit_sha": sha,
        "commit_url": f"https://github.com/{repo}/commit/{sha}",
        "repo": repo,
        "branch": branch,
        "changed_content_types": list(content_types),
        "changed_paths": [f"src/_data/elixir{content_type.title().replace('-', '')}.json" for content_type in content_types],
    }


def test_clan_awareness_tick_uses_bundle_without_refetch():
    """_clan_awareness_tick should use heartbeat.tick bundle and not refetch CR API."""
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{"type": "player_level_up", "name": "King Levy", "tag": "#ABC", "old_level": 65, "new_level": 66}],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.cr_api.get_clan") as mock_get_clan,
        patch("elixir.cr_api.get_current_war") as mock_get_war,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war)
    mock_get_clan.assert_not_called()
    mock_get_war.assert_not_called()


def test_clan_awareness_tick_reseeds_startup_system_signals_before_tick():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._core.queue_startup_system_signals") as mock_queue,
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.runtime_status.mark_job_success"),
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_queue.assert_called_once_with()


def test_detect_role_changes_emits_elder_promotion_signal():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        conn.execute(
            "UPDATE member_current_state SET observed_at = ? WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-03-01T10:00:00",),
        )
        conn.execute(
            "UPDATE member_state_snapshots SET observed_at = ? WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-03-01T10:00:00",),
        )
        conn.commit()

        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder"}],
            conn=conn,
        )

        signals = heartbeat.detect_role_changes(conn=conn)

        assert len(signals) == 1
        assert signals[0]["type"] == "elder_promotion"
        assert signals[0]["tag"] == "#ABC123"
        assert signals[0]["old_role"] == "member"
        assert signals[0]["new_role"] == "elder"
        assert signals[0]["signal_log_type"].startswith("role_change:#ABC123:member->elder:")
    finally:
        conn.close()


def test_detect_role_changes_ignores_demotion_from_elder():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder"}],
            conn=conn,
        )
        conn.execute(
            "UPDATE member_current_state SET observed_at = ? WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-03-01T10:00:00",),
        )
        conn.execute(
            "UPDATE member_state_snapshots SET observed_at = ? WHERE member_id = (SELECT member_id FROM members WHERE player_tag = '#ABC123')",
            ("2026-03-01T10:00:00",),
        )
        conn.commit()

        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )

        signals = heartbeat.detect_role_changes(conn=conn)

        assert signals == []
    finally:
        conn.close()


def test_war_awareness_tick_uses_stored_war_detection_bundle():
    bundle = heartbeat.WarAwarenessResult(
        signals=[{"type": "war_battle_day_live_update", "season_id": 129, "day_number": 1}],
        clan={"name": "POAP KINGS", "tag": "#J2RGCRVG"},
        war={"state": "warDay"},
        cursor_updates=[{"detector_key": "war_live_state_pipeline", "scope_key": "", "cursor_int": 12, "cursor_text": None, "metadata": {}}],
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.detect_war_signals_from_storage", return_value=bundle) as mock_detect,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock(return_value=True)) as mock_deliver,
        patch("runtime.jobs._core._persist_signal_detector_cursors") as mock_persist,
    ):
        asyncio.run(elixir._war_awareness_tick())

    mock_detect.assert_called_once_with()
    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war)
    mock_persist.assert_called_once_with(bundle.cursor_updates)


def test_war_poll_tick_uses_war_ingest_entrypoint():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.heartbeat.ingest_live_war_state", return_value={
            "war": {"state": "warDay"},
            "race_log_refreshed": True,
            "race_log_items": 1,
        }) as mock_ingest,
        patch("elixir._clear_cr_api_failure_alert_if_recovered") as mock_clear,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
    ):
        asyncio.run(elixir._war_poll_tick())

    mock_ingest.assert_called_once_with(refresh_race_log=True)
    mock_clear.assert_called_once_with()
    assert mock_success.call_args.args[0] == "war_poll"
    assert "war snapshot stored" in mock_success.call_args.args[1]


def test_war_awareness_tick_does_not_advance_cursors_when_delivery_fails():
    bundle = heartbeat.WarAwarenessResult(
        signals=[{"type": "war_battle_day_live_update", "season_id": 129, "day_number": 1}],
        clan={"name": "POAP KINGS", "tag": "#J2RGCRVG"},
        war={"state": "warDay"},
        cursor_updates=[{"detector_key": "war_live_state_pipeline", "scope_key": "", "cursor_int": 12, "cursor_text": None, "metadata": {}}],
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.detect_war_signals_from_storage", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock(return_value=False)),
        patch("runtime.jobs._core._persist_signal_detector_cursors") as mock_persist,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._war_awareness_tick())

    mock_persist.assert_not_called()
    assert mock_failure.call_args.args[0] == "war_awareness"


def test_heartbeat_tick_include_war_false_skips_live_war_fetch():
    conn = db.get_connection(":memory:")
    try:
        with (
            patch("heartbeat.cr_api.get_clan", return_value={"memberList": [{"tag": "#ABC", "name": "King Levy"}]}),
            patch("heartbeat.cr_api.get_current_war") as mock_get_war,
        ):
            heartbeat.tick(conn=conn, include_nonwar=False, include_war=False)

        mock_get_war.assert_not_called()
    finally:
        conn.close()


def test_deliver_signal_group_saves_multipart_channel_update_as_separate_messages():
    signals = [{"type": "war_week_rollover", "season_id": 130, "week": 1}]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    war = {"state": "warDay"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "river-race"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "war-week-1",
            "source_signal_type": "war_week_rollover",
            "target_channel_key": "river-race",
            "target_channel_id": "1482352067573059675",
            "intent": "war_update",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("elixir.db.mark_signal_sent"),
        patch("elixir.elixir_agent.generate_channel_update", return_value={
            "event_type": "channel_update",
            "summary": "War update",
            "content": ["First post", "Second post"],
        }),
        patch("elixir._post_to_elixir", new=AsyncMock()),
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    observation_saves = [
        call for call in mock_save.call_args_list
        if call.kwargs.get("event_type") in {"channel_update", "channel_update_part"}
    ]
    assert len(observation_saves) == 2
    assert observation_saves[0].args[2] == "First post"
    assert observation_saves[1].args[2] == "Second post"
    assert observation_saves[0].kwargs["event_type"] == "channel_update"
    assert observation_saves[1].kwargs["event_type"] == "channel_update_part"


def test_deliver_signal_group_posts_preauthored_system_signal_without_llm():
    signals = [{
        "type": "capability_unlock",
        "signal_key": "capability_card_modes_and_war_completion_v1",
        "payload": {
            "audience": "clan",
            "title": "Achievement Unlocked: Sharper Card And War Intel",
            "discord_content": "**Subject**\n\nPreauthored body.",
        },
    }]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    war = {"state": "training"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "announcements"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "capability_card_modes_and_war_completion_v1",
            "source_signal_type": "capability_unlock",
            "target_channel_key": "announcements",
            "target_channel_id": "1474760975851982959",
            "intent": "system_update",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("elixir.db.mark_signal_sent"),
        patch("elixir.db.mark_system_signal_announced"),
        patch("elixir.elixir_agent.generate_channel_update") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_generate.assert_not_called()
    mock_post.assert_awaited_once_with(
        channel,
        {
            "event_type": "channel_update",
            "summary": "Achievement Unlocked: Sharper Card And War Intel",
            "content": "**Subject**\n\nPreauthored body.",
        },
    )
    assert mock_save.call_args.args[2] == "**Subject**\n\nPreauthored body."


def test_deliver_signal_group_stores_war_recap_memory_for_river_race_batch():
    signals = [{"type": "war_battle_day_complete", "season_id": 129, "week": 2, "day_number": 1}]
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    war = {"state": "warDay"}

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.name = "river-race"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._signals.plan_signal_outcomes", return_value=[{
            "source_signal_key": "war-day-recap",
            "source_signal_type": "war_battle_day_complete",
            "target_channel_key": "river-race",
            "target_channel_id": "1482352067573059675",
            "intent": "war_update",
            "required": True,
            "payload": {"signals": signals},
            "delivery_status": "planned",
        }]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.get_signal_outcome", return_value=None),
        patch("elixir.db.upsert_signal_outcome"),
        patch("elixir.db.save_message"),
        patch("elixir.db.list_signal_outcomes", return_value=[{"delivery_status": "delivered"}]),
        patch("elixir.db.mark_signal_sent"),
        patch("elixir.elixir_agent.generate_channel_update", return_value={
            "event_type": "channel_update",
            "summary": "Battle day recap",
            "content": "Battle day recap post",
        }),
        patch("elixir._post_to_elixir", new=AsyncMock()),
        patch("runtime.jobs._signals.maybe_upsert_signal_memory"),
        patch("runtime.jobs._signals._store_recap_memories_for_signal_batch") as mock_memory,
    ):
        asyncio.run(elixir._deliver_signal_group(signals, clan, war))

    mock_memory.assert_called_once_with(signals, ["Battle day recap post"], 1482352067573059675)


def test_clan_awareness_tick_posts_join_messages_through_shared_sender():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{"type": "member_join", "name": "King Levy", "tag": "#ABC"}],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war)


def test_clan_awareness_tick_marks_non_system_signal_sent_after_successful_post():
    signals = [{"type": "war_week_rollover", "season_id": 130, "week": 1}]
    with patch("elixir.db.mark_signal_sent") as mock_mark_signal_sent:
        asyncio.run(elixir._mark_signal_group_completed(signals))
    mock_mark_signal_sent.assert_called_once_with("war_week_rollover", db.chicago_today())


def test_clan_awareness_tick_does_not_mark_non_system_signal_sent_when_post_fails():
    outcome_rows = [{"delivery_status": "failed"}]
    assert not all(row.get("delivery_status") in {"delivered", "skipped"} for row in outcome_rows)


def test_plan_signal_outcomes_routes_clan_audience_system_signal_to_announcements():
    outcomes = plan_signal_outcomes([{
        "type": "capability_unlock",
        "signal_key": "capability_boat_defense_intelligence_v1",
        "payload": {
            "title": "Achievement Unlocked: Boat Defense Intel",
            "audience": "clan",
        },
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "announcements"


def test_plan_signal_outcomes_routes_leadership_audience_system_signal_to_leader_lounge():
    outcomes = plan_signal_outcomes([{
        "type": "capability_unlock",
        "signal_key": "capability_private_ops_note_v1",
        "payload": {
            "title": "Achievement Unlocked: Ops Note",
            "audience": "leadership",
        },
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "leader-lounge"


def test_plan_signal_outcomes_makes_badge_level_only_batches_optional():
    outcomes = plan_signal_outcomes([{
        "type": "badge_level_milestone",
        "tag": "#ABC",
        "name": "King Levy",
        "badge_name": "MasteryKnight",
    }])

    assert len(outcomes) == 1
    assert outcomes[0]["target_channel_key"] == "player-progress"
    assert outcomes[0]["required"] is False


def test_plan_signal_outcomes_routes_live_finish_signal_to_war_channels():
    outcomes = plan_signal_outcomes([{
        "type": "war_race_finished_live",
        "season_id": 129,
        "section_index": 1,
        "week": 2,
    }])

    assert [(item["target_channel_key"], item["required"]) for item in outcomes] == [
        ("river-race", True),
        ("leader-lounge", False),
    ]


def test_clan_awareness_tick_does_not_mark_system_signal_sent_before_success():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{
            "type": "capability_unlock",
            "signal_key": "capability_memory_system_v1",
            "payload": {"title": "Achievement Unlocked: Stronger Memory"},
            "title": "Achievement Unlocked: Stronger Memory",
        }],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "training"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.db.mark_signal_sent") as mock_mark_signal_sent,
        patch("elixir.db.mark_system_signal_announced") as mock_mark_announced,
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock(return_value=False)) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_deliver.assert_awaited_once_with(bundle.signals, bundle.clan, bundle.war)
    mock_mark_signal_sent.assert_not_called()
    mock_mark_announced.assert_not_called()


def test_clan_awareness_tick_routes_multiple_system_signals_independently():
    bundle = heartbeat.HeartbeatTickResult(
        signals=[
            {
                "type": "capability_unlock",
                "signal_key": "capability_memory_system_v1",
                "payload": {"title": "Achievement Unlocked: Stronger Memory"},
            },
            {
                "type": "capability_unlock",
                "signal_key": "capability_battle_pulse_v1",
                "payload": {"title": "Achievement Unlocked: Battle Pulse"},
            },
        ],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "training"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    assert mock_deliver.await_count == 2


def test_weekly_clan_recap_syncs_members_page_payload_when_poap_kings_enabled():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 456
    channel.name = "announcements"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=456),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {"state": "training"}))),
        patch("elixir._build_weekly_clan_recap_context", return_value="summary context"),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.generate_weekly_digest", return_value="This week POAP KINGS pushed hard."),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message"),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value=_publish_result("members")) as mock_publish,
        patch("runtime.jobs._core._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
    ):
        asyncio.run(elixir._weekly_clan_recap())

    mock_post.assert_awaited_once()
    mock_publish.assert_called_once_with(
        {
            "members": {
                "title": "Weekly Recap",
                "message": "This week POAP KINGS pushed hard.",
                "generated": mock_publish.call_args.args[0]["members"]["generated"],
                "source": "weekly_clan_recap",
            }
        },
        "Elixir POAP KINGS weekly recap sync",
    )
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.args[0] == "weekly-recap"
    assert mock_notify.await_args.kwargs["publish_result"]["commit_url"].startswith("https://github.com/")


def test_detect_pending_system_signals_retries_until_announced():
    conn = db.get_connection(":memory:")
    try:
        db.queue_system_signal(
            "capability_memory_system_v1",
            "capability_unlock",
            {"title": "Achievement Unlocked: Stronger Memory"},
            conn=conn,
        )
        db.mark_signal_sent("system_signal::capability_memory_system_v1", "2026-03-10", conn=conn)

        signals = heartbeat.detect_pending_system_signals(today_str="2026-03-10", conn=conn)
    finally:
        conn.close()

    assert len(signals) == 1
    assert signals[0]["signal_key"] == "capability_memory_system_v1"


def test_player_intel_refresh_uses_refresh_targets_without_llm():
    """_player_intel_refresh should refresh stale members without touching the LLM."""
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war") as mock_get_war,
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members") as mock_snapshot_members,
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets) as mock_targets,
        patch("elixir.db.snapshot_player_profile") as mock_snapshot_profile,
        patch("elixir.db.snapshot_player_battlelog") as mock_snapshot_battlelog,
        patch("elixir.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_snapshot_members.assert_called_once_with(clan["memberList"])
    mock_get_war.assert_not_called()
    mock_targets.assert_called_once_with(elixir.PLAYER_INTEL_BATCH_SIZE, elixir.PLAYER_INTEL_STALE_HOURS)
    mock_snapshot_profile.assert_called_once()
    mock_snapshot_battlelog.assert_called_once_with("#ABC", [{"type": "PvP"}])
    mock_sleep.assert_awaited_once()


def test_player_intel_refresh_posts_progression_signals():
    """_player_intel_refresh should route progression signals through grouped outcome delivery."""
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[{"type": "player_level_up", "tag": "#ABC", "name": "King Levy", "old_level": 65, "new_level": 66}]),
        patch("elixir.db.snapshot_player_battlelog"),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_awaited_once()
    args = mock_deliver.await_args.args
    assert args[0][0]["type"] == "player_level_up"
    assert args[1] == clan


def test_player_intel_refresh_splits_optional_badge_level_batches():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[
            {"type": "player_level_up", "tag": "#ABC", "name": "King Levy", "old_level": 65, "new_level": 66},
            {"type": "badge_level_milestone", "tag": "#ABC", "name": "King Levy", "badge_name": "MasteryKnight"},
        ]),
        patch("elixir.db.snapshot_player_battlelog", return_value=[]),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    assert mock_deliver.await_count == 2
    first_batch = mock_deliver.await_args_list[0].args[0]
    second_batch = mock_deliver.await_args_list[1].args[0]
    assert [signal["type"] for signal in first_batch] == ["player_level_up"]
    assert [signal["type"] for signal in second_batch] == ["badge_level_milestone"]


def test_player_intel_refresh_posts_battle_pulse_signals():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[]),
        patch("elixir.db.snapshot_player_battlelog", return_value=[
            {"type": "battle_hot_streak", "tag": "#ABC", "name": "King Levy", "streak": 4},
            {"type": "battle_trophy_push", "tag": "#ABC", "name": "King Levy", "trophy_delta": 111},
        ]),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_awaited_once()
    signal_types = [signal["type"] for signal in mock_deliver.await_args.args[0]]
    assert signal_types == ["battle_hot_streak", "battle_trophy_push"]


def test_player_intel_refresh_does_not_post_baseline_profile_discovery():
    clan = {"memberList": [{"name": "royalkiller864", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "royalkiller864"}]
    first_profile = {
        "tag": "#ABC",
        "name": "royalkiller864",
        "currentDeck": [],
        "cards": [
            {"name": "Knight", "level": 1, "maxLevel": 16, "rarity": "common"},
            {"name": "Goblin Barrel", "level": 9, "maxLevel": 14, "rarity": "epic"},
        ],
        "badges": [
            {"name": "EmoteCollection", "level": 1, "maxLevel": 10, "progress": 10, "target": 25},
            {"name": "BattleWins", "level": 2, "maxLevel": 10, "progress": 20, "target": 50},
        ],
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value=first_profile),
        patch("elixir.cr_api.get_player_battle_log", return_value=[]),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[]),
        patch("elixir.db.snapshot_player_battlelog", return_value=[]),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_not_awaited()


def test_player_intel_refresh_marks_failure_when_all_player_endpoints_fail():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value=None),
        patch("elixir.cr_api.get_player_battle_log", return_value=None),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
        patch("elixir._maybe_alert_cr_api_failure", new=AsyncMock()) as mock_alert,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_not_awaited()
    mock_alert.assert_awaited_once_with("player intel refresh")
    mock_success.assert_not_called()
    assert mock_failure.call_args.args[0] == "player_intel_refresh"
    assert "refreshed 0 of 1 member(s)" in mock_failure.call_args.args[1]
    assert "profile failures 1" in mock_failure.call_args.args[1]
    assert "battle log failures 1" in mock_failure.call_args.args[1]
    assert "full target failures 1" in mock_failure.call_args.args[1]


def test_player_intel_refresh_reports_partial_endpoint_failures_without_hiding_success():
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=None),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_current_war_status", return_value={"state": "warDay"}),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[]),
        patch("runtime.jobs._core._deliver_signal_group", new=AsyncMock()) as mock_deliver,
        patch("elixir._maybe_alert_cr_api_failure", new=AsyncMock()) as mock_alert,
        patch("elixir.runtime_status.mark_job_success") as mock_success,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_deliver.assert_not_awaited()
    mock_alert.assert_awaited_once_with("player intel refresh")
    mock_failure.assert_not_called()
    assert mock_success.call_args.args[0] == "player_intel_refresh"
    assert "refreshed 1 of 1 member(s)" in mock_success.call_args.args[1]
    assert "battle log failures 1" in mock_success.call_args.args[1]


def test_clanops_weekly_review_posts_to_clanops_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 200
    channel.name = "leader-lounge"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.prompts.discord_channels_by_workflow", return_value=[{"id": 200, "name": "#leader-lounge", "subagent": "leader-lounge", "workflow": "clanops"}]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"state": "warDay"}))),
        patch("elixir._build_weekly_clanops_review", return_value="<@&1474762111287824584>\n**Weekly ClanOps Review**") as mock_build,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
        patch("runtime.jobs._core.upsert_weekly_summary_memory") as mock_memory,
    ):
        asyncio.run(elixir._clanops_weekly_review())

    mock_build.assert_called_once_with({"name": "POAP KINGS"}, {"state": "warDay"})
    mock_post.assert_awaited_once_with(channel, {"content": "<@&1474762111287824584>\n**Weekly ClanOps Review**"})
    assert mock_save.call_args.kwargs["event_type"] == "weekly_clanops_review"
    mock_memory.assert_called_once()
    assert mock_memory.call_args.kwargs["event_type"] == "weekly_clanops_review"
    assert mock_memory.call_args.kwargs["scope"] == "leadership"


def test_weekly_clan_recap_posts_to_weekly_digest_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 500
    channel.name = "announcements"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.poap_kings_site.site_enabled", return_value=False),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=500),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"state": "warDay"}))),
        patch("elixir._build_weekly_clan_recap_context", return_value="=== WEEKLY CLAN RECAP SNAPSHOT ===") as mock_build,
        patch("elixir.db.list_channel_messages", return_value=[{"role": "assistant", "content": "**Weekly Recap | March 4, 2026**\n\nlast week's recap"}]),
        patch("elixir.elixir_agent.generate_weekly_digest", return_value="This week POAP KINGS pushed hard.") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
        patch("runtime.jobs._core.upsert_weekly_summary_memory") as mock_memory,
    ):
        asyncio.run(elixir._weekly_clan_recap())

    mock_build.assert_called_once_with({"name": "POAP KINGS"}, {"state": "warDay"})
    mock_generate.assert_called_once_with("=== WEEKLY CLAN RECAP SNAPSHOT ===", "last week's recap")
    post_content = mock_post.await_args.args[1]["content"]
    assert post_content.startswith("**Weekly Recap | ")
    assert post_content.endswith("This week POAP KINGS pushed hard.")
    assert mock_save.call_args.kwargs["event_type"] == "weekly_clan_recap"
    mock_memory.assert_called_once()
    assert mock_memory.call_args.kwargs["event_type"] == "weekly_clan_recap"
    assert mock_memory.call_args.kwargs["scope"] == "public"


def test_format_weekly_recap_post_adds_subject_line_and_strips_existing_header():
    post = elixir._format_weekly_recap_post(
        "**Weekly Recap | March 4, 2026**\n\n**Clan momentum:** Strong week overall.",
        now=datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc),
    )

    assert post == "**Weekly Recap | March 11, 2026**\n\n**Clan momentum:** Strong week overall."


def test_weekly_clan_recap_marks_failure_when_channel_send_forbidden():
    channel = AsyncMock()
    channel.id = 123
    channel.name = "weekly-digest"
    channel.type = "text"
    channel.send = AsyncMock(side_effect=discord.Forbidden(response=SimpleNamespace(status=403, reason="Forbidden"), message="Missing Permissions"))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"state": "warDay"}))),
        patch("elixir._build_weekly_clan_recap_context", return_value="=== WEEKLY CLAN RECAP SNAPSHOT ==="),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.generate_weekly_digest", return_value="recap text"),
        patch("elixir.runtime_status.mark_job_start"),
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        try:
            asyncio.run(elixir._weekly_clan_recap())
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "weekly recap post failed: missing Discord permissions in #weekly-digest" == str(exc)

    mock_failure.assert_called_once_with("weekly_clan_recap", "missing Discord permissions in #weekly-digest")


def test_promotion_content_cycle_publishes_website_and_promotion_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}) as mock_roster,
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={
                "discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG [2000]", "body": "Recruiting body"},
            },
        ) as mock_generate,
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value=_publish_result("promote")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_roster.assert_called_once_with(clan, True)
    mock_generate.assert_called_once()
    mock_publish.assert_called_once_with(
        {
            "promote": {
                "discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG [2000]", "body": "Recruiting body"},
            }
        },
        "Elixir POAP KINGS promotion content update",
    )
    channel_posts = mock_post.await_args.args[1]["content"]
    assert len(channel_posts) == 2
    assert "Discord recruiting copy" in channel_posts[0]
    assert "Reddit recruiting copy" in channel_posts[1]
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.args[0] == "promotion-content"
    assert mock_notify.await_args.kwargs["publish_result"]["commit_sha"] == "abc123def456"
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[0].kwargs["event_type"] == "promotion_content_cycle"
    assert mock_save.call_args_list[1].kwargs["event_type"] == "promotion_content_cycle_part"


def test_promotion_content_cycle_fails_when_site_write_returns_false():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={"discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."}},
        ),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", side_effect=RuntimeError("GitHub publish failed")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_publish.assert_called_once_with(
        {"promote": {"discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."}}},
        "Elixir POAP KINGS promotion content update",
    )
    mock_post.assert_not_awaited()
    mock_notify.assert_awaited_once_with("promotion-content", error_detail="GitHub publish failed")
    failure_message = mock_failure.call_args.args[1]
    assert failure_message == "site publish failed: GitHub publish failed"


def test_promotion_content_cycle_fails_when_discord_header_misses_required_trophy_text():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={
                "discord": {"body": "**POAP KINGS is recruiting [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG [2000]", "body": "Recruiting body"},
            },
        ),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content") as mock_publish,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_publish.assert_not_called()
    mock_post.assert_not_awaited()
    assert mock_failure.call_args.args[1] == (
        "invalid promotion content: discord.body first line must include exact text "
        "`Required Trophies: [2000]`"
    )


def test_promotion_content_cycle_fails_when_reddit_title_misses_required_token():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 400
    channel.name = "promote-the-clan"
    channel.type = "text"
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("runtime.jobs._site._get_singleton_channel_id", return_value=400),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=(clan, {"state": "warDay"}))),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch(
            "elixir.elixir_agent.generate_promote_content",
            return_value={
                "discord": {"body": "**POAP KINGS is recruiting | Required Trophies: [2000]**\nJoin POAP KINGS this weekend."},
                "reddit": {"title": "POAP KINGS #J2RGCRVG", "body": "Recruiting body"},
            },
        ),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content") as mock_publish,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._promotion_content_cycle())

    mock_publish.assert_not_called()
    mock_post.assert_not_awaited()
    assert mock_failure.call_args.args[1] == "invalid promotion content: reddit.title must include exact token `[2000]`"

def test_site_data_refresh_fails_when_poap_kings_publish_raises():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", side_effect=RuntimeError("GitHub publish failed")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._site_data_refresh())

    mock_publish.assert_called_once_with(
        {"roster": {"members": [{"name": "King Levy"}]}, "clan": {"memberCount": 1}},
        "Elixir POAP KINGS site data refresh",
    )
    mock_notify.assert_awaited_once_with("site-data-refresh", error_detail="GitHub publish failed")
    assert mock_failure.call_args.args[1] == "GitHub publish failed"


def test_site_content_cycle_fails_when_daily_site_publish_raises():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy", "tag": "ABC"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.elixir_agent.generate_home_message", return_value="Home message"),
        patch("runtime.jobs._site.poap_kings_site.load_published", return_value=None),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", side_effect=RuntimeError("GitHub publish failed")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_failure") as mock_failure,
    ):
        asyncio.run(elixir._site_content_cycle())

    mock_publish.assert_called_once_with(
        {
            "roster": {"members": [{"name": "King Levy", "tag": "ABC"}]},
            "clan": {"memberCount": 1},
            "home": {"message": "Home message", "generated": mock_publish.call_args.args[0]["home"]["generated"]},
        },
        "Elixir POAP KINGS daily site sync",
    )
    mock_notify.assert_awaited_once_with("site-content", error_detail="GitHub publish failed")
    assert mock_failure.call_args.args[1] == "GitHub publish failed"


def test_notify_poapkings_publish_posts_success_to_poapkings_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 900
    channel.name = "poapkings-com"
    channel.type = "text"
    publish_result = _publish_result("home", sha="feedbeef1234567")

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._site._channel_config_by_key", return_value={
            "id": 900,
            "name": "#poapkings-com",
            "subagent_key": "poapkings-com",
            "memory_scope": "public",
        }),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.jobs._site.build_subagent_memory_context", return_value=""),
        patch("elixir.elixir_agent.generate_channel_update", return_value={
            "event_type": "channel_update",
            "summary": "POAP KINGS publish complete",
            "content": "Published `feedbee` https://github.com/jthingelstad/poapkings.com/commit/feedbeef1234567",
        }) as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        result = asyncio.run(elixir._notify_poapkings_publish("site-content", publish_result=publish_result))

    assert result is True
    mock_post.assert_awaited_once()
    assert "Commit SHA: feedbeef1234567" in mock_generate.call_args.args[2]
    assert "Commit URL: https://github.com/jthingelstad/poapkings.com/commit/feedbeef1234567" in mock_generate.call_args.args[2]
    assert mock_save.call_args.kwargs["workflow"] == "poapkings-com"
    assert mock_save.call_args.kwargs["event_type"] == "poapkings_publish_success"


def test_notify_poapkings_publish_skips_no_change_runs():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        result = asyncio.run(elixir._notify_poapkings_publish("site-content", publish_result={"changed": False}))

    assert result is False
    mock_post.assert_not_awaited()


def test_site_content_cycle_notifies_poapkings_channel_on_success():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy", "tag": "ABC"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.elixir_agent.generate_home_message", return_value="Home message"),
        patch("runtime.jobs._site.poap_kings_site.load_published", return_value=None),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value=_publish_result("roster", "clan", "home")) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_success"),
    ):
        asyncio.run(elixir._site_content_cycle())

    mock_publish.assert_called_once()
    mock_notify.assert_awaited_once()
    assert mock_notify.await_args.args[0] == "site-content"
    assert mock_notify.await_args.kwargs["publish_result"]["changed_content_types"] == ["roster", "clan", "home"]


def test_site_content_cycle_skips_poapkings_notification_when_no_change():
    clan = {
        "name": "POAP KINGS",
        "tag": "#J2RGCRVG",
        "memberList": [{"name": "King Levy", "tag": "#ABC"}],
    }

    with (
        patch("runtime.jobs._site.poap_kings_site.site_enabled", return_value=True),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.poap_kings_site.build_roster_data", return_value={"members": [{"name": "King Levy", "tag": "ABC"}]}),
        patch("elixir.poap_kings_site.build_clan_data", return_value={"memberCount": 1}),
        patch("elixir.elixir_agent.generate_home_message", return_value="Home message"),
        patch("runtime.jobs._site.poap_kings_site.load_published", return_value=None),
        patch("runtime.jobs._site.poap_kings_site.publish_site_content", return_value={"changed": False}) as mock_publish,
        patch("runtime.jobs._site._notify_poapkings_publish", new=AsyncMock()) as mock_notify,
        patch("elixir.runtime_status.mark_job_success"),
    ):
        asyncio.run(elixir._site_content_cycle())

    mock_publish.assert_called_once()
    mock_notify.assert_awaited_once_with(
        "site-content",
        publish_result={
            "changed": False,
            "commit_sha": None,
            "commit_url": None,
            "repo": None,
            "branch": None,
            "changed_content_types": [],
            "changed_paths": [],
        },
    )


def test_detect_cake_days_uses_effective_join_date_and_birthdays():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-03-08", conn=conn)
        db.set_member_birthday("#ABC123", "King Levy", 3, 8, conn=conn)

        signals = heartbeat.detect_cake_days("2026-03-08", conn=conn)

        join_signal = next(signal for signal in signals if signal["type"] == "join_anniversary")
        birthday_signal = next(signal for signal in signals if signal["type"] == "member_birthday")

        assert join_signal["members"] == [{
            "tag": "#ABC123",
            "name": "King Levy",
            "joined_date": "2024-03-08",
            "months": 24,
            "quarters": 8,
            "years": 2,
            "is_yearly": True,
        }]
        assert birthday_signal["members"] == [{
            "tag": "#ABC123",
            "name": "King Levy",
            "birth_month": 3,
            "birth_day": 8,
        }]
    finally:
        conn.close()


def test_detect_cake_days_emits_quarterly_join_milestone():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2025-12-08", conn=conn)

        signals = heartbeat.detect_cake_days("2026-03-08", conn=conn)
        join_signal = next(signal for signal in signals if signal["type"] == "join_anniversary")

        assert join_signal["members"] == [{
            "tag": "#ABC123",
            "name": "King Levy",
            "joined_date": "2025-12-08",
            "months": 3,
            "quarters": 1,
            "years": 0,
            "is_yearly": False,
        }]
    finally:
        conn.close()


def test_detect_cake_days_dedupes_announcements_for_the_day():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.set_member_join_date("#ABC123", "King Levy", "2024-03-08", conn=conn)
        db.set_member_birthday("#ABC123", "King Levy", 3, 8, conn=conn)

        first = heartbeat.detect_cake_days("2026-03-08", conn=conn)
        assert {signal["type"] for signal in first} >= {"join_anniversary", "member_birthday"}

        db.mark_announcement_sent("2026-03-08", "join_anniversary", "#ABC123", conn=conn)
        db.mark_announcement_sent("2026-03-08", "birthday", "#ABC123", conn=conn)
        second = heartbeat.detect_cake_days("2026-03-08", conn=conn)
        assert second == []
    finally:
        conn.close()


def test_clan_awareness_tick_marks_cake_day_announcements_after_successful_post():
    from runtime.jobs._signals import _mark_delivered_signals

    bundle = heartbeat.HeartbeatTickResult(
        signals=[{
            "type": "member_birthday",
            "members": [{"tag": "#ABC123", "name": "King Levy", "birth_month": 3, "birth_day": 8}],
        }],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC123"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def fake_deliver(signals, clan, war):
        _mark_delivered_signals(signals)
        return True

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.jobs._core.heartbeat.tick", return_value=bundle),
        patch("runtime.jobs._core._deliver_signal_group", side_effect=fake_deliver),
        patch("elixir.db.mark_signal_sent") as mock_mark_signal_sent,
        patch("elixir.db.mark_announcement_sent") as mock_mark_announcement_sent,
    ):
        asyncio.run(elixir._clan_awareness_tick())

    mock_mark_signal_sent.assert_called_once_with("member_birthday", db.chicago_today())
    mock_mark_announcement_sent.assert_called_once_with(db.chicago_today(), "birthday", "#ABC123")


def test_detect_war_rollovers_emits_week_rollover_for_new_live_week():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 2,
                        "createdDate": "20260222T120000.000Z",
                        "standings": [
                            {"rank": 1, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10000}}
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 2,
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 10000,
                    "repairPoints": 0,
                    "periodPoints": 1200,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 3,
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 250,
                    "repairPoints": 0,
                    "periodPoints": 250,
                    "clanScore": 141,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_rollovers(conn=conn)

        assert [sig["type"] for sig in signals] == ["war_week_rollover"]
        assert signals[0]["previous_week"] == 3
        assert signals[0]["week"] == 4
        assert signals[0]["season_id"] == 129
        assert signals[0]["season_changed"] is False
    finally:
        conn.close()


def test_detect_war_rollovers_emits_week_and_season_rollover_for_section_wrap():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 3,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {"rank": 1, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 12850}}
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 3,
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 12850,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 5,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 400,
                    "repairPoints": 0,
                    "periodPoints": 400,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_rollovers(conn=conn)

        assert [sig["type"] for sig in signals] == ["war_week_rollover", "war_season_rollover"]
        assert signals[0]["previous_week"] == 4
        assert signals[0]["week"] == 1
        assert signals[0]["previous_season_id"] == 129
        assert signals[0]["season_id"] == 130
        assert signals[0]["season_changed"] is True
        assert signals[1]["previous_season_id"] == 129
        assert signals[1]["season_id"] == 130
    finally:
        conn.close()


def test_detect_war_day_transition_emits_battle_phase_active_from_api_state():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:30:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 0,
                    "periodIndex": 3,
                    "periodType": "warDay",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 0,
                        "repairPoints": 0,
                        "periodPoints": 0,
                        "clanScore": 140,
                        "participants": [],
                    },
                },
                conn=conn,
            )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals == [{
            "type": "war_battle_phase_active",
            "signal_log_type": "war_battle_phase_active::slive-w00-p003",
            "signal_date": "2026-03-14",
            "season_id": None,
            "week": 1,
            "section_index": 0,
            "period_index": 3,
            "period_type": "warDay",
            "message": "Battle phase is live. Time to use those war decks.",
        }]
    finally:
        conn.close()


def test_detect_war_day_transition_emits_practice_phase_active_from_api_state():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:30:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 1,
                    "periodIndex": 1,
                    "periodType": "trainingDay",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 0,
                        "repairPoints": 0,
                        "periodPoints": 0,
                        "clanScore": 140,
                        "participants": [],
                    },
                },
                conn=conn,
            )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals == [{
            "type": "war_practice_phase_active",
            "signal_log_type": "war_practice_phase_active::slive-w01-p001",
            "signal_date": "2026-03-14",
            "season_id": None,
            "week": 2,
            "section_index": 1,
            "period_index": 1,
            "period_type": "trainingDay",
            "colosseum_week": False,
            "boat_defense_setup_scope": "one_time_per_practice_week",
            "boat_defense_tracking_available": False,
            "latest_clan_defense_status": None,
            "boat_defense_tracking_note": (
                "The live River Race API does not expose which members have placed "
                "boat defenses. It only exposes clan-level defense performance in "
                "period logs after days are logged."
            ),
            "message": (
                "Practice phase is live. Boat defenses are a one-time setup during "
                "practice days, so get them in early before battle days."
            ),
        }]
    finally:
        conn.close()


def test_detect_war_day_transition_marks_final_battle_day_from_api_period_index():
    conn = db.get_connection(":memory:")
    try:
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 5,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 3200,
                    "repairPoints": 0,
                    "periodPoints": 3200,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 6,
                "periodType": "warDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 6400,
                    "repairPoints": 0,
                    "periodPoints": 3200,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals[0]["type"] == "war_final_battle_day"
        assert signals[0]["week"] == 1
        assert signals[0]["period_index"] == 6
        assert db.get_current_war_status(conn=conn)["battle_day_number"] == 4
        assert db.get_current_war_status(conn=conn)["phase_display"] == "Battle Day 4"
        assert signals[0]["message"] == "Last day of battles this week. Use remaining decks!"
    finally:
        conn.close()


def test_detect_war_day_transition_includes_latest_clan_defense_status_when_available():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 0,
                        "createdDate": "20260301T120000.000Z",
                        "standings": [
                            {"rank": 1, "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 12850}}
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 1,
                "periodIndex": 7,
                "periodType": "training",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
                "periodLogs": [
                    {
                        "periodIndex": 6,
                        "items": [
                            {
                                "clan": {"tag": "#J2RGCRVG"},
                                "pointsEarned": 4200,
                                "progressStartOfDay": 3311,
                                "progressEndOfDay": 6622,
                                "endOfDayRank": 0,
                                "progressEarned": 3000,
                                "numOfDefensesRemaining": 7,
                                "progressEarnedFromDefenses": 311,
                            }
                        ],
                    }
                ],
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert signals[0]["type"] == "war_practice_phase_active"
        assert signals[0]["latest_clan_defense_status"]["num_defenses_remaining"] == 7
        assert signals[0]["latest_clan_defense_status"]["progress_earned_from_defenses"] == 311
        assert signals[0]["latest_clan_defense_status"]["phase_display"] == "Battle Day 4"
        assert signals[0]["latest_clan_defense_status"]["current_week_match"] is False
    finally:
        conn.close()


def test_detect_war_day_transition_marks_final_practice_day_from_api_period_index():
    conn = db.get_connection(":memory:")
    try:
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 1,
                "periodType": "trainingDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )
        db.upsert_war_current_state(
            {
                "state": "full",
                "sectionIndex": 0,
                "periodIndex": 2,
                "periodType": "trainingDay",
                "clan": {
                    "tag": "#J2RGCRVG",
                    "name": "POAP KINGS",
                    "fame": 0,
                    "repairPoints": 0,
                    "periodPoints": 0,
                    "clanScore": 140,
                    "participants": [],
                },
            },
            conn=conn,
        )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert [signal["type"] for signal in signals] == ["war_final_practice_day"]
        assert signals[0]["week"] == 1
        assert signals[0]["period_index"] == 2
        assert signals[0]["boat_defense_setup_scope"] == "one_time_per_practice_week"
        assert signals[0]["boat_defense_tracking_available"] is False
        assert signals[0]["latest_clan_defense_status"] is None
        assert signals[0]["boat_defense_tracking_note"] == (
            "The live River Race API does not expose which members have placed "
            "boat defenses. It only exposes clan-level defense performance in "
            "period logs after days are logged."
        )
        assert signals[0]["message"] == (
            "Last day of practice this week. Boat defenses are a one-time setup, "
            "so make sure they are set before battle days start."
        )
        assert db.get_current_war_status(conn=conn)["practice_day_number"] == 3
        assert db.get_current_war_status(conn=conn)["phase_display"] == "Practice Day 3"
    finally:
        conn.close()


def test_detect_war_day_transition_marks_battle_phase_complete_from_api_transition():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T09:30:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 0,
                    "periodIndex": 6,
                    "periodType": "warDay",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 6400,
                        "repairPoints": 0,
                        "periodPoints": 3200,
                        "clanScore": 140,
                        "participants": [],
                    },
                },
                conn=conn,
            )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:05:00"):
            db.upsert_war_current_state(
                {
                    "state": "full",
                    "sectionIndex": 1,
                    "periodIndex": 0,
                    "periodType": "trainingDay",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 0,
                        "repairPoints": 0,
                        "periodPoints": 0,
                        "clanScore": 141,
                        "participants": [],
                    },
                },
                conn=conn,
            )

        signals = heartbeat.detect_war_day_transition(conn=conn)

        assert [sig["type"] for sig in signals] == [
            "war_practice_phase_active",
            "war_battle_days_complete",
        ]
        assert signals[0]["week"] == 2
        assert signals[0]["period_type"] == "trainingDay"
        assert signals[1] == {
            "type": "war_battle_days_complete",
            "signal_log_type": "war_battle_days_complete::slive-w00-p006",
            "signal_date": "2026-03-13",
            "previous_season_id": None,
            "season_id": None,
            "previous_week": 1,
            "week": 2,
            "previous_period_type": "warDay",
            "period_type": "trainingDay",
            "message": "Battle phase has ended. River Race has moved out of battle days.",
        }
    finally:
        conn.close()


def test_detect_war_battle_checkpoints_waits_until_midday():
    with patch("heartbeat.db.get_current_war_day_state", return_value={
        "phase": "battle",
        "war_day_key": "s00130-w01-p010",
        "season_id": 130,
        "section_index": 1,
        "period_index": 10,
        "week": 1,
        "phase_display": "Battle Day 1",
        "day_number": 1,
        "day_total": 4,
        "time_left_seconds": 13 * 3600,
    }):
        signals = heartbeat.detect_war_battle_checkpoints()

    assert signals == []


def test_detect_war_battle_checkpoints_emits_midday_update():
    with (
        patch("heartbeat.db.get_current_war_day_state", return_value={
            "phase": "battle",
            "war_day_key": "s00130-w01-p010",
            "season_id": 130,
            "section_index": 1,
            "period_index": 10,
            "week": 1,
            "phase_display": "Battle Day 1",
            "day_number": 1,
            "day_total": 4,
            "race_rank": 2,
            "clan_fame": 4200,
            "clan_score": 180,
            "period_points": 4000,
            "time_left_seconds": 12 * 3600,
            "time_left_text": "12h 0m",
            "used_all_4": [{"name": "King Levy"}],
            "used_some": [{"name": "Finn"}],
            "used_none": [{"name": "Vijay"}],
            "top_fame_today": [{"name": "King Levy", "fame_today": 800}],
            "top_fame_total": [{"name": "King Levy", "fame": 1200}],
            "engaged_count": 2,
            "finished_count": 1,
            "untouched_count": 1,
            "total_participants": 3,
        }),
        patch("heartbeat.db.was_signal_sent", return_value=False),
    ):
        signals = heartbeat.detect_war_battle_checkpoints()

    assert len(signals) == 1
    assert signals[0]["type"] == "war_battle_day_live_update"
    assert signals[0]["signal_log_type"] == "war_battle_day_checkpoint::s00130-w01-p010::h12"
    assert signals[0]["checkpoint_hour"] == 12
    assert signals[0]["checkpoint_label"] == "midday check-in"
    assert signals[0]["checkpoint_hours_remaining"] == 12
    assert signals[0]["needs_lead_recovery"] is True
    assert signals[0]["lead_pressure"] == "high"
    assert "restore first place" in signals[0]["lead_call_to_action"]


def test_detect_war_battle_checkpoints_prefers_latest_reached_checkpoint():
    def was_signal_sent(signal_type, date_str, conn=None):
        del date_str, conn
        return signal_type.endswith("::h21")

    with (
        patch("heartbeat.db.get_current_war_day_state", return_value={
            "phase": "battle",
            "war_day_key": "s00130-w01-p010",
            "season_id": 130,
            "section_index": 1,
            "period_index": 10,
            "week": 1,
            "phase_display": "Battle Day 1",
            "day_number": 1,
            "day_total": 4,
            "race_rank": 3,
            "time_left_seconds": 5 * 3600,
            "time_left_text": "5h 0m",
        }),
        patch("heartbeat.db.was_signal_sent", side_effect=was_signal_sent),
    ):
        signals = heartbeat.detect_war_battle_checkpoints()

    assert len(signals) == 1
    assert signals[0]["type"] == "war_battle_day_live_update"
    assert signals[0]["signal_log_type"] == "war_battle_day_checkpoint::s00130-w01-p010::h18"
    assert signals[0]["checkpoint_hour"] == 18
    assert signals[0]["checkpoint_label"] == "late push"
    assert signals[0]["needs_lead_recovery"] is True
    assert signals[0]["lead_pressure"] == "high"


def test_detect_war_deck_usage_compatibility_wrapper_emits_final_push_checkpoint():
    with (
        patch("heartbeat.db.get_current_war_day_state", return_value={
            "phase": "battle",
            "war_day_key": "s00130-w01-p010",
            "season_id": 130,
            "section_index": 1,
            "period_index": 10,
            "week": 1,
            "phase_display": "Battle Day 1",
            "day_number": 1,
            "day_total": 4,
            "race_rank": 2,
            "time_left_seconds": 3 * 3600,
            "time_left_text": "3h 0m",
        }),
        patch("heartbeat.db.was_signal_sent", return_value=False),
    ):
        signals = heartbeat.detect_war_deck_usage({"state": "full"})

    assert len(signals) == 1
    assert signals[0]["type"] == "war_battle_day_final_hours"
    assert signals[0]["signal_log_type"] == "war_battle_day_checkpoint::s00130-w01-p010::h21"
    assert signals[0]["checkpoint_hour"] == 21
    assert signals[0]["checkpoint_label"] == "final push"
    assert signals[0]["needs_lead_recovery"] is True
    assert signals[0]["lead_pressure"] == "high"


def test_get_current_war_status_marks_live_finish_and_keeps_trophy_stakes_conservative():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T11:19:07"):
            db.upsert_war_current_state(
                {
                    "seasonId": 130,
                    "sectionIndex": 1,
                    "periodIndex": 13,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 10146,
                        "repairPoints": 0,
                        "periodPoints": 10146,
                        "clanScore": 160,
                        "finishTime": "20260315T095605.000Z",
                        "participants": [],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10146, "repairPoints": 0, "periodPoints": 10146, "clanScore": 160}],
                },
                conn=conn,
            )

        status = db.get_current_war_status(conn=conn)
    finally:
        conn.close()

    assert status["finish_time"] == "20260315T095605.000Z"
    assert status["race_completed"] is True
    assert status["race_completed_at"] == "2026-03-15T09:56:05"
    assert status["race_completed_early"] is True
    assert status["trophy_change"] is None
    assert status["trophy_stakes_known"] is False
    assert status["trophy_stakes_text"] is None


def test_get_current_war_status_ignores_sentinel_finish_time():
    conn = db.get_connection(":memory:")
    try:
        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T11:19:07"):
            db.upsert_war_current_state(
                {
                    "seasonId": 130,
                    "sectionIndex": 3,
                    "periodIndex": 13,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 5100,
                        "repairPoints": 0,
                        "periodPoints": 5100,
                        "clanScore": 160,
                        "finishTime": "19691231T235959.000Z",
                        "participants": [],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 5100, "repairPoints": 0, "periodPoints": 5100, "clanScore": 160}],
                },
                conn=conn,
            )

        status = db.get_current_war_status(conn=conn)
    finally:
        conn.close()

    assert status["finish_time"] is None
    assert status["race_completed"] is False
    assert status["race_completed_at"] is None


def test_get_current_war_status_surfaces_same_week_trophy_stakes_when_known():
    conn = db.get_connection(":memory:")
    try:
        db.store_war_log(
            {
                "items": [{
                    "seasonId": 130,
                    "sectionIndex": 1,
                    "createdDate": "20260315T095606.000Z",
                    "standings": [{
                        "rank": 1,
                        "trophyChange": 100,
                        "clan": {
                            "tag": "#J2RGCRVG",
                            "name": "POAP KINGS",
                            "fame": 10146,
                            "repairPoints": 0,
                            "finishTime": "20260315T095605.000Z",
                            "participants": [],
                        },
                    }],
                }],
            },
            "#J2RGCRVG",
            conn=conn,
        )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T11:19:07"):
            db.upsert_war_current_state(
                {
                    "seasonId": 130,
                    "sectionIndex": 1,
                    "periodIndex": 13,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 10146,
                        "repairPoints": 0,
                        "periodPoints": 10146,
                        "clanScore": 160,
                        "finishTime": "20260315T095605.000Z",
                        "participants": [],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10146, "repairPoints": 0, "periodPoints": 10146, "clanScore": 160}],
                },
                conn=conn,
            )

        status = db.get_current_war_status(conn=conn)
    finally:
        conn.close()

    assert status["trophy_change"] == 100
    assert status["trophy_stakes_known"] is True
    assert status["trophy_stakes_text"] == "100 trophies on the line"


def test_detect_war_signals_from_storage_seeds_forward_without_backfill():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader"}],
            conn=conn,
        )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
            db.upsert_war_current_state(
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "periodIndex": 10,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 500,
                        "repairPoints": 0,
                        "periodPoints": 500,
                        "clanScore": 150,
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 300, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 3},
                        ],
                    },
                    "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 500, "repairPoints": 0, "periodPoints": 500, "clanScore": 150}],
                },
                conn=conn,
            )

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    assert result.signals == []
    assert {update["detector_key"] for update in result.cursor_updates} == {
        heartbeat.WAR_LIVE_STATE_CURSOR_KEY,
        heartbeat.WAR_PARTICIPANT_CURSOR_KEY,
    }


def test_detect_war_signals_from_storage_replays_multiple_new_finishers_once():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
            ],
            conn=conn,
        )

        first_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 500,
                "repairPoints": 0,
                "periodPoints": 500,
                "clanScore": 150,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 300, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 3},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 200, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 500, "repairPoints": 0, "periodPoints": 500, "clanScore": 150}],
        }
        second_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 900,
                "repairPoints": 0,
                "periodPoints": 900,
                "clanScore": 155,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 300, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 900, "repairPoints": 0, "periodPoints": 900, "clanScore": 155}],
        }
        third_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 10,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 1300,
                "repairPoints": 0,
                "periodPoints": 1300,
                "clanScore": 160,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                    {"tag": "#DEF456", "name": "Vijay", "fame": 700, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 1300, "repairPoints": 0, "periodPoints": 1300, "clanScore": 160}],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
            db.upsert_war_current_state(first_payload, conn=conn)

        seed_result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in seed_result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T11:00:00"):
            db.upsert_war_current_state(second_payload, conn=conn)
        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T12:00:00"):
            db.upsert_war_current_state(third_payload, conn=conn)

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )
        rerun = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    all_deck_signals = [signal for signal in result.signals if signal["type"] == "war_member_used_all_decks"]
    assert len(all_deck_signals) == 0
    assert rerun.signals == []


def test_detect_war_signals_from_storage_emits_live_finish_and_suppresses_pressure_signals():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader"}],
            conn=conn,
        )

        seed_payload = {
            "seasonId": 130,
            "sectionIndex": 1,
            "periodIndex": 13,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 8400,
                "repairPoints": 0,
                "periodPoints": 8400,
                "clanScore": 158,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 2, "decksUsedToday": 2},
                ],
            },
            "clans": [
                {"tag": "#RIVAL1", "name": "Rivals", "fame": 8700, "repairPoints": 0, "periodPoints": 8700, "clanScore": 160},
                {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 8400, "repairPoints": 0, "periodPoints": 8400, "clanScore": 158},
            ],
        }
        finish_payload = {
            "seasonId": 130,
            "sectionIndex": 1,
            "periodIndex": 13,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 10146,
                "repairPoints": 0,
                "periodPoints": 10146,
                "clanScore": 160,
                "finishTime": "20260315T095605.000Z",
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 700, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 3, "decksUsedToday": 3},
                ],
            },
            "clans": [
                {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 10146, "repairPoints": 0, "periodPoints": 10146, "clanScore": 160},
                {"tag": "#RIVAL1", "name": "Rivals", "fame": 9900, "repairPoints": 0, "periodPoints": 9900, "clanScore": 159},
            ],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T20:00:00"):
            db.upsert_war_current_state(seed_payload, conn=conn)

        seed_result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in seed_result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-15T23:00:00"):
            db.upsert_war_current_state(finish_payload, conn=conn)

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    signal_types = {signal["type"] for signal in result.signals}
    assert "war_race_finished_live" in signal_types
    assert "war_battle_day_live_update" not in signal_types
    assert "war_battle_day_final_hours" not in signal_types
    assert "war_battle_rank_change" not in signal_types

    finish_signal = next(signal for signal in result.signals if signal["type"] == "war_race_finished_live")
    assert finish_signal["finish_time"] == "20260315T095605.000Z"
    assert finish_signal["race_completed_early"] is True


def test_battle_lead_payload_becomes_completion_aware_after_live_finish():
    payload = heartbeat._battle_lead_payload(
        2,
        war_state={
            "race_completed": True,
            "race_completed_at": "2026-03-15T09:56:05",
            "race_completed_early": True,
            "trophy_stakes_known": True,
            "trophy_stakes_text": "100 trophies on the line",
        },
    )

    assert payload["lead_pressure"] == "complete"
    assert payload["needs_lead_recovery"] is False
    assert "already finished" in payload["lead_story"]
    assert "clean closure" in payload["lead_call_to_action"]


def test_detect_war_signals_from_storage_replays_multiple_live_state_rows_once():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "leader"}],
            conn=conn,
        )

        seed_payload = {
            "seasonId": 129,
            "sectionIndex": 0,
            "periodIndex": 2,
            "periodType": "trainingDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 0,
                "repairPoints": 0,
                "periodPoints": 0,
                "clanScore": 140,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 0, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 0, "decksUsedToday": 0},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0, "repairPoints": 0, "periodPoints": 0, "clanScore": 140}],
        }
        battle_payload = {
            "seasonId": 129,
            "sectionIndex": 0,
            "periodIndex": 3,
            "periodType": "warDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 400,
                "repairPoints": 0,
                "periodPoints": 400,
                "clanScore": 141,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 400, "repairPoints": 0, "periodPoints": 400, "clanScore": 141}],
        }
        rollover_payload = {
            "seasonId": 129,
            "sectionIndex": 1,
            "periodIndex": 0,
            "periodType": "trainingDay",
            "state": "full",
            "clan": {
                "tag": "#J2RGCRVG",
                "name": "POAP KINGS",
                "fame": 0,
                "repairPoints": 0,
                "periodPoints": 0,
                "clanScore": 142,
                "participants": [
                    {"tag": "#ABC123", "name": "King Levy", "fame": 400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 0},
                ],
            },
            "clans": [{"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 0, "repairPoints": 0, "periodPoints": 0, "clanScore": 142}],
        }

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T09:00:00"):
            db.upsert_war_current_state(seed_payload, conn=conn)

        seed_result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in seed_result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )

        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:05:00"):
            db.upsert_war_current_state(battle_payload, conn=conn)
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:05:00"):
            db.upsert_war_current_state(rollover_payload, conn=conn)

        result = heartbeat.detect_war_signals_from_storage(conn=conn)
        for update in result.cursor_updates:
            db.upsert_signal_detector_cursor(
                update["detector_key"],
                update["scope_key"],
                cursor_text=update["cursor_text"],
                cursor_int=update["cursor_int"],
                metadata=update["metadata"],
                conn=conn,
            )
        rerun = heartbeat.detect_war_signals_from_storage(conn=conn)
    finally:
        conn.close()

    signal_types = [signal["type"] for signal in result.signals]
    assert "war_battle_phase_active" in signal_types
    assert "war_practice_phase_active" in signal_types
    assert "war_battle_days_complete" in signal_types
    assert "war_week_rollover" in signal_types
    assert rerun.signals == []


def test_detect_war_battle_checkpoints_respects_war_reset_date_before_utc_reset():
    conn = db.get_connection(":memory:")
    try:
        for signal_type in (
            "war_battle_day_checkpoint::s00130-w01-p010::h21",
            "war_battle_day_checkpoint::s00130-w01-p010::h18",
            "war_battle_day_checkpoint::s00130-w01-p010::h12",
        ):
            db.mark_signal_sent(signal_type, "2026-03-13", conn=conn)

        with patch("heartbeat.db.get_current_war_day_state", return_value={
            "phase": "battle",
            "war_day_key": "s00130-w01-p010",
            "season_id": 130,
            "section_index": 1,
            "period_index": 10,
            "week": 1,
            "phase_display": "Battle Day 1",
            "day_number": 1,
            "day_total": 4,
            "observed_at": "2026-03-14T08:30:00",
            "time_left_seconds": 90 * 60,
            "time_left_text": "1h 30m",
        }):
            signals = heartbeat.detect_war_battle_checkpoints(conn=conn)

        assert signals == []
    finally:
        conn.close()


def test_detect_war_day_markers_emits_day_start_and_prior_day_recap():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
            ],
            conn=conn,
        )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-13T10:00:00"):
            db.upsert_war_current_state(
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "periodIndex": 10,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 800,
                        "repairPoints": 0,
                        "periodPoints": 800,
                        "clanScore": 151,
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 4},
                            {"tag": "#DEF456", "name": "Vijay", "fame": 100, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 1},
                        ],
                    },
                        "clans": [
                            {"tag": "#OTHER1", "name": "Other Clan", "fame": 950, "repairPoints": 0, "periodPoints": 950, "clanScore": 160},
                            {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 800, "repairPoints": 0, "periodPoints": 800, "clanScore": 151},
                        ],
                },
                conn=conn,
            )
        with patch("storage.war_ingest._utcnow", return_value="2026-03-14T10:05:00"):
            db.upsert_war_current_state(
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "periodIndex": 11,
                    "periodType": "warDay",
                    "state": "full",
                    "clan": {
                        "tag": "#J2RGCRVG",
                        "name": "POAP KINGS",
                        "fame": 1200,
                        "repairPoints": 0,
                        "periodPoints": 400,
                        "clanScore": 152,
                        "participants": [
                            {"tag": "#ABC123", "name": "King Levy", "fame": 500, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                            {"tag": "#DEF456", "name": "Vijay", "fame": 100, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 1, "decksUsedToday": 0},
                        ],
                    },
                        "clans": [
                            {"tag": "#OTHER1", "name": "Other Clan", "fame": 1400, "repairPoints": 0, "periodPoints": 500, "clanScore": 165},
                            {"tag": "#J2RGCRVG", "name": "POAP KINGS", "fame": 1200, "repairPoints": 0, "periodPoints": 400, "clanScore": 152},
                        ],
                },
                conn=conn,
            )

        signals = heartbeat.detect_war_day_markers(conn=conn)

        assert [signal["type"] for signal in signals] == [
            "war_battle_day_started",
            "war_battle_day_complete",
        ]
        assert signals[0]["phase_display"] == "Battle Day 2"
        assert signals[0]["needs_lead_recovery"] is True
        assert signals[0]["lead_pressure"] == "high"
        assert signals[1]["phase_display"] == "Battle Day 1"
        assert signals[1]["finished_count"] == 1
        assert signals[1]["engaged_count"] == 2
        assert signals[1]["top_fame_today"][0]["name"] == "King Levy"
    finally:
        conn.close()


def test_detect_war_week_complete_enriches_completion_signal():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [
                {"tag": "#ABC123", "name": "King Levy", "role": "leader"},
                {"tag": "#DEF456", "name": "Vijay", "role": "member"},
            ],
            conn=conn,
        )
        db.store_war_log(
            {
                "items": [
                    {
                        "seasonId": 129,
                        "sectionIndex": 1,
                        "createdDate": "20260308T120000.000Z",
                        "standings": [
                            {
                                "rank": 1,
                                "trophyChange": 20,
                                "clan": {
                                    "tag": "#J2RGCRVG",
                                    "name": "POAP KINGS",
                                    "fame": 14000,
                                    "finishTime": "20260308T180000.000Z",
                                    "participants": [
                                        {"tag": "#ABC123", "name": "King Levy", "fame": 3600, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                        {"tag": "#DEF456", "name": "Vijay", "fame": 2400, "repairPoints": 0, "boatAttacks": 0, "decksUsed": 4, "decksUsedToday": 0},
                                    ],
                                },
                            }
                        ],
                    }
                ]
            },
            "J2RGCRVG",
            conn=conn,
        )

        signals = heartbeat.detect_war_week_complete(
            [{
                "type": "war_completed",
                "season_id": 129,
                "section_index": 1,
                "our_rank": 1,
                "our_fame": 14000,
                "total_clans": 5,
                "won": True,
            }],
            conn=conn,
        )

        assert signals[0]["type"] == "war_week_complete"
        assert signals[0]["week"] == 2
        assert signals[0]["week_summary"]["top_participants"][0]["name"] == "King Levy"
    finally:
        conn.close()


def test_detect_war_completion_retries_until_signal_is_marked():
    conn = db.get_connection(":memory:")
    try:
        race_log = {
            "items": [
                {
                    "seasonId": 129,
                    "sectionIndex": 1,
                    "createdDate": "20260308T120000.000Z",
                    "standings": [
                        {
                            "rank": 1,
                            "trophyChange": 20,
                            "clan": {
                                "tag": "#J2RGCRVG",
                                "name": "POAP KINGS",
                                "fame": 14000,
                                "finishTime": "20260308T180000.000Z",
                                "participants": [],
                            },
                        }
                    ],
                }
            ]
        }

        with patch("heartbeat.cr_api.get_river_race_log", return_value=race_log):
            first = heartbeat.detect_war_completion("J2RGCRVG", conn=conn)
            second = heartbeat.detect_war_completion("J2RGCRVG", conn=conn)

        assert len(first) == 1
        assert first[0]["type"] == "war_completed"
        assert first[0]["signal_log_type"] == "war_completed::129:1"
        assert len(second) == 1

        db.mark_signal_sent("war_completed::129:1", db.chicago_today(), conn=conn)

        with patch("heartbeat.cr_api.get_river_race_log", return_value=race_log):
            third = heartbeat.detect_war_completion("J2RGCRVG", conn=conn)

        assert third == []
    finally:
        conn.close()


def test_observation_signal_batches_group_completion_family_together():
    batches = elixir._observation_signal_batches(
        [
            {"type": "war_completed", "season_id": 129, "section_index": 1},
            {"type": "war_week_complete", "season_id": 129, "section_index": 1},
            {"type": "war_champ_standings", "season_id": 129, "section_index": 1},
            {"type": "war_battle_day_complete", "season_id": 129, "day_number": 1},
        ]
    )

    assert len(batches) == 2
    assert [signal["type"] for signal in batches[0]] == ["war_battle_day_complete"]
    assert [signal["type"] for signal in batches[1]] == [
        "war_completed",
        "war_week_complete",
        "war_champ_standings",
    ]


def test_observation_signal_batches_merge_day_transition():
    """Complete + started signals with same season/week merge into one batch."""
    batches = elixir._observation_signal_batches(
        [
            {"type": "war_battle_day_complete", "season_id": 131, "week": 3, "day_number": 2},
            {"type": "war_battle_day_started", "season_id": 131, "week": 3, "day_number": 3},
        ]
    )
    assert len(batches) == 1
    assert [s["type"] for s in batches[0]] == [
        "war_battle_day_complete",
        "war_battle_day_started",
    ]


def test_observation_signal_batches_merge_phase_transition():
    """Practice complete + battle started merge (phase transition within same week)."""
    batches = elixir._observation_signal_batches(
        [
            {"type": "war_practice_day_complete", "season_id": 131, "week": 3, "day_number": 3},
            {"type": "war_battle_day_started", "season_id": 131, "week": 3, "day_number": 1},
        ]
    )
    assert len(batches) == 1
    assert [s["type"] for s in batches[0]] == [
        "war_practice_day_complete",
        "war_battle_day_started",
    ]


def test_observation_signal_batches_no_merge_different_weeks():
    """Complete and started from different weeks stay separate."""
    batches = elixir._observation_signal_batches(
        [
            {"type": "war_battle_day_complete", "season_id": 131, "week": 2, "day_number": 4},
            {"type": "war_battle_day_started", "season_id": 131, "week": 3, "day_number": 1},
        ]
    )
    assert len(batches) == 2


def test_observation_signal_batches_rank_change_stays_separate():
    """Rank change signal is not merged with day transition."""
    batches = elixir._observation_signal_batches(
        [
            {"type": "war_battle_day_complete", "season_id": 131, "week": 3, "day_number": 2},
            {"type": "war_battle_day_started", "season_id": 131, "week": 3, "day_number": 3},
            {"type": "war_battle_rank_change", "season_id": 131, "week": 3},
        ]
    )
    assert len(batches) == 2
    merged = [b for b in batches if len(b) == 2][0]
    assert [s["type"] for s in merged] == [
        "war_battle_day_complete",
        "war_battle_day_started",
    ]
    solo = [b for b in batches if len(b) == 1][0]
    assert solo[0]["type"] == "war_battle_rank_change"
