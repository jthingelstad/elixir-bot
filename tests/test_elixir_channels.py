"""Tests for channel-role routing in elixir.py."""

import asyncio
from datetime import datetime, timedelta
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


def test_post_to_elixir_sends_content_list_as_multiple_messages():
    channel = SimpleNamespace(send=AsyncMock())

    asyncio.run(elixir._post_to_elixir(channel, {"content": ["First post", "Second post"]}))

    assert channel.send.await_args_list[0].args == ("First post",)
    assert channel.send.await_args_list[1].args == ("Second post",)


def test_on_message_replies_with_fallback_when_channel_agent_returns_none():
    message = _make_message(200, "clan-ops", "<@999> What is my current war participation rate over the last 4 weeks?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=True),
        patch("elixir._get_channel_behavior", return_value={
            "id": 200,
            "name": "#clan-ops",
            "role": "clanops",
            "workflow": "clanops",
            "mention_required": False,
            "allow_proactive": True,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message"),
        patch("elixir.db.record_prompt_failure", return_value=17) as mock_failure,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch("elixir.elixir_agent.respond_in_channel", return_value=None),
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
        patch("elixir.runtime_status.snapshot", return_value={
            "openai": {
                "last_error": "Error code: 429 rate_limit_exceeded",
                "last_model": "gpt-4.1-mini",
                "last_call_at": "2026-03-07T19:12:00",
            }
        }),
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once_with(
        "I don't have enough recent war participation data to answer that reliably yet."
    )
    mock_failure.assert_called_once_with(
        "What is my current war participation rate over the last 4 weeks?",
        "agent_none",
        "respond_in_channel",
        workflow="clanops",
        channel_id=200,
        channel_name="clan-ops",
        discord_user_id=123,
        discord_message_id=555,
        detail=None,
        result_preview=None,
        openai_last_error="Error code: 429 rate_limit_exceeded",
        openai_last_model="gpt-4.1-mini",
        openai_last_call_at="2026-03-07T19:12:00",
        raw_json=None,
    )
    mock_share.assert_not_awaited()
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


def test_on_message_handles_explicit_member_deck_request_without_llm():
    message = _make_message(200, "clan-ops", "<@999> what cards are in @Vijay deck?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=True),
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
        patch("elixir.db.resolve_member", return_value=[{
            "player_tag": "#DEF456",
            "current_name": "Vijay",
            "member_ref": "Vijay",
            "member_ref_with_handle": "Vijay (<@456>)",
            "match_score": 850,
            "match_source": "discord_display_exact",
        }]) as mock_resolve,
        patch("elixir.db.get_member_current_deck", return_value={
            "fetched_at": "2026-03-07T12:00:00",
            "cards": [
                {"name": "Knight", "level": 16},
                {"name": "Fireball", "level": 16},
            ],
        }),
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_resolve.assert_called_once_with("@Vijay", limit=3)
    message.reply.assert_awaited_once_with(
        "**Current Deck for Vijay (<@456>)**\n"
        "- Knight — Level 16\n"
        "- Fireball — Level 16\n"
        "_Snapshot: 2026-03-07 06:00 AM CT_"
    )
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[1].kwargs["event_type"] == "member_deck_report"
    mock_respond.assert_not_called()
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


def test_on_message_handles_clanops_schedule_directly():
    message = _make_message(200, "clan-ops", "schedule")

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
        patch("elixir._build_schedule_report", return_value="**Elixir Schedule**\n- `heartbeat`: Every hour.") as mock_build,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_build.assert_called_once_with()
    message.reply.assert_awaited_once_with("**Elixir Schedule**\n- `heartbeat`: Every hour.")
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[1].kwargs["event_type"] == "schedule_report"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_clanops_admin_command_directly():
    message = _make_message(200, "clan-ops", "do heartbeat --preview")

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
        patch("elixir.dispatch_admin_command", new=AsyncMock(return_value="Ran `heartbeat` in preview mode.")) as mock_admin,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_admin.assert_awaited_once_with("heartbeat", preview=True, short=False)
    message.reply.assert_awaited_once_with("Ran `heartbeat` in preview mode.")
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[1].kwargs["event_type"] == "clanops_admin_heartbeat_preview"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_build_schedule_report_shows_47_minute_heartbeat():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch.object(elixir, "HEARTBEAT_INTERVAL_MINUTES", 47),
        patch.object(elixir, "HEARTBEAT_JITTER_SECONDS", 300),
    ):
        report = elixir._build_schedule_report()

    assert "Every 47 minutes with up to 300s jitter." in report


def test_build_schedule_report_includes_promotion_content_sync():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch.object(elixir, "PROMOTION_CONTENT_DAY", "fri"),
        patch.object(elixir, "PROMOTION_CONTENT_HOUR", 9),
    ):
        report = elixir._build_schedule_report()

    assert "promotion content sync" in report
    assert "Every Fri at 09:00 CT." in report


def test_build_status_report_omits_job_schedule_section():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch("elixir.runtime_status.snapshot", return_value={
            "started_at": "2026-03-08T10:00:00",
            "env": {
                "has_discord_token": True,
                "has_openai_api_key": True,
                "has_cr_api_key": True,
            },
            "api": {
                "last_ok": True,
                "last_endpoint": "clan",
                "last_entity_key": "J2RGCRVG",
                "last_call_at": "2026-03-08T10:30:00",
                "last_status_code": 200,
                "last_duration_ms": 125,
                "call_count": 10,
                "error_count": 0,
            },
            "openai": {
                "last_ok": True,
                "last_workflow": "observation",
                "last_model": "gpt-4o",
                "last_call_at": "2026-03-08T10:29:00",
                "last_duration_ms": 500,
                "last_prompt_tokens": 100,
                "last_completion_tokens": 50,
                "last_total_tokens": 150,
                "call_count": 3,
                "error_count": 0,
            },
            "jobs": {
                "heartbeat": {"last_summary": "ok"},
            },
        }),
        patch("elixir.db.get_system_status", return_value={
            "db_path": "/tmp/elixir.db",
            "db_size_bytes": 1024,
            "schema_display": "V2 baseline (migration v2)",
            "schema_version": 2,
            "roster_summary": {"active_members": 21},
            "freshness": {
                "member_state_at": "2026-03-08T10:00:00",
                "player_profile_at": "2026-03-08T09:00:00",
                "battle_fact_at": "2026-03-08T08:00:00",
                "war_state_at": "2026-03-08T10:30:00",
            },
            "counts": {
                "raw_payload_count": 10,
                "battle_fact_count": 20,
                "message_count": 30,
                "discord_links": 5,
            },
            "latest_raw_payload": {
                "endpoint": "currentriverrace",
                "fetched_at": "2026-03-08T10:30:00",
            },
            "raw_payloads_by_endpoint": [
                {"endpoint": "currentriverrace", "count": 5},
            ],
            "stale_player_intel_targets": 2,
            "latest_signal": {
                "signal_type": "war_week_rollover",
                "signal_date": "2026-03-08",
            },
            "current_season_id": 130,
        }),
    ):
        report = elixir._build_status_report()

    assert "🛠️ Jobs:" not in report
    assert "Current war season id: 130" in report


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


def test_on_message_handles_roster_join_dates_directly():
    message = _make_message(200, "clan-ops", "Who are the members of the clan and when did they join?")

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
        patch(
            "elixir._build_roster_join_dates_report",
            return_value="**Clan Roster + Join Dates**\n1. King Levy (coLeader) — joined 2024-01-15",
        ) as mock_build,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_build.assert_called_once_with()
    message.reply.assert_awaited_once_with(
        "**Clan Roster + Join Dates**\n1. King Levy (coLeader) — joined 2024-01-15"
    )
    assert mock_save.call_args_list[1].kwargs["event_type"] == "roster_join_dates_report"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_kick_risk_directly():
    message = _make_message(200, "clan-ops", "Who is at risk of being kicked based on participation thresholds?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=True),
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
        patch(
            "elixir._build_kick_risk_report",
            return_value="**Kick Risk (Inactive 7+ Days)**\n- Vijay — last seen 8 days ago",
        ) as mock_build,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_build.assert_called_once_with()
    message.reply.assert_awaited_once_with(
        "**Kick Risk (Inactive 7+ Days)**\n- Vijay — last seen 8 days ago"
    )
    assert mock_save.call_args_list[1].kwargs["event_type"] == "kick_risk_report"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_top_war_contributors_directly():
    message = _make_message(200, "clan-ops", "Who are the top 5 contributors to clan wars this season?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=True),
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
        patch(
            "elixir._build_top_war_contributors_report",
            return_value="**Top War Contributors (Season 130)**\n1. King Levy — 3,200 fame across 4 race(s)",
        ) as mock_build,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_build.assert_called_once_with()
    message.reply.assert_awaited_once_with(
        "**Top War Contributors (Season 130)**\n1. King Levy — 3,200 fame across 4 race(s)"
    )
    assert mock_save.call_args_list[1].kwargs["event_type"] == "top_war_contributors_report"
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
            {"name": "King Levy", "member_ref": "King Levy (<@1474760692992180429>)", "donations_week": 220, "trophies": 9000, "clan_rank": 1},
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
                {"member_ref": "King Levy (<@1474760692992180429>)", "total_fame": 3200},
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
    assert "top donors King Levy (<@1474760692992180429>) 220, Finn 180, Vijay 140" in report
    assert "War now: season 77 | week 2 | state riverRace | rank 1" in report
    assert "Watch list: 1 with no war decks this season | 1 at risk | 1 on cold streaks | 1 joined in last 30d" in report
    assert "War today: 2 used all 4 decks | 3 used some | 2 unused" in report
    assert "Recent joins: New Guy (join timing unknown)" in report
    assert "Cold streaks: Finn lost 3 straight" in report


def test_build_clan_status_report_uses_non_war_risk_watchlist():
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 21, "avg_exp_level": 60.5, "avg_trophies": 7523.4}),
        patch("elixir.db.list_members", return_value=[]),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}) as mock_risk,
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
        patch("elixir.db.list_recent_joins", return_value=[]),
        patch("elixir.db.get_war_deck_status_today", return_value={}),
    ):
        elixir._build_clan_status_report({"name": "POAP KINGS", "members": 21}, {})

    mock_risk.assert_called_once_with(require_war_participation=False)


