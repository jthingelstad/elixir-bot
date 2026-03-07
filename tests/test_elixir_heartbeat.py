"""Tests for elixir heartbeat orchestration."""

import asyncio
from unittest.mock import AsyncMock, patch

import heartbeat
import elixir


def test_heartbeat_tick_uses_bundle_without_refetch():
    """_heartbeat_tick should use heartbeat.tick bundle and not refetch CR API."""
    bundle = heartbeat.HeartbeatTickResult(
        signals=[{"type": "trophy_milestone", "name": "King Levy", "tag": "#ABC"}],
        clan={"memberList": [{"name": "King Levy", "tag": "#ABC"}]},
        war={"state": "warDay"},
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()

    with (
        patch.object(elixir, "HEARTBEAT_START_HOUR", 0),
        patch.object(elixir, "HEARTBEAT_END_HOUR", 24),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.heartbeat.tick", return_value=bundle),
        patch("elixir.cr_api.get_clan") as mock_get_clan,
        patch("elixir.cr_api.get_current_war") as mock_get_war,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={"channel": {"state": None, "episodes": []}}),
        patch("elixir.elixir_agent.observe_and_post", return_value={"content": "msg", "summary": "s"}) as mock_observe,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        asyncio.run(elixir._heartbeat_tick())

    mock_observe.assert_called_once_with(bundle.clan, bundle.war, bundle.signals, [], {"channel": {"state": None, "episodes": []}})
    mock_post.assert_awaited_once()
    mock_get_clan.assert_not_called()
    mock_get_war.assert_not_called()


def test_player_intel_refresh_uses_refresh_targets_without_llm():
    """_player_intel_refresh should refresh stale members without touching the LLM."""
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir.db.snapshot_members") as mock_snapshot_members,
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets) as mock_targets,
        patch("elixir.db.snapshot_player_profile") as mock_snapshot_profile,
        patch("elixir.db.snapshot_player_battlelog") as mock_snapshot_battlelog,
        patch("elixir.asyncio.sleep", new=AsyncMock()) as mock_sleep,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_snapshot_members.assert_called_once_with(clan["memberList"])
    mock_targets.assert_called_once_with(elixir.PLAYER_INTEL_BATCH_SIZE, elixir.PLAYER_INTEL_STALE_HOURS)
    mock_snapshot_profile.assert_called_once()
    mock_snapshot_battlelog.assert_called_once_with("#ABC", [{"type": "PvP"}])
    mock_sleep.assert_awaited_once()


def test_player_intel_refresh_posts_progression_signals():
    """_player_intel_refresh should route profile progression signals through observation posting."""
    clan = {"memberList": [{"name": "King Levy", "tag": "#ABC"}]}
    targets = [{"tag": "#ABC", "name": "King Levy"}]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.asyncio.sleep", new=AsyncMock()),
        patch("elixir.cr_api.get_clan", return_value=clan),
        patch("elixir.cr_api.get_current_war", return_value={"state": "warDay"}),
        patch("elixir.cr_api.get_player", return_value={"tag": "#ABC", "name": "King Levy"}),
        patch("elixir.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.snapshot_members"),
        patch("elixir.db.get_player_intel_refresh_targets", return_value=targets),
        patch("elixir.db.snapshot_player_profile", return_value=[{"type": "player_level_up", "tag": "#ABC", "name": "King Levy", "old_level": 65, "new_level": 66}]),
        patch("elixir.db.snapshot_player_battlelog"),
        patch("elixir.db.upsert_war_current_state"),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={"channel": {"state": None, "episodes": []}}),
        patch("elixir.db.save_message"),
        patch("elixir.elixir_agent.observe_and_post", return_value={"content": "level up post", "summary": "s"}) as mock_observe,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        asyncio.run(elixir._player_intel_refresh())

    mock_observe.assert_called_once()
    args = mock_observe.call_args.args
    assert args[0] == clan
    assert args[2][0]["type"] == "player_level_up"
    mock_post.assert_awaited_once()


def test_clanops_weekly_review_posts_to_clanops_channel():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = AsyncMock()
    channel.id = 200
    channel.name = "leader-lounge"
    channel.type = "text"

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.prompts.discord_channels_by_role", return_value=[{"id": 200, "name": "#leader-lounge", "role": "clanops"}]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"state": "warDay"}))),
        patch("elixir._build_weekly_clanops_review", return_value="<@&1474762111287824584>\n**Weekly ClanOps Review**") as mock_build,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        asyncio.run(elixir._clanops_weekly_review())

    mock_build.assert_called_once_with({"name": "POAP KINGS"}, {"state": "warDay"})
    mock_post.assert_awaited_once_with(channel, {"content": "<@&1474762111287824584>\n**Weekly ClanOps Review**"})
    assert mock_save.call_args.kwargs["event_type"] == "weekly_clanops_review"
