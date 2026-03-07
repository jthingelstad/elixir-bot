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