def test_build_clan_status_report_formats_recent_joins_as_relative_days():
    joined_date = (datetime.now(elixir.CHICAGO).date() - timedelta(days=3)).isoformat()
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 21, "avg_exp_level": 60.5, "avg_trophies": 7523.4}),
        patch("elixir.db.list_members", return_value=[]),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}),
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
        patch("elixir.db.list_recent_joins", return_value=[{"member_ref": "Ditika", "joined_date": joined_date}]),
        patch("elixir.db.get_war_deck_status_today", return_value={}),
    ):
        report = elixir._build_clan_status_report({"name": "POAP KINGS", "members": 21}, {})

    assert "Recent joins: Ditika (3 days ago)" in report


def test_build_clan_status_report_prefers_live_recent_join_delta():
    today = datetime.now(elixir.CHICAGO).date().isoformat()
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 21, "avg_exp_level": 60.5, "avg_trophies": 7523.4}),
        patch("elixir.db.list_members", return_value=[]),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value={
            "races": 1,
            "total_clan_fame": 1000,
            "fame_per_active_member": 50.0,
            "top_contributors": [],
            "nonparticipants": [],
        }),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}),
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
        patch("elixir.db.list_recent_joins", return_value=[{"member_ref": "Vijay", "joined_date": "2026-03-07"}]),
        patch("elixir.db.get_war_deck_status_today", return_value={}),
    ):
        report = elixir._build_clan_status_report(
            {
                "name": "POAP KINGS",
                "members": 21,
                "_elixir_recent_joins": [{"member_ref": "Ditika", "joined_date": today}],
            },
            {},
        )

    assert "Watch list: 0 with no war decks this season | 0 at risk | 0 on cold streaks | 1 joined in last 30d" in report
    assert "Recent joins: Ditika (today)" in report
    assert "Vijay" not in report.split("Recent joins: ", 1)[1]


