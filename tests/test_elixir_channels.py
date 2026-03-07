"""Tests for channel-role routing in elixir.py."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import elixir


class _TypingContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DummyChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.type = "text"

    def typing(self):
        return _TypingContext()


def _make_message(channel_id, channel_name, content, *, mentions=None):
    author = SimpleNamespace(
        bot=False,
        id=123,
        name="jamie",
        display_name="Jamie",
        global_name=None,
    )
    return SimpleNamespace(
        author=author,
        channel=_DummyChannel(channel_id, channel_name),
        content=content,
        mentions=mentions or [],
        role_mentions=[],
        id=555,
        reply=AsyncMock(),
    )


def test_on_message_routes_interactive_channel_when_mentioned():
    message = _make_message(100, "member-chat", "<@999> how am I doing?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=True),
        patch("elixir._get_channel_behavior", return_value={
            "id": 100,
            "name": "#member-chat",
            "role": "interactive",
            "workflow": "interactive",
            "mention_required": True,
            "allow_proactive": False,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]) as mock_history,
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message"),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch("elixir.elixir_agent.respond_in_channel", return_value={"event_type": "channel_response", "content": "You look solid.", "summary": "solid"}) as mock_respond,
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    assert mock_respond.call_args.kwargs["workflow"] == "interactive"
    assert mock_respond.call_args.kwargs["proactive"] is False
    mock_history.assert_called_once_with("channel_user:100:123", elixir.CHANNEL_CONVERSATION_LIMIT)
    message.reply.assert_awaited_once_with("You look solid.")
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_on_message_routes_clanops_proactively_without_mention():
    message = _make_message(200, "clan-ops", "I think we need to review promotions this week.")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 200,
            "name": "#clan-ops",
            "role": "clanops",
            "workflow": "clanops",
            "mention_required": False,
            "allow_proactive": True,
        }),
        patch("elixir._clanops_cooldown_elapsed", return_value=True),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]) as mock_history,
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch("elixir.elixir_agent.respond_in_channel", return_value={"event_type": "channel_response", "content": "I can pull the current promotion candidates if you want.", "summary": "ops"}) as mock_respond,
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    assert mock_respond.call_args.kwargs["workflow"] == "clanops"
    assert mock_respond.call_args.kwargs["proactive"] is True
    mock_history.assert_called_once_with("channel_user:200:123", elixir.CHANNEL_CONVERSATION_LIMIT)
    assert mock_save.call_args_list[0].args[0] == "channel_user:200:123"
    message.reply.assert_awaited_once_with("I can pull the current promotion candidates if you want.")
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_on_message_handles_clanops_status_directly():
    message = _make_message(200, "clan-ops", "status")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 200,
            "name": "#clan-ops",
            "role": "clanops",
            "workflow": "clanops",
            "mention_required": False,
            "allow_proactive": True,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._build_status_report", return_value="**Elixir Status**\n- Build: `abc123`"),
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once_with("**Elixir Status**\n- Build: `abc123`")
    assert mock_save.call_count == 2
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_clanops_clan_status_directly():
    message = _make_message(200, "clan-ops", "clan status")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 200,
            "name": "#clan-ops",
            "role": "clanops",
            "workflow": "clanops",
            "mention_required": False,
            "allow_proactive": True,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS", "members": 21}, {"clans": [{}, {}, {}]}))) as mock_load,
        patch("elixir._build_clan_status_report", return_value="**POAP KINGS Status**\n- Roster: 21/50 members") as mock_build,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_load.assert_awaited_once()
    mock_build.assert_called_once_with({"name": "POAP KINGS", "members": 21}, {"clans": [{}, {}, {}]})
    message.reply.assert_awaited_once_with("**POAP KINGS Status**\n- Roster: 21/50 members")
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[1].kwargs["event_type"] == "clan_status_report"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_clanops_clan_status_short_directly():
    message = _make_message(200, "clan-ops", "clan status short")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 200,
            "name": "#clan-ops",
            "role": "clanops",
            "workflow": "clanops",
            "mention_required": False,
            "allow_proactive": True,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS", "members": 21}, {}))) as mock_load,
        patch("elixir._build_clan_status_short_report", return_value="**POAP KINGS Status (Short)**\n- Roster: 21/50") as mock_build,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_load.assert_awaited_once()
    mock_build.assert_called_once_with({"name": "POAP KINGS", "members": 21}, {})
    message.reply.assert_awaited_once_with("**POAP KINGS Status (Short)**\n- Roster: 21/50")
    assert mock_save.call_args_list[1].kwargs["event_type"] == "clan_status_short_report"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_clanops_help_directly():
    message = _make_message(200, "clan-ops", "help")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 200,
            "name": "#clan-ops",
            "role": "clanops",
            "workflow": "clanops",
            "mention_required": False,
            "allow_proactive": True,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once()
    assert "ClanOps" in message.reply.await_args.args[0]
    assert mock_save.call_args_list[1].kwargs["event_type"] == "clanops_help"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_interactive_help_directly():
    message = _make_message(100, "member-chat", "help")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=True),
        patch("elixir._get_channel_behavior", return_value={
            "id": 100,
            "name": "#member-chat",
            "role": "interactive",
            "workflow": "interactive",
            "mention_required": True,
            "allow_proactive": False,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once()
    assert "Interactive" in message.reply.await_args.args[0]
    assert mock_save.call_args_list[1].kwargs["event_type"] == "interactive_help"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_build_clan_status_report_summarizes_operational_clan_state():
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={
            "active_members": 21,
            "avg_exp_level": 60.5,
            "avg_trophies": 7523.4,
            "donations_week_total": 1340,
        }),
        patch("elixir.db.list_members", return_value=[
            {"name": "King Levy", "member_ref": "King Levy (@jamie)", "donations_week": 220, "trophies": 9000, "clan_rank": 1},
            {"name": "Finn", "member_ref": "Finn", "donations_week": 180, "trophies": 8500, "clan_rank": 2},
            {"name": "Vijay", "member_ref": "Vijay", "donations_week": 140, "trophies": 8100, "clan_rank": 3},
        ]),
        patch("elixir.db.get_current_war_status", return_value={
            "clan_name": "POAP KINGS",
            "season_id": 77,
            "week": 2,
            "war_state": "riverRace",
            "race_rank": 1,
            "fame": 12345,
            "repair_points": 120,
            "clan_score": 4560,
        }),
        patch("elixir.db.get_war_season_summary", return_value={
            "races": 2,
            "total_clan_fame": 23456,
            "fame_per_active_member": 1116.95,
            "top_contributors": [
                {"member_ref": "King Levy (@jamie)", "total_fame": 3200},
                {"member_ref": "Finn", "total_fame": 3100},
            ],
            "nonparticipants": [{"member_ref": "Vijay"}],
        }),
        patch("elixir.db.get_members_at_risk", return_value={"members": [{"member_ref": "Vijay"}]}),
        patch("elixir.db.get_members_on_losing_streak", return_value=[{"member_ref": "Finn", "current_streak": 3}]),
        patch("elixir.db.list_recent_joins", return_value=[{"member_ref": "New Guy"}]),
        patch("elixir.db.get_war_deck_status_today", return_value={
            "total_participants": 21,
            "used_all_4": [{}, {}],
            "used_some": [{}, {}, {}],
            "used_none": [{}, {}],
        }),
    ):
        report = elixir._build_clan_status_report(
            {"name": "POAP KINGS", "members": 21, "clanScore": 55555, "clanWarTrophies": 3210, "requiredTrophies": 5000, "donationsPerWeek": 1400},
            {"clans": [{}, {}, {}, {}, {}]},
        )

    assert report.startswith("**POAP KINGS Status**")
    assert "Roster: 21/50 members | 29 open" in report
    assert "weekly donations 1,400" in report
    assert "top donors King Levy (@jamie) 220, Finn 180, Vijay 140" in report
    assert "War now: season 77 | week 2 | state riverRace | rank 1" in report
    assert "Watch list: 1 with no war decks this season | 1 at risk | 1 on losing streaks | 1 joined in last 30d" in report
    assert "War today: 2 used all 4 decks | 3 used some | 2 unused" in report


def test_build_clan_status_short_report_is_compact():
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={
            "active_members": 21,
            "avg_exp_level": 60.5,
            "avg_trophies": 7523.4,
        }),
        patch("elixir.db.get_current_war_status", return_value={
            "clan_name": "POAP KINGS",
            "season_id": 77,
            "week": 2,
            "race_rank": 1,
            "fame": 12345,
        }),
        patch("elixir.db.get_war_season_summary", return_value={
            "fame_per_active_member": 1116.95,
            "top_contributors": [
                {"member_ref": "King Levy (@jamie)", "total_fame": 3200},
                {"member_ref": "Finn", "total_fame": 3100},
            ],
        }),
        patch("elixir.db.get_members_at_risk", return_value={"members": [{"member_ref": "Vijay"}]}),
        patch("elixir.db.get_members_on_losing_streak", return_value=[{"member_ref": "Finn", "current_streak": 3}]),
    ):
        report = elixir._build_clan_status_short_report({"name": "POAP KINGS", "members": 21}, {})

    assert report.startswith("**POAP KINGS Status (Short)**")
    assert "Roster: 21/50 | open 29" in report
    assert "War: season 77 | week 2 | rank 1 | fame 12,345" in report
    assert "Season: fame/member 1,117.0 | top King Levy (@jamie) 3,200, Finn 3,100" in report
    assert "Watch: 1 at risk | 1 slumping" in report


def test_build_weekly_clanops_review_tags_leaders_and_summarizes_actions():
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 21}),
        patch("elixir.db.get_promotion_candidates", return_value={
            "composition": {"elders": 5, "target_elder_min": 4, "target_elder_max": 6, "elder_capacity_remaining": 1},
            "recommended": [
                {"member_ref": "King Levy (@jamie)", "donations": 220, "war_races_played": 4, "tenure_days": 90, "days_inactive": 0},
            ],
            "borderline": [
                {"member_ref": "Finn", "donations": 120, "war_races_played": 2, "tenure_days": 20, "days_inactive": 1},
            ],
        }),
        patch("elixir.db.get_members_at_risk", return_value={
            "members": [
                {"member_ref": "Vijay", "reasons": [{"detail": "last seen 8 days ago"}, {"detail": "0 donations this week"}]},
            ]
        }),
        patch("elixir.db.get_war_season_summary", return_value={"nonparticipants": [{"member_ref": "Chanco"}]}),
    ):
        report = elixir._build_weekly_clanops_review(
            {"name": "POAP KINGS"},
            {"clan": {"fame": 12345, "repairPoints": 120, "clanScore": 4560}},
        )

    assert report.startswith(f"<@&{elixir.LEADER_ROLE_ID}>")
    assert "**Weekly ClanOps Review**" in report
    assert "Promote now (1): King Levy (@jamie) — 220 donations, 4 war races, 90d tenure, seen 0d ago" in report
    assert "Borderline (1): Finn — 120 donations, 2 war races, 20d tenure, seen 1d ago" in report
    assert "Demotion/kick watch (1): Vijay — last seen 8 days ago; 0 donations this week" in report
    assert "No war decks this season (1): Chanco" in report


def test_share_channel_result_tags_leader_role_for_arena_relay():
    channel = AsyncMock()
    channel.id = 300
    channel.name = "arena-relay"
    channel.type = "text"

    with (
        patch("elixir.prompts.resolve_channel_reference", return_value={"id": 300, "role": "arena_relay", "name": "#arena-relay"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message"),
    ):
        asyncio.run(
            elixir._share_channel_result(
                {"event_type": "channel_share", "share_content": "Relay this to clan chat.", "share_channel": "#arena-relay"},
                "clanops",
            )
        )

    mock_post.assert_awaited_once_with(channel, {"content": f"<@&{elixir.LEADER_ROLE_ID}>\nRelay this to clan chat."})