def test_load_live_clan_context_attaches_same_cycle_recent_joins():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.cr_api.get_clan", return_value={
            "name": "POAP KINGS",
            "memberList": [
                {"tag": "#AAA", "name": "Existing"},
                {"tag": "#BBB", "name": "Ditika"},
            ],
        }),
        patch("elixir.db.get_active_roster_map", return_value={"#AAA": "Existing"}),
        patch("elixir.db.snapshot_members"),
        patch("elixir.cr_api.get_current_war", return_value={}),
    ):
        clan, war = asyncio.run(elixir._load_live_clan_context())

    assert war == {}
    assert clan["_elixir_recent_joins"] == [
        {
            "player_tag": "BBB",
            "tag": "BBB",
            "current_name": "Ditika",
            "name": "Ditika",
            "member_ref": "Ditika",
            "joined_date": datetime.now(elixir.CHICAGO).date().isoformat(),
        }
    ]


def test_load_live_clan_context_does_not_mark_existing_members_new_when_db_tags_keep_hash():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.cr_api.get_clan", return_value={
            "name": "POAP KINGS",
            "memberList": [
                {"tag": "#AAA", "name": "Existing"},
                {"tag": "#BBB", "name": "Also Existing"},
            ],
        }),
        patch("elixir.db.get_active_roster_map", return_value={"#AAA": "Existing", "#BBB": "Also Existing"}),
        patch("elixir.db.snapshot_members"),
        patch("elixir.cr_api.get_current_war", return_value={}),
    ):
        clan, war = asyncio.run(elixir._load_live_clan_context())

    assert war == {}
    assert "_elixir_recent_joins" not in clan


def test_build_roster_join_dates_report_uses_human_fallback_for_missing_dates():
    with patch("elixir.db.list_members", return_value=[
        {"current_name": "raquaza", "role": "coLeader", "joined_date": None},
        {"current_name": "King Levy", "role": "leader", "joined_date": "2024-01-15"},
    ]):
        report = elixir._build_roster_join_dates_report()

    assert "raquaza (coLeader) — join date not tracked yet" in report
    assert "King Levy (leader) — joined 2024-01-15" in report


def test_build_kick_risk_report_uses_inactivity_only():
    with patch("elixir.db.get_members_at_risk", return_value={
        "members": [
            {
                "member_ref": "Vijay",
                "reasons": [
                    {"type": "inactive", "detail": "last seen 8 days ago"},
                    {"type": "low_donations", "detail": "0 donations this week"},
                ],
            }
        ]
    }) as mock_risk:
        report = elixir._build_kick_risk_report()

    mock_risk.assert_called_once_with(
        inactivity_days=7,
        min_donations_week=0,
        require_war_participation=False,
        tenure_grace_days=0,
    )
    assert report == "**Kick Risk (Inactive 7+ Days)**\n- Vijay — last seen 8 days ago"


def test_build_top_war_contributors_report_formats_season_leaders():
    with patch("elixir.db.get_war_season_summary", return_value={
        "season_id": 130,
        "top_contributors": [
            {"member_ref": "King Levy", "total_fame": 3200, "races_played": 4},
            {"member_ref": "Vijay", "total_fame": 2800, "races_played": 4},
        ],
    }) as mock_summary:
        report = elixir._build_top_war_contributors_report()

    mock_summary.assert_called_once_with(top_n=5)
    assert report == (
        "**Top War Contributors (Season 130)**\n"
        "1. King Levy — 3,200 fame across 4 race(s)\n"
        "2. Vijay — 2,800 fame across 4 race(s)"
    )


def test_reply_text_converts_markdown_images_to_discord_friendly_text():
    message = _make_message(200, "clan-ops", "deck")

    asyncio.run(
        elixir._reply_text(
            message,
            "![Royal Ghost](https://example.com/ghost.png)\n![Witch](https://example.com/witch.png)",
        )
    )

    message.reply.assert_awaited_once_with(
        "Royal Ghost: https://example.com/ghost.png\nWitch: https://example.com/witch.png"
    )


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
                {"member_ref": "King Levy (<@1474760692992180429>)", "total_fame": 3200},
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
    assert "Season: fame/member 1,117.0 | top King Levy (<@1474760692992180429>) 3,200, Finn 3,100" in report
    assert "Watch: 1 at risk | 1 on cold streaks" in report


def test_build_clan_status_short_report_uses_non_war_risk_watchlist():
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 21, "avg_exp_level": 60.5, "avg_trophies": 7523.4}),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}) as mock_risk,
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
    ):
        elixir._build_clan_status_short_report({"name": "POAP KINGS", "members": 21}, {})

    mock_risk.assert_called_once_with(require_war_participation=False)


def test_build_weekly_clanops_review_tags_leaders_and_summarizes_actions():
    with (
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 21}),
        patch("elixir.db.get_promotion_candidates", return_value={
            "composition": {"elders": 5, "target_elder_min": 4, "target_elder_max": 6, "elder_capacity_remaining": 1},
            "recommended": [
                {"member_ref": "King Levy (<@1474760692992180429>)", "donations": 220, "war_races_played": 4, "tenure_days": 90, "days_inactive": 0},
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
    assert "Promote now (1): King Levy (<@1474760692992180429>) — 220 donations, 4 war races, 90d tenure, seen 0d ago" in report
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
