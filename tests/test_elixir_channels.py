"""Tests for channel-role routing in elixir.py."""

import asyncio
import json
import signal
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import elixir
from runtime.activities import (
    list_registered_activities,
    manual_activity_choices,
    register_scheduled_activities,
    schedule_specs_from_registry,
)
from runtime.admin import admin_command_requires_leader
from runtime.discord_commands import register_elixir_app_commands


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


def _make_message(channel_id, channel_name, content, *, mentions=None, roles=None):
    author = SimpleNamespace(
        bot=False,
        id=123,
        name="jamie",
        display_name="Jamie",
        global_name=None,
        roles=roles or [],
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


class _FakeTree:
    def __init__(self):
        self.commands = []

    def add_command(self, cmd, guild=None):
        del guild
        self.commands.append(cmd)


class _FakeBot:
    def __init__(self):
        self.tree = _FakeTree()


def test_on_message_routes_interactive_channel_when_mentioned():
    message = _make_message(100, "member-chat", "<@999> how am I doing?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.helpers._common.bot", new=SimpleNamespace(user=SimpleNamespace(id=999))),
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
    mock_history.assert_called_once_with("channel_user:100:123", elixir.CHANNEL_CONVERSATION_LIMIT)
    message.reply.assert_awaited_once_with("You look solid.")
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_on_message_routes_ask_elixir_without_mention():
    message = _make_message(1482368505058955467, "ask-elixir", "what deck should I learn next?")
    sent_message = SimpleNamespace(id=987)
    message.reply = AsyncMock(return_value=sent_message)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 1482368505058955467,
            "name": "#ask-elixir",
            "subagent": "ask-elixir",
            "workflow": "interactive",
            "reply_policy": "open_channel",
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]) as mock_history,
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch(
            "elixir.elixir_agent.respond_in_channel",
            return_value={"event_type": "channel_response", "content": "Try a deck with faster cycles so you can learn matchups quicker.", "summary": "learn a faster deck"},
        ) as mock_respond,
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    assert mock_respond.call_args.kwargs["workflow"] == "interactive"
    assert mock_respond.call_args.kwargs["channel_name"] == "#ask-elixir"
    mock_history.assert_called_once_with("channel_user:1482368505058955467:123", elixir.CHANNEL_CONVERSATION_LIMIT)
    message.reply.assert_awaited_once_with("Try a deck with faster cycles so you can learn matchups quicker.")
    assistant_save = [call for call in mock_save.call_args_list if call.args[1] == "assistant"][0]
    assert assistant_save.kwargs["discord_message_id"] == "987"
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_on_message_routes_reception_without_mention():
    message = _make_message(1476456514121109514, "reception", "how do I get verified?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 1476456514121109514,
            "name": "#reception",
            "subagent": "reception",
            "workflow": "reception",
            "reply_policy": "open_channel",
            "memory_scope": "public",
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message"),
        patch("runtime.channel_router.cr_api.get_clan", return_value={"memberList": [{"tag": "#ABC123", "name": "King Levy"}]}),
        patch(
            "runtime.channel_router.elixir_agent.respond_in_reception",
            return_value={"event_type": "reception_response", "content": "Set your server nickname to your Clash name and I can help verify you."},
        ) as mock_respond,
        patch("elixir._reply_text", new=AsyncMock()) as mock_reply,
    ):
        asyncio.run(elixir.on_message(message))

    assert mock_respond.call_args.kwargs["question"] == "how do I get verified?"
    mock_reply.assert_awaited_once_with(message, "Set your server nickname to your Clash name and I can help verify you.")
    mock_process.assert_not_awaited()


def test_on_message_does_not_save_unsent_interactive_reply():
    message = _make_message(100, "member-chat", "<@999> how am I doing?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.helpers._common.bot", new=SimpleNamespace(user=SimpleNamespace(id=999))),
        patch("elixir._get_channel_behavior", return_value={
            "id": 100,
            "name": "#member-chat",
            "role": "interactive",
            "workflow": "interactive",
            "mention_required": True,
            "allow_proactive": False,
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch(
            "elixir.elixir_agent.respond_in_channel",
            return_value={"event_type": "channel_response", "content": "You look solid.", "summary": "solid"},
        ),
        patch("elixir._reply_text", new=AsyncMock(side_effect=RuntimeError("send failed"))),
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    assistant_saves = [call for call in mock_save.call_args_list if call.args[1] == "assistant"]
    assert assistant_saves == []
    mock_share.assert_not_awaited()
    message.reply.assert_awaited_once_with("Hit an error. Try again in a moment.")
    mock_process.assert_not_awaited()


def test_on_raw_reaction_add_records_negative_feedback_and_invites_retry():
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=123,
        emoji="👎",
        member=SimpleNamespace(bot=False),
    )
    assistant_row = {
        "message_id": 77,
        "discord_message_id": "987",
        "thread_id": 5,
        "channel_id": "1482368505058955467",
        "discord_user_id": "123",
        "author_type": "assistant",
        "workflow": "interactive",
        "event_type": "channel_response",
        "content": "Try a faster cycle deck.",
        "summary": "faster deck",
        "created_at": "2026-03-15T12:00:00",
    }
    reacted_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        reply=AsyncMock(return_value=SimpleNamespace(id=654)),
    )
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=reacted_message))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._get_channel_behavior", return_value={"id": 1482368505058955467, "name": "#ask-elixir", "subagent": "ask-elixir"}),
        patch("runtime.prompt_feedback.db.get_message_by_discord_message_id", return_value=assistant_row),
        patch("runtime.prompt_feedback.db.upsert_prompt_feedback", return_value={"prompt_feedback_id": 44, "became_active_down": True}) as mock_upsert,
        patch("runtime.prompt_feedback.db.mark_prompt_feedback_retry_invited") as mock_mark,
        patch("runtime.app.bot", new=SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _channel_id: channel)),
    ):
        asyncio.run(elixir.on_raw_reaction_add(payload))

    mock_upsert.assert_called_once()
    channel.fetch_message.assert_awaited_once_with(987)
    reacted_message.add_reaction.assert_awaited_once_with("✅")
    reacted_message.reply.assert_awaited_once()
    mock_mark.assert_called_once_with(44, retry_message_id=654)


def test_on_raw_reaction_add_records_positive_feedback_and_acknowledges_receipt():
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=123,
        emoji="👍",
        member=SimpleNamespace(bot=False),
    )
    assistant_row = {
        "message_id": 77,
        "discord_message_id": "987",
        "thread_id": 5,
        "channel_id": "1482368505058955467",
        "discord_user_id": "123",
        "author_type": "assistant",
        "workflow": "interactive",
        "event_type": "channel_response",
        "content": "Try a faster cycle deck.",
        "summary": "faster deck",
        "created_at": "2026-03-15T12:00:00",
    }
    reacted_message = SimpleNamespace(add_reaction=AsyncMock(), reply=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=reacted_message))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._get_channel_behavior", return_value={"id": 1482368505058955467, "name": "#ask-elixir", "subagent": "ask-elixir"}),
        patch("runtime.prompt_feedback.db.get_message_by_discord_message_id", return_value=assistant_row),
        patch("runtime.prompt_feedback.db.upsert_prompt_feedback", return_value={"prompt_feedback_id": 45, "became_active_down": False}) as mock_upsert,
        patch("runtime.prompt_feedback.db.mark_prompt_feedback_retry_invited") as mock_mark,
        patch("runtime.app.bot", new=SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _channel_id: channel)),
    ):
        asyncio.run(elixir.on_raw_reaction_add(payload))

    mock_upsert.assert_called_once()
    channel.fetch_message.assert_awaited_once_with(987)
    reacted_message.add_reaction.assert_awaited_once_with("✅")
    reacted_message.reply.assert_not_awaited()
    mock_mark.assert_not_called()


def test_on_raw_reaction_add_ignores_non_owner_feedback():
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=9999,
        emoji="👎",
        member=SimpleNamespace(bot=False),
    )
    assistant_row = {
        "message_id": 77,
        "discord_message_id": "987",
        "thread_id": 5,
        "channel_id": "1482368505058955467",
        "discord_user_id": "123",
        "author_type": "assistant",
        "workflow": "interactive",
        "event_type": "channel_response",
        "content": "Try a faster cycle deck.",
        "summary": "faster deck",
        "created_at": "2026-03-15T12:00:00",
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._get_channel_behavior", return_value={"id": 1482368505058955467, "name": "#ask-elixir", "subagent": "ask-elixir"}),
        patch("runtime.prompt_feedback.db.get_message_by_discord_message_id", return_value=assistant_row),
        patch("runtime.prompt_feedback.db.upsert_prompt_feedback") as mock_upsert,
        patch("runtime.app.bot", new=SimpleNamespace(user=SimpleNamespace(id=111), get_channel=lambda _channel_id: None)),
    ):
        asyncio.run(elixir.on_raw_reaction_add(payload))

    mock_upsert.assert_not_called()


def test_on_raw_reaction_add_does_not_repeat_retry_invitation_for_active_down_feedback():
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=123,
        emoji="👎",
        member=SimpleNamespace(bot=False),
    )
    assistant_row = {
        "message_id": 77,
        "discord_message_id": "987",
        "thread_id": 5,
        "channel_id": "1482368505058955467",
        "discord_user_id": "123",
        "author_type": "assistant",
        "workflow": "interactive",
        "event_type": "channel_response",
        "content": "Try a faster cycle deck.",
        "summary": "faster deck",
        "created_at": "2026-03-15T12:00:00",
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._get_channel_behavior", return_value={"id": 1482368505058955467, "name": "#ask-elixir", "subagent": "ask-elixir"}),
        patch("runtime.prompt_feedback.db.get_message_by_discord_message_id", return_value=assistant_row),
        patch("runtime.prompt_feedback.db.upsert_prompt_feedback", return_value={"prompt_feedback_id": 44, "became_active_down": False}) as mock_upsert,
        patch("runtime.prompt_feedback.db.mark_prompt_feedback_retry_invited") as mock_mark,
        patch("runtime.app.bot", new=SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _channel_id: None)),
    ):
        asyncio.run(elixir.on_raw_reaction_add(payload))

    mock_upsert.assert_called_once()
    mock_mark.assert_not_called()


def test_on_raw_reaction_remove_clears_matching_feedback():
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=123,
        emoji="👍",
    )
    assistant_row = {
        "message_id": 77,
        "discord_message_id": "987",
        "thread_id": 5,
        "channel_id": "1482368505058955467",
        "discord_user_id": "123",
        "author_type": "assistant",
        "workflow": "interactive",
        "event_type": "channel_response",
        "content": "Try a faster cycle deck.",
        "summary": "faster deck",
        "created_at": "2026-03-15T12:00:00",
    }

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._get_channel_behavior", return_value={"id": 1482368505058955467, "name": "#ask-elixir", "subagent": "ask-elixir"}),
        patch("runtime.prompt_feedback.db.get_message_by_discord_message_id", return_value=assistant_row),
        patch("runtime.prompt_feedback.db.clear_prompt_feedback") as mock_clear,
        patch("runtime.app.bot", new=SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _channel_id: None)),
    ):
        asyncio.run(elixir.on_raw_reaction_remove(payload))

    mock_clear.assert_called_once_with(
        assistant_discord_message_id=987,
        discord_user_id=123,
        feedback_value="up",
    )


def test_on_message_saves_primary_discord_message_id_for_multipart_ask_elixir_reply():
    message = _make_message(1482368505058955467, "ask-elixir", "give me a deeper explanation")
    sent_messages = [
        SimpleNamespace(id=2001),
        SimpleNamespace(id=2002),
    ]
    message.reply = AsyncMock(side_effect=sent_messages)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 1482368505058955467,
            "name": "#ask-elixir",
            "subagent": "ask-elixir",
            "workflow": "interactive",
            "reply_policy": "open_channel",
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch(
            "agent.intent_router.classify_intent",
            return_value={"route": "llm_chat", "confidence": 1.0, "rationale": "test"},
        ),
        patch(
            "elixir.elixir_agent.respond_in_channel",
            return_value={
                "event_type": "channel_response",
                "content": ["Part one.", "Part two."],
                "summary": "two-part answer",
            },
        ),
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    assistant_save = [call for call in mock_save.call_args_list if call.args[1] == "assistant"][0]
    assert assistant_save.kwargs["discord_message_id"] == "2001"
    assert assistant_save.args[2] == "Part one.\n\nPart two."
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_is_bot_mentioned_requires_leading_mention():
    bot_user = SimpleNamespace(id=999)
    direct_message = _make_message(100, "member-chat", "<@999> how am I doing?")
    mid_message = _make_message(100, "member-chat", "how am I doing, <@999>?")

    with patch("runtime.helpers._common.bot", new=SimpleNamespace(user=bot_user)):
        assert elixir._is_bot_mentioned(direct_message) is True
        assert elixir._is_bot_mentioned(mid_message) is False


def test_strip_bot_mentions_removes_only_leading_mention():
    with (
        patch("runtime.helpers._common.bot", new=SimpleNamespace(user=SimpleNamespace(id=999))),
        patch("runtime.helpers._common.BOT_ROLE_ID", 777),
    ):
        assert elixir._strip_bot_mentions("<@999> help <@999>") == "help <@999>"
        assert elixir._strip_bot_mentions("help <@999>") == "help <@999>"
        assert elixir._strip_bot_mentions("<@&777> help") == "help"


def test_post_to_elixir_sends_content_list_as_multiple_messages():
    channel = SimpleNamespace(send=AsyncMock())

    asyncio.run(elixir._post_to_elixir(channel, {"content": ["First post", "Second post"]}))

    assert channel.send.await_args_list[0].args == ("First post",)
    assert channel.send.await_args_list[1].args == ("Second post",)


def test_entry_posts_merges_related_multipart_updates_into_one_message():
    posts = elixir._entry_posts(
        {
            "content": [
                "Battle Day 1 is live. Use all 4 decks today.",
                "We are in 2nd place right now, so early decks matter.",
                "If you have not started yet, get those war decks in early.",
            ]
        }
    )

    assert len(posts) == 1
    assert "Battle Day 1 is live." in posts[0]
    assert "We are in 2nd place right now" in posts[0]


def test_entry_posts_keeps_distinct_updates_separate():
    posts = elixir._entry_posts(
        {
            "content": [
                "King Levy just crossed 9000 trophies.",
                "Vijay is leading donations this week with 2500 cards given.",
            ]
        }
    )

    assert posts == [
        "King Levy just crossed 9000 trophies.",
        "Vijay is leading donations this week with 2500 cards given.",
    ]


def test_post_to_elixir_resolves_custom_emoji_shortcodes():
    guild = SimpleNamespace(emojis=[SimpleNamespace(name="elixir_hype", id=321, animated=False)])
    channel = SimpleNamespace(send=AsyncMock(), guild=guild)

    asyncio.run(elixir._post_to_elixir(channel, {"content": "Keep climbing :elixir_hype:"}))

    channel.send.assert_awaited_once_with("Keep climbing <:elixir_hype:321>")


def test_post_startup_message_posts_build_hash_to_clanops():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    proactive_channels = [
        {"id": 200, "name": "#leader-lounge", "workflow": "clanops"},
        {"id": 300, "name": "#ask-elixir", "workflow": "interactive"},
    ]

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.prompts.discord_channels_by_workflow", return_value=[{"id": 200, "name": "#leader-lounge"}]),
        patch("elixir.prompts.discord_channel_configs", return_value=proactive_channels),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.RELEASE_LABEL", 'v3.0 "Three-Lane Elixir"'),
        patch("elixir.elixir_agent.BUILD_HASH", "abc1234"),
        patch("elixir.elixir_agent.generate_message", return_value=":elixir_hype: I just dropped into the arena and the king tower is awake.") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        sent = asyncio.run(elixir._post_startup_message())

    assert sent is True
    mock_generate.assert_called_once()
    posted = mock_post.await_args.args[1]["content"]
    assert posted.startswith("**Elixir Online**")
    assert 'Release: `v3.0 "Three-Lane Elixir"`' in posted
    assert "Build: `abc1234`" in posted
    assert "king tower is awake" in posted
    assert "Channel audit: 2/2 active channels reachable and writable." in posted
    assert mock_save.call_args.kwargs["workflow"] == "clanops"
    assert mock_save.call_args.kwargs["event_type"] == "startup_announcement"


def test_post_startup_message_fetches_channel_when_not_cached():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.prompts.discord_channels_by_workflow", return_value=[{"id": 200, "name": "#leader-lounge"}]),
        patch("elixir.prompts.discord_channel_configs", return_value=[{"id": 200, "name": "#leader-lounge", "workflow": "clanops"}]),
        patch.object(elixir.bot, "get_channel", return_value=None),
        patch.object(elixir.bot, "fetch_channel", new=AsyncMock(return_value=channel)) as mock_fetch,
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.RELEASE_LABEL", 'v3.0 "Three-Lane Elixir"'),
        patch("elixir.elixir_agent.BUILD_HASH", "abc1234"),
        patch("elixir.elixir_agent.generate_message", return_value="Elixir has entered the arena.") as mock_generate,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        sent = asyncio.run(elixir._post_startup_message())

    assert sent is True
    assert mock_fetch.await_count == 2
    mock_fetch.assert_any_await(200)
    mock_generate.assert_called_once()
    mock_post.assert_awaited_once()
    assert mock_save.call_args.kwargs["event_type"] == "startup_announcement"


def test_acquire_pid_file_ignores_non_elixir_reused_pid(tmp_path):
    pid_file = tmp_path / "elixir.pid"
    pid_file.write_text("999")

    with (
        patch("elixir.PID_FILE", str(pid_file)),
        patch("elixir.os.getpid", return_value=1234),
        patch("elixir._process_exists", return_value=True),
        patch("elixir._pid_looks_like_elixir", return_value=False),
        patch("elixir.os.kill") as mock_kill,
        patch("elixir.log.warning") as mock_warning,
    ):
        elixir._acquire_pid_file()

    payload = json.loads(pid_file.read_text())
    assert payload["pid"] == 1234
    mock_kill.assert_not_called()
    mock_warning.assert_called_once()


def test_acquire_pid_file_stops_prior_elixir_process(tmp_path):
    pid_file = tmp_path / "elixir.pid"
    pid_file.write_text(json.dumps({"pid": 999}))

    with (
        patch("elixir.PID_FILE", str(pid_file)),
        patch("elixir.os.getpid", return_value=1234),
        patch("elixir._process_exists", return_value=True),
        patch("elixir._pid_looks_like_elixir", return_value=True),
        patch("elixir._wait_for_process_exit", return_value=True),
        patch("elixir.os.kill") as mock_kill,
    ):
        elixir._acquire_pid_file()

    payload = json.loads(pid_file.read_text())
    assert payload["pid"] == 1234
    mock_kill.assert_called_once_with(999, signal.SIGTERM)


def test_startup_channel_audit_reports_missing_or_unwritable_channels():
    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")
    writable = SimpleNamespace(id=300, name="ask-elixir", type="text")
    blocked_perms = SimpleNamespace(view_channel=True, send_messages=False)
    blocked_channel = SimpleNamespace(
        id=400,
        name="river-race",
        type="text",
        guild=SimpleNamespace(id=1, me=object()),
        permissions_for=lambda member: blocked_perms,
    )

    def fake_get_channel(channel_id):
        return {200: channel, 300: writable}.get(channel_id)

    async def fake_fetch_channel(channel_id):
        if channel_id == 400:
            return blocked_channel
        raise RuntimeError("missing")

    with (
        patch.object(elixir.bot, "get_channel", side_effect=fake_get_channel),
        patch.object(elixir.bot, "fetch_channel", new=AsyncMock(side_effect=fake_fetch_channel)),
        patch.object(type(elixir.bot), "user", new_callable=PropertyMock, return_value=SimpleNamespace(id=999)),
        patch(
            "elixir.prompts.discord_channel_configs",
            return_value=[
                {"id": 200, "name": "#leader-lounge", "workflow": "clanops"},
                {"id": 300, "name": "#ask-elixir", "workflow": "interactive"},
                {"id": 400, "name": "#river-race", "workflow": "channel_update"},
                {"id": 500, "name": "#missing", "workflow": "channel_update"},
            ],
        ),
    ):
        summary = asyncio.run(elixir._startup_channel_audit_summary())

    assert "Channel audit: 2/4 active channels reachable and writable." in summary
    assert "#river-race not writable" in summary
    assert "#missing missing or unreachable" in summary


def test_ask_elixir_daily_insight_posts_fun_fact():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = SimpleNamespace(id=1482368505058955467, name="ask-elixir", type="text")

    with (
        patch("runtime.jobs._core.asyncio.to_thread", side_effect=fake_to_thread),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("runtime.jobs._core._get_singleton_channel_id", return_value=1482368505058955467),
        patch(
            "elixir.prompts.discord_channels_by_subagent",
            return_value={
                "ask-elixir": {
                    "id": 1482368505058955467,
                    "name": "#ask-elixir",
                    "subagent_key": "ask-elixir",
                    "memory_scope": "public",
                    "durable_memory_enabled": True,
                }
            },
        ),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("runtime.jobs._core.build_subagent_memory_context", return_value={}),
        patch("runtime.jobs._core._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS", "tag": "#J2RGCRVG", "memberList": [{"name": "Jamie"}]}, {}))),
        patch("elixir.db.get_clan_roster_summary", return_value={"active_members": 50, "donations_week_total": 12345, "avg_member_trophies": 8123}),
        patch("elixir.db.build_clan_trend_summary_context", return_value="Clan score is up over the last 7 days."),
        patch("elixir.db.get_members_on_hot_streak", return_value=[{"name": "Jamie", "current_streak": 5}]),
        patch("elixir.db.get_trending_war_contributors", return_value={"members": [{"name": "Vijay", "trend_delta": 240}]}),
        patch("elixir.db.list_longest_tenure_members", return_value=[{"name": "King Levy", "joined_date": "2023-01-01"}]),
        patch("elixir.db.get_members_with_most_level_16_cards", return_value=[{"name": "Ace", "level_16_card_count": 14}]),
        patch("elixir.db.list_members", return_value=[{"current_name": "Jamie", "donations_week": 400, "clan_rank": 1}]),
        patch("elixir.db.get_war_season_summary", return_value={"top_members": [{"name": "Vijay", "total_fame": 3200}]}),
        patch(
            "elixir.elixir_agent.generate_channel_update",
            return_value={
                "event_type": "channel_update",
                "summary": "hidden clan pattern",
                "content": "Did you know? Our top donor this week is also sitting on a five-win streak. Generosity and momentum travel together.",
            },
        ) as mock_generate,
        patch("runtime.jobs._core._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        asyncio.run(elixir._ask_elixir_daily_insight())

    assert mock_generate.call_args.args[0] == "#ask-elixir"
    assert mock_generate.call_args.args[1] == "ask-elixir"
    mock_post.assert_awaited_once()
    assert mock_save.call_args.kwargs["workflow"] == "ask-elixir"
    assert mock_save.call_args.kwargs["event_type"] == "daily_clan_insight"


def test_on_message_replies_with_fallback_when_channel_agent_returns_none():
    message = _make_message(200, "clan-ops", "<@999> What is my current war participation rate over the last 4 weeks?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.helpers._common.bot", new=SimpleNamespace(user=SimpleNamespace(id=999))),
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
            "llm": {
                "last_error": "Error code: 429 rate_limit_exceeded",
                "last_model": "claude-sonnet-4-6",
                "last_call_at": "2026-03-07T19:12:00",
            }
        }),
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once_with(
        "I couldn't produce a clean answer from the data I have. Try asking a narrower clan ops question."
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
        llm_last_error="Error code: 429 rate_limit_exceeded",
        llm_last_model="claude-sonnet-4-6",
        llm_last_call_at="2026-03-07T19:12:00",
        raw_json=None,
    )
    mock_share.assert_not_awaited()
    mock_process.assert_not_awaited()


def test_on_message_logs_agent_failure_payload_details():
    message = _make_message(200, "clan-ops", "<@999> Who is on the hottest streak right now?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.helpers._common.bot", new=SimpleNamespace(user=SimpleNamespace(id=999))),
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
        patch("elixir.db.record_prompt_failure", return_value=18) as mock_failure,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch("elixir.elixir_agent.respond_in_channel", return_value={
            "_error": {
                "kind": "schema_error",
                "detail": "missing required field: content",
                "phase": "repair_response",
                "result_preview": '{"event_type":"channel_response"}',
                "raw_json": {"event_type": "channel_response"},
            }
        }),
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
        patch("elixir.runtime_status.snapshot", return_value={
            "llm": {
                "last_error": None,
                "last_model": "claude-sonnet-4-6",
                "last_call_at": "2026-03-11T07:00:00",
            }
        }),
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once_with(
        "I couldn't produce a clean answer from the data I have. Try asking a narrower clan ops question."
    )
    mock_failure.assert_called_once_with(
        "Who is on the hottest streak right now?",
        "schema_error",
        "respond_in_channel",
        workflow="clanops",
        channel_id=200,
        channel_name="clan-ops",
        discord_user_id=123,
        discord_message_id=555,
        detail="repair_response: missing required field: content",
        result_preview='{"event_type":"channel_response"}',
        llm_last_error=None,
        llm_last_model="claude-sonnet-4-6",
        llm_last_call_at="2026-03-11T07:00:00",
        raw_json={"event_type": "channel_response"},
    )
    mock_share.assert_not_awaited()
    mock_process.assert_not_awaited()


def test_on_message_ignores_unmentioned_clanops_chat():
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
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]) as mock_history,
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch("elixir.elixir_agent.respond_in_channel", return_value={"event_type": "channel_response", "content": "I can pull the current promotion candidates if you want.", "summary": "ops"}) as mock_respond,
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    mock_history.assert_not_called()
    mock_save.assert_not_called()
    mock_respond.assert_not_called()
    mock_share.assert_not_awaited()
    message.reply.assert_not_awaited()
    mock_process.assert_awaited_once_with(message)


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
                {"name": "Knight", "level": 16, "supports_evo": True, "supports_hero": True, "mode_status_label": "Evo + Hero unlocked"},
                {"name": "Fireball", "level": 16, "supports_evo": False, "supports_hero": False, "mode_status_label": None},
            ],
        }),
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_resolve.assert_called_once_with("@Vijay", limit=3)
    message.reply.assert_awaited_once_with(
        "**Current Deck for Vijay (<@456>)**\n"
        "- Knight — Level 16 (Evo + Hero unlocked)\n"
        "- Fireball — Level 16\n"
        "_Activation depends on deck slot; these labels show what the card supports or has unlocked._\n"
        "_Snapshot: 2026-03-07 06:00 AM CT_"
    )
    assert mock_save.call_count == 2
    assert mock_save.call_args_list[1].kwargs["event_type"] == "member_deck_report"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_keeps_interpretive_main_deck_questions_in_llm_path():
    message = _make_message(1482368505058955467, "ask-elixir", "What is the average level of the cards I use in my current main deck?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir._is_bot_mentioned", return_value=False),
        patch("elixir._get_channel_behavior", return_value={
            "id": 1482368505058955467,
            "name": "#ask-elixir",
            "subagent": "ask-elixir",
            "workflow": "interactive",
            "reply_policy": "open_channel",
            "memory_scope": "public",
        }),
        patch("elixir.db.upsert_discord_user"),
        patch("elixir.db.list_thread_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={"channel": {"state": None, "episodes": []}}),
        patch("elixir.db.save_message"),
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch("elixir.elixir_agent.respond_in_channel", return_value={"event_type": "channel_response", "content": "LLM answer", "summary": "llm"}) as mock_respond,
        patch("elixir.db.resolve_member") as mock_resolve,
        patch("elixir.db.get_member_current_deck") as mock_current_deck,
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    mock_current_deck.assert_not_called()
    mock_resolve.assert_not_called()
    mock_respond.assert_called_once()
    message.reply.assert_awaited_once_with("LLM answer")
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_on_message_rewrites_member_refs_before_reply_and_save():
    message = _make_message(100, "member-chat", "<@999> how is King Levy doing?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_format_member_reference(tag, conn=None, **_kwargs):
        return "King Levy" if tag == "#ABC123" else tag

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
        patch("elixir.db.list_thread_messages", return_value=[]),
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.format_member_reference", side_effect=fake_format_member_reference),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"memberList": []}, {}))),
        patch(
            "elixir.elixir_agent.respond_in_channel",
            return_value={
                "event_type": "channel_response",
                "content": "King Levy is trending up.",
                "summary": "up",
                "member_tags": ["#ABC123"],
            },
        ) as mock_respond,
        patch("elixir._share_channel_result", new=AsyncMock()) as mock_share,
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once_with("King Levy is trending up.")
    assert mock_save.call_args_list[1].args[2] == "King Levy is trending up."
    mock_respond.assert_called_once()
    mock_share.assert_awaited_once()
    mock_process.assert_not_awaited()


def test_slash_help_does_not_save_conversation_history():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    help_command = root.get_command("help")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(id=123, name="jamie", display_name="Jamie", roles=[]),
        response=response,
        followup=followup,
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.discord_commands.render_admin_help", return_value="help text"),
        patch("runtime.discord_commands.db.save_message") as mock_save,
    ):
        asyncio.run(help_command.callback(interaction))

    response.send_message.assert_awaited_once_with("help text", ephemeral=True)
    followup.send.assert_not_awaited()
    mock_save.assert_not_called()


def test_register_elixir_app_commands_includes_signals():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    signal_group = root.get_command("signal")
    assert signal_group is not None
    assert signal_group.get_command("show") is not None


def test_register_elixir_app_commands_includes_member_audit_discord():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    member_group = root.get_command("member")
    assert member_group is not None
    assert member_group.get_command("audit-discord") is not None


def test_dispatch_admin_command_handles_member_audit_discord():
    human = SimpleNamespace(
        id=555, bot=False, display_name="UnlinkedUser", nick=None,
        name="UnlinkedUser", global_name="UnlinkedUser",
        roles=[],
    )
    linked = SimpleNamespace(
        id=777, bot=False, display_name="King Levy", nick="King Levy",
        name="kinglevy", global_name="King Levy",
        roles=[SimpleNamespace(id=999)],
    )
    bot_member = SimpleNamespace(id=888, bot=True)
    guild = SimpleNamespace(
        members=[human, linked, bot_member],
        get_role=lambda rid: SimpleNamespace(id=999),
    )

    def fake_linked_lookup(user_id, **_kwargs):
        return {"player_tag": "#ABC"} if int(user_id) == 777 else None

    with (
        patch("runtime.app.bot", new=SimpleNamespace(get_guild=lambda gid: guild)),
        patch("runtime.app.GUILD_ID", 100),
        patch("runtime.app.MEMBER_ROLE_ID", 999),
        patch("db.list_members", return_value=[
            {"player_tag": "#ABC", "current_name": "King Levy", "discord_user_id": "777"},
            {"player_tag": "#DEF", "current_name": "Lonely", "discord_user_id": None},
        ]),
        patch("db.get_linked_member_for_discord_user", side_effect=fake_linked_lookup),
        patch("db.resolve_member", return_value=[]),
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command("member.audit-discord", preview=False, short=False, args={}),
        )

    assert "Active clan members: 2 (1 without a Discord link)" in result
    assert "Unlinked Discord users: 1" in result
    assert "Lonely" in result
    assert "UnlinkedUser" in result


def test_dispatch_admin_command_handles_verify_discord():
    with (
        patch("runtime.admin._resolve_member_tag", return_value=("#ABC123", "King Levy")),
        patch("runtime.onboarding.verify_discord_membership", new=AsyncMock(return_value="Verified Discord identity for King Levy.")) as mock_verify,
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "member.verify-discord",
                preview=False,
                short=False,
                args={"member": "King Levy"},
            )
        )

    assert result == "Verified Discord identity for King Levy."
    mock_verify.assert_awaited_once_with("#ABC123")


def test_dispatch_admin_command_handles_clan_list_full():
    with patch("runtime.admin._build_clan_list_report", return_value="**Clan List Full (2 active)**") as mock_report:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "clan.members",
                preview=False,
                short=False,
                args={"detail": "full"},
            )
        )

    assert result == "**Clan List Full (2 active)**"
    mock_report.assert_called_once_with(full=True)


def test_dispatch_admin_command_returns_runtime_job_failure_text():
    with patch("elixir._weekly_clan_recap", new=AsyncMock(side_effect=RuntimeError("weekly recap post failed: missing Discord permissions in #weekly-digest"))):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "activity.run",
                preview=False,
                short=False,
                args={"activity": "weekly-recap"},
            )
        )

    assert result == "`weekly-recap` failed: weekly recap post failed: missing Discord permissions in #weekly-digest"


def test_dispatch_admin_command_handles_system_signals():
    with patch("runtime.admin._run_system_signals", new=AsyncMock(return_value="Published 1 pending system signal(s).")) as mock_run:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "signal.publish-pending",
                preview=False,
                short=False,
                args={},
            )
        )

    assert result == "Published 1 pending system signal(s)."
    mock_run.assert_awaited_once_with(preview=False)


def test_dispatch_admin_command_handles_signals():
    with patch("runtime.admin._build_signals_report", return_value="**Elixir Signals**") as mock_report:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "signal.show",
                preview=False,
                short=False,
                args={"view": "all", "limit": "10"},
            )
        )

    assert result == "**Elixir Signals**"
    mock_report.assert_called_once_with(view="all", recent_limit="10")


def test_build_signals_report_includes_routing_recent_and_pending():
    conn = elixir.db.get_connection(":memory:")
    try:
        elixir.db.upsert_signal_outcome(
            "member_join:#ABC123",
            "member_join",
            "clan-events",
            1482352241628414013,
            "member_join_public",
            required=True,
            delivery_status="delivered",
            payload={"signals": [{"type": "member_join", "tag": "#ABC123", "name": "King Levy"}]},
            delivered=True,
            conn=conn,
        )
        elixir.db.upsert_signal_outcome(
            "member_join:#ABC123",
            "member_join",
            "leader-lounge",
            1474762000000000000,
            "member_join_ops",
            required=True,
            delivery_status="failed",
            error_detail="missing channel permissions",
            payload={"signals": [{"type": "member_join", "tag": "#ABC123", "name": "King Levy"}]},
            mark_attempt=True,
            conn=conn,
        )
        elixir.db.queue_system_signal(
            "capability_three_lane_elixir_v3",
            "capability_unlock",
            {"type": "capability_unlock", "payload": {"audience": "clan"}},
            conn=conn,
        )

        from runtime.admin import _build_signals_report

        report = _build_signals_report(conn=conn)
    finally:
        conn.close()

    assert "**Elixir Signals**" in report
    assert "Routing:" in report
    assert "`member_join`" in report or "`member_join_public`" in report
    assert "Recent routed signals" in report
    assert "`member_join:#ABC123`" in report
    assert "`clan-events` `member_join_public` delivered" in report
    assert "`leader-lounge` `member_join_ops` failed" in report
    assert "missing channel permissions" in report
    assert "Pending system signals (1)" in report
    assert "`capability_three_lane_elixir_v3`" in report


def test_dispatch_admin_command_handles_activity_run():
    with patch("runtime.admin._run_runtime_job", new=AsyncMock(return_value="Ran `site-content`.")) as mock_job:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "activity.run",
                preview=False,
                short=False,
                args={"activity": "site-content"},
            )
        )

    assert result == "Ran `site-content`."
    mock_job.assert_awaited_once_with("site-content", preview=False)


def test_dispatch_admin_command_handles_set_discord():
    with (
        patch("runtime.onboarding.resolve_discord_member_input", new=AsyncMock(return_value=None)),
        patch("runtime.admin.asyncio.to_thread", new=AsyncMock(side_effect=[("#ABC123", "King Levy")])) as mock_to_thread,
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "member.set",
                preview=False,
                short=False,
                args={"member": "King Levy", "field": "discord", "value": "@kinglevy"},
            )
        )

    assert "Couldn't resolve `@kinglevy` to a unique Discord member for King Levy." in result
    assert "Use a real mention" in result
    assert len(mock_to_thread.await_args_list) == 1


def test_dispatch_admin_command_handles_set_discord_with_resolved_guild_member():
    guild_member = SimpleNamespace(id=456, name="ditaka_user", display_name="Ditaka")
    with (
        patch("runtime.onboarding.resolve_discord_member_input", new=AsyncMock(return_value=guild_member)),
        patch("runtime.admin.asyncio.to_thread", new=AsyncMock(side_effect=[("#VGJJLC9PR", "Ditaka"), None])) as mock_to_thread,
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "member.set",
                preview=False,
                short=False,
                args={"member": "Ditaka", "field": "discord", "value": "Ditaka"},
            )
        )

    assert result == "Linked Discord identity for Ditaka to Ditaka (<@456>)."
    assert mock_to_thread.await_args_list[1].args == (
        elixir.db.link_discord_user_to_member,
        456,
        "#VGJJLC9PR",
    )
    assert mock_to_thread.await_args_list[1].kwargs == {
        "username": "ditaka_user",
        "display_name": "Ditaka",
        "source": "manual_name_resolution",
    }


def test_dispatch_admin_command_handles_set_note_and_writes_contextual_memory():
    from runtime import admin as runtime_admin

    with patch("runtime.admin.asyncio.to_thread", new=AsyncMock(side_effect=[("#ABC123", "King Levy"), None, None])) as mock_to_thread:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "member.set",
                preview=False,
                short=False,
                args={"member": "King Levy", "field": "note", "value": "Reliable war leader."},
            )
        )

    assert result == "Set note for King Levy."
    assert mock_to_thread.await_args_list[1].args == (
        elixir.db.set_member_note,
        "#ABC123",
        None,
        "Reliable war leader.",
    )
    assert mock_to_thread.await_args_list[2].args == (runtime_admin.upsert_member_note_memory,)
    assert mock_to_thread.await_args_list[2].kwargs == {
        "member_tag": "#ABC123",
        "member_label": "King Levy",
        "note": "Reliable war leader.",
        "created_by": "leader:admin-command",
        "metadata": {"command": "set-note"},
    }


def test_dispatch_admin_command_handles_clear_note_and_archives_contextual_memory():
    from runtime import admin as runtime_admin

    with patch("runtime.admin.asyncio.to_thread", new=AsyncMock(side_effect=[("#ABC123", "King Levy"), None, None])) as mock_to_thread:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "member.clear",
                preview=False,
                short=False,
                args={"member": "King Levy", "field": "note"},
            )
        )

    assert result == "Cleared note for King Levy."
    assert mock_to_thread.await_args_list[1].args == (
        elixir.db.clear_member_note,
        "#ABC123",
        None,
    )
    assert mock_to_thread.await_args_list[2].args == (runtime_admin.archive_member_note_memory,)
    assert mock_to_thread.await_args_list[2].kwargs == {
        "member_tag": "#ABC123",
        "actor": "leader:admin-command",
    }


def test_resolve_member_tag_accepts_name_with_tag_label():
    from runtime import admin as runtime_admin

    with patch(
        "db.resolve_member",
        return_value=[{"player_tag": "#VGJJLC9PR", "match_score": 1000, "member_ref_with_handle": "Ditaka"}],
    ) as mock_resolve:
        tag, label = runtime_admin._resolve_member_tag("Ditaka (#VGJJLC9PR)")

    assert tag == "#VGJJLC9PR"
    assert label == "Ditaka"
    mock_resolve.assert_called_once_with("#VGJJLC9PR", limit=3, conn=None)


def test_resolve_member_tag_rejects_empty_and_overlong_inputs():
    from runtime import admin as runtime_admin
    import pytest

    with pytest.raises(ValueError, match="required"):
        runtime_admin._resolve_member_tag("")
    with pytest.raises(ValueError, match="required"):
        runtime_admin._resolve_member_tag("   ")
    with pytest.raises(ValueError, match="64 characters"):
        runtime_admin._resolve_member_tag("x" * 100)


def test_admin_command_requires_leader_for_memory():
    assert admin_command_requires_leader("memory.show") is True
    assert admin_command_requires_leader("system.status") is False


def test_dispatch_admin_command_handles_memory():
    with patch("runtime.admin._build_memory_report", return_value="**Elixir Memory**\n- Context store: 3 total") as mock_report:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "memory.show",
                preview=False,
                short=False,
                args={"member": "King Levy", "limit": "3", "include_system_internal": "true"},
            )
        )

    assert result == "**Elixir Memory**\n- Context store: 3 total"
    mock_report.assert_called_once_with(
        member_query="King Levy",
        query=None,
        limit="3",
        include_system_internal=True,
    )


def test_dispatch_admin_command_handles_db_status():
    with patch("elixir._build_db_status_report", return_value="**Elixir DB Status | Memory**\n- Tables:") as mock_report:
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "system.storage",
                preview=False,
                short=False,
                args={"view": "memory"},
            )
        )

    assert result == "**Elixir DB Status | Memory**\n- Tables:"
    mock_report.assert_called_once_with(group="memory")


def test_dispatch_admin_command_handles_war_status():
    with (
        patch("elixir._load_live_clan_context", new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"clans": [{}, {}]}))) as mock_load,
        patch("elixir._build_war_status_report", return_value="**POAP KINGS War Status**\n- Live: Battle Day 2") as mock_report,
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "clan.war",
                preview=False,
                short=False,
                args={},
            )
        )

    assert result == "**POAP KINGS War Status**\n- Live: Battle Day 2"
    mock_load.assert_awaited_once_with()
    mock_report.assert_called_once_with({"name": "POAP KINGS"}, {"clans": [{}, {}]})


def test_slash_clan_members_full_passes_flag_to_admin_dispatch():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    clan_group = root.get_command("clan")
    clan_list_command = clan_group.get_command("members")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(id=123, name="jamie", display_name="Jamie", roles=[]),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.discord_commands.dispatch_admin_command", new=AsyncMock(return_value="full list")) as mock_dispatch,
    ):
        asyncio.run(clan_list_command.callback(interaction, detail="full"))

    mock_dispatch.assert_awaited_once_with(
        "clan.members",
        preview=False,
        short=False,
        args={"detail": "full"},
    )
    response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content="full list")
    followup.send.assert_not_awaited()


def test_slash_system_storage_dispatches_to_admin():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    system_group = root.get_command("system")
    db_status_command = system_group.get_command("storage")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(id=123, name="jamie", display_name="Jamie", roles=[]),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.discord_commands.dispatch_admin_command", new=AsyncMock(return_value="db report")) as mock_dispatch,
    ):
        asyncio.run(db_status_command.callback(interaction, view="memory"))

    mock_dispatch.assert_awaited_once_with(
        "system.storage",
        preview=False,
        short=False,
        args={"view": "memory"},
    )
    response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content="db report")
    followup.send.assert_not_awaited()


def test_slash_clan_war_dispatches_to_admin():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    clan_group = root.get_command("clan")
    war_status_command = clan_group.get_command("war")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(id=123, name="jamie", display_name="Jamie", roles=[]),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.discord_commands.dispatch_admin_command", new=AsyncMock(return_value="war report")) as mock_dispatch,
    ):
        asyncio.run(war_status_command.callback(interaction))

    mock_dispatch.assert_awaited_once_with(
        "clan.war",
        preview=False,
        short=False,
        args={},
    )
    response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content="war report")
    followup.send.assert_not_awaited()


def test_slash_member_set_discord_passes_identity_to_admin_dispatch():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    member_group = root.get_command("member")
    set_discord_command = member_group.get_command("set")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(id=123, name="jamie", display_name="Jamie", roles=[SimpleNamespace(name="Leader")]),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.app._has_leader_role", return_value=True),
        patch("runtime.discord_commands.dispatch_admin_command", new=AsyncMock(return_value="linked")) as mock_dispatch,
    ):
        asyncio.run(set_discord_command.callback(interaction, member="King Levy", field="discord", value="@kinglevy"))

    mock_dispatch.assert_awaited_once_with(
        "member.set",
        preview=False,
        short=False,
        args={"member": "King Levy", "field": "discord", "value": "@kinglevy"},
    )
    response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content="linked")
    followup.send.assert_not_awaited()


def test_slash_activity_run_defers_before_dispatching():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = bot.tree.commands[0]
    jobs_group = root.get_command("activity")
    run_command = jobs_group.get_command("run")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(id=123, name="jamie", display_name="Jamie", roles=[SimpleNamespace(name="Leader")]),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.app._has_leader_role", return_value=True),
        patch("runtime.discord_commands.dispatch_admin_command", new=AsyncMock(return_value="job failed")) as mock_dispatch,
    ):
        asyncio.run(run_command.callback(interaction, activity="weekly-recap", preview=False))

    response.defer.assert_awaited_once_with(ephemeral=True)
    mock_dispatch.assert_awaited_once_with(
        "activity.run",
        preview=False,
        short=False,
        args={"activity": "weekly-recap"},
    )
    interaction.edit_original_response.assert_awaited_once_with(content="job failed")
    followup.send.assert_not_awaited()


def test_queue_startup_system_signals_enqueues_memory_capability_announcement():
    with patch("elixir.db.queue_system_signal") as mock_queue:
        elixir.queue_startup_system_signals()

    queued = {call.args[0]: call.args[2] for call in mock_queue.call_args_list}
    assert queued["release_three_lane_elixir_v3"]["title"] == 'Achievement Unlocked: v3 "Three-Lane Elixir"'
    assert queued["release_three_lane_elixir_v3"]["capability_area"] == "three_lane_elixir"
    assert "Three-Lane Elixir" in queued["release_three_lane_elixir_v3"]["message"]
    assert "River Race coordination" in " ".join(queued["release_three_lane_elixir_v3"]["details"])
    assert "Ask Elixir" in " ".join(queued["release_three_lane_elixir_v3"]["details"])
    assert queued["capability_memory_system_v1"]["title"] == "Achievement Unlocked: Stronger Memory"
    assert queued["capability_memory_system_v1"]["capability_area"] == "memory"
    assert "/elixir memory show" in " ".join(queued["capability_memory_system_v1"]["details"])
    assert queued["capability_battle_pulse_v1"]["title"] == "Achievement Unlocked: Battle Pulse"
    assert queued["capability_battle_pulse_v1"]["capability_area"] == "battle_pulse"
    assert "Path of Legend" in " ".join(queued["capability_battle_pulse_v1"]["details"])
    assert queued["capability_badge_and_achievement_celebrations_v1"]["title"] == "Achievement Unlocked: Badge Celebrations"
    assert queued["capability_badge_and_achievement_celebrations_v1"]["capability_area"] == "badge_celebrations"
    assert "Years Played" in " ".join(queued["capability_badge_and_achievement_celebrations_v1"]["details"])
    assert queued["capability_player_profile_depth_v1"]["title"] == "Achievement Unlocked: Deeper Player Profiles"
    assert queued["capability_player_profile_depth_v1"]["capability_area"] == "player_profile_depth"
    assert "games-per-day" in queued["capability_player_profile_depth_v1"]["message"]
    assert queued["capability_weekly_clan_recap_v2"]["title"] == "Achievement Unlocked: Weekly Clan Recap"
    assert queued["capability_weekly_clan_recap_v2"]["capability_area"] == "weekly_recap"
    assert "Every Monday" in queued["capability_weekly_clan_recap_v2"]["message"]
    assert "best single snapshot" in " ".join(queued["capability_weekly_clan_recap_v2"]["details"])
    assert queued["capability_long_term_trends_v1"]["title"] == "Achievement Unlocked: Long-Term Trend Tracking"
    assert queued["capability_long_term_trends_v1"]["capability_area"] == "long_term_trends"
    assert "time-series" in queued["capability_long_term_trends_v1"]["message"]
    assert "future charts" in " ".join(queued["capability_long_term_trends_v1"]["details"])
    assert queued["capability_roster_showcase_depth_v1"]["title"] == "Achievement Unlocked: Deeper Roster Showcase"
    assert queued["capability_roster_showcase_depth_v1"]["capability_area"] == "roster_showcase"
    assert "badge highlights" in queued["capability_roster_showcase_depth_v1"]["message"].lower()
    assert queued["capability_poap_kings_integration_v2"]["title"] == "Achievement Unlocked: Formal POAP KINGS Integration"
    assert queued["capability_poap_kings_integration_v2"]["capability_area"] == "poap_kings_integration"
    assert "behind the scenes" in queued["capability_poap_kings_integration_v2"]["message"]
    assert "website publishing now lives in a dedicated integration" in " ".join(
        queued["capability_poap_kings_integration_v2"]["details"]
    )
    assert queued["capability_war_awareness_v1"]["title"] == "Achievement Unlocked: War Awareness"
    assert queued["capability_war_awareness_v1"]["capability_area"] == "war_awareness"
    assert "live game-driven phases" in queued["capability_war_awareness_v1"]["message"]
    assert "day-by-day battle recaps" in " ".join(queued["capability_war_awareness_v1"]["details"])
    assert queued["capability_card_modes_and_war_completion_v1"]["title"] == "Achievement Unlocked: Sharper Card And War Intel"
    assert queued["capability_card_modes_and_war_completion_v1"]["capability_area"] == "war_and_card_intel"
    assert "Hero + Evo" in queued["capability_card_modes_and_war_completion_v1"]["message"]
    assert queued["capability_card_modes_and_war_completion_v1"]["discord_content"].startswith("**Achievement Unlocked: Sharper Card And War Intel**")
    assert "clock-based" in " ".join(queued["capability_card_modes_and_war_completion_v1"]["details"])
    assert "finished the race" in " ".join(queued["capability_card_modes_and_war_completion_v1"]["details"])
    assert queued["capability_subagent_behavior_upgrade_v1"]["title"] == "Achievement Unlocked: Sharper Channel Instincts"
    assert queued["capability_subagent_behavior_upgrade_v1"]["capability_area"] == "subagent_behavior"
    assert "channel to channel" in queued["capability_subagent_behavior_upgrade_v1"]["message"]
    assert queued["capability_subagent_behavior_upgrade_v1"]["discord_content"].startswith("**Achievement Unlocked: Sharper Channel Instincts**")
    assert "#reception" in queued["capability_subagent_behavior_upgrade_v1"]["discord_content"]
    assert "leader" in " ".join(queued["capability_subagent_behavior_upgrade_v1"]["details"]).lower()
    assert queued["capability_ask_elixir_reaction_feedback_v1"]["title"] == "Achievement Unlocked: Ask Elixir Feedback Reactions"
    assert queued["capability_ask_elixir_reaction_feedback_v1"]["capability_area"] == "ask_elixir_feedback"
    assert "thumbs-up" in queued["capability_ask_elixir_reaction_feedback_v1"]["message"]
    assert queued["capability_ask_elixir_reaction_feedback_v1"]["discord_content"].startswith("**Achievement Unlocked: Ask Elixir Feedback Reactions**")
    assert "#ask-elixir" in queued["capability_ask_elixir_reaction_feedback_v1"]["discord_content"]
    assert "review loop" in " ".join(queued["capability_ask_elixir_reaction_feedback_v1"]["details"])


def test_queue_startup_system_signals_can_seed_pending_signal_in_connection():
    conn = elixir.db.get_connection(":memory:")
    try:
        elixir.queue_startup_system_signals(conn=conn)
        pending = elixir.db.list_pending_system_signals(conn=conn)
    finally:
        conn.close()

    from runtime.system_signals import STARTUP_SYSTEM_SIGNALS

    expected_keys = {s["signal_key"] for s in STARTUP_SYSTEM_SIGNALS}
    actual_keys = {item["signal_key"] for item in pending}
    assert actual_keys == expected_keys
    assert len(pending) == len(STARTUP_SYSTEM_SIGNALS)


def test_cr_api_auth_failure_alert_posts_once_per_signature():
    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch("elixir.prompts.discord_channels_by_workflow", return_value=[{"id": 200, "name": "#leader-lounge", "subagent": "leader-lounge", "workflow": "clanops"}]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.format_member_reference", return_value="King Thing (<@704062105258557511>)"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.runtime_status.snapshot", return_value={
            "api": {
                "last_ok": False,
                "last_status_code": 403,
                "last_error": "403 Client Error: Forbidden",
                "last_endpoint": "clan",
                "last_entity_key": "J2RGCRVG",
            }
        }),
    ):
        elixir._CR_API_ALERT_SIGNATURE = None
        first = asyncio.run(elixir._maybe_alert_cr_api_failure("live clan refresh"))
        second = asyncio.run(elixir._maybe_alert_cr_api_failure("live clan refresh"))

    try:
        assert first is True
        assert second is False
        mock_post.assert_awaited_once()
        posted = mock_post.await_args.args[1]["content"]
        assert "King Thing" in posted
        assert "live clan refresh" in posted
        assert "IP allowlist" in posted or "key or its IP allowlist" in posted
        assert mock_save.call_count == 1
        assert mock_save.call_args.kwargs["event_type"] == "cr_api_auth_failure"
    finally:
        elixir._CR_API_ALERT_SIGNATURE = None


def test_cr_api_outage_alert_posts_after_consecutive_failures():
    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch("elixir.prompts.discord_channels_by_workflow", return_value=[{"id": 200, "name": "#leader-lounge", "subagent": "leader-lounge", "workflow": "clanops"}]),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.format_member_reference", return_value="King Thing (<@704062105258557511>)"),
        patch("elixir.db.save_message") as mock_save,
        patch("elixir.runtime_status.snapshot", return_value={
            "api": {
                "last_ok": False,
                "last_status_code": 500,
                "last_error": "500 Server Error: Internal Server Error",
                "last_endpoint": "clan",
                "last_entity_key": "J2RGCRVG",
                "consecutive_error_count": 3,
            }
        }),
    ):
        elixir._CR_API_OUTAGE_ALERT_SIGNATURE = None
        sent = asyncio.run(elixir._maybe_alert_cr_api_failure("player intel refresh"))

    try:
        assert sent is True
        mock_post.assert_awaited_once()
        posted = mock_post.await_args.args[1]["content"]
        assert "failed 3 times in a row" in posted
        assert "player intel refresh" in posted
        assert mock_save.call_args.kwargs["event_type"] == "cr_api_outage"
    finally:
        elixir._CR_API_OUTAGE_ALERT_SIGNATURE = None


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

    assert "clan-events" in report
    assert "clan-awareness" in report
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

    assert "promote-the-clan" in report
    assert "promotion-content" in report
    assert "Discord: #promote-the-clan" in report
    assert "POAP KINGS: promotion payloads" in report
    assert "Every Fri at 09:00 CT." in report


def test_build_schedule_report_includes_weekly_clan_recap():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch.object(elixir, "WEEKLY_RECAP_DAY", "mon"),
        patch.object(elixir, "WEEKLY_RECAP_HOUR", 9),
    ):
        report = elixir._build_schedule_report()

    assert "weekly-recap" in report
    assert "Every Mon at 09:00 CT." in report


def test_build_schedule_report_shows_30_minute_player_intel_refresh():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch.object(elixir, "PLAYER_INTEL_REFRESH_MINUTES", 30),
        patch.object(elixir, "PLAYER_INTEL_REFRESH_JITTER_SECONDS", 900),
    ):
        report = elixir._build_schedule_report()

    assert "player-progress" in report
    assert "player-progression" in report
    assert "Every 30 minutes with up to 900s jitter." in report


def test_build_schedule_report_includes_clock_aligned_war_pipeline():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch.object(elixir, "WAR_POLL_MINUTE", 0),
        patch.object(elixir, "WAR_AWARENESS_MINUTE", 5),
    ):
        report = elixir._build_schedule_report()

    assert "river-race" in report
    assert "war-poll" in report
    assert "Every hour at :00 CT." in report
    assert "war-awareness" in report
    assert "Every hour at :05 CT." in report
    assert "Discord routed outcomes: #river-race, optional #leader-lounge" in report


def test_activity_registry_has_unique_keys_and_required_fields():
    activities = list_registered_activities()

    assert activities
    keys = [activity.activity_key for activity in activities]
    assert len(keys) == len(set(keys))
    assert all(activity.owner_subagent for activity in activities)
    assert all(activity.job_id for activity in activities)
    assert all(activity.job_function for activity in activities)
    assert all(activity.schedule_kind in {"interval", "cron"} for activity in activities)
    assert all(activity.delivery_targets for activity in activities)


def test_activity_registry_exposes_war_and_promotion_visibility():
    specs = {spec["activity_key"]: spec for spec in schedule_specs_from_registry(elixir)}

    assert "war-poll" in specs
    assert specs["war-poll"]["owner_subagent"] == "river-race"
    assert specs["war-poll"]["schedule"] == "Every hour at :00 CT."
    assert "war-awareness" in specs
    assert specs["war-awareness"]["owner_subagent"] == "river-race"
    assert specs["war-awareness"]["schedule"] == "Every hour at :05 CT."
    assert "#river-race" in " ".join(specs["war-awareness"]["delivery_targets"])
    assert "daily-clan-insight" in specs
    assert specs["daily-clan-insight"]["owner_subagent"] == "ask-elixir"
    assert "Discord: #ask-elixir" in specs["daily-clan-insight"]["delivery_targets"]
    assert "Daily at 12:00 CT." in specs["daily-clan-insight"]["schedule"]
    assert "30 minutes jitter" in specs["daily-clan-insight"]["schedule"]
    assert "promotion-content" in specs
    assert "Discord: #promote-the-clan" in specs["promotion-content"]["delivery_targets"]
    assert "POAP KINGS: promotion payloads" in specs["promotion-content"]["delivery_targets"]


def test_activity_registry_registers_scheduler_jobs_from_one_source():
    added = []

    class _Scheduler:
        def add_job(self, func, schedule_kind, id, **kwargs):
            added.append({"func": func, "schedule_kind": schedule_kind, "id": id, "kwargs": kwargs})

    registered = register_scheduled_activities(
        scheduler=_Scheduler(),
        runtime_module=elixir,
        create_task=lambda fn: fn,
    )

    job_ids = {item["id"] for item in added}
    assert {item["activity_key"] for item in registered} == {
        "clan-awareness",
        "war-poll",
        "war-awareness",
        "player-progression",
        "daily-clan-insight",
        "leadership-review",
        "memory-synthesis",
        "weekly-recap",
        "site-content",
        "promotion-content",
        "daily-quiz",
        "card-catalog-sync",
        "db-maintenance",
    }
    assert "war-poll" in job_ids
    assert "war-awareness" in job_ids
    assert "daily-clan-insight" in job_ids
    assert "promotion-content" in job_ids


def test_manual_activity_choices_exclude_internal_war_poll():
    choices = manual_activity_choices()
    values = {value for _, value in choices}

    assert "war-awareness" in values
    assert "war-poll" not in values


def test_build_status_report_omits_job_schedule_section():
    scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [],
    )

    with (
        patch("elixir.scheduler", scheduler),
        patch("elixir.elixir_agent.RELEASE_LABEL", 'v3.0 "Three-Lane Elixir"'),
        patch("elixir.elixir_agent.BUILD_HASH", "abc1234"),
        patch("elixir.runtime_status.snapshot", return_value={
            "started_at": "2026-03-08T10:00:00",
            "env": {
                "has_discord_token": True,
                "has_claude_api_key": True,
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
            "llm": {
                "last_ok": True,
                "last_workflow": "observation",
                "last_model": "claude-sonnet-4-6",
                "last_call_at": "2026-03-08T10:29:00",
                "last_duration_ms": 500,
                "last_prompt_tokens": 100,
                "last_completion_tokens": 50,
                "last_total_tokens": 150,
                "call_count": 3,
                "error_count": 0,
            },
            "jobs": {
                "clan_awareness": {"last_summary": "ok"},
            },
        }),
        patch("elixir.db.get_system_status", return_value={
            "db_path": "/tmp/elixir.db",
            "db_size_bytes": 1024,
            "schema_display": "baseline schema (migration v2)",
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
            "contextual_memory": {
                "sqlite_vec_enabled": True,
                "latest_memory_at": "2026-03-08T10:20:00",
                "total": 7,
                "leader_notes": 3,
                "inferences": 2,
                "system_notes": 2,
            },
        }),
        patch("elixir._member_role_grant_status", return_value={
            "configured": True,
            "ok": False,
            "reason": "Manage Roles permission missing",
            "bot_top_role_position": 2,
            "member_role_position": 3,
            "manage_roles": False,
        }),
    ):
        report = elixir._build_status_report()

    assert "🛠️ Jobs:" not in report
    assert '🏷️ Release: `v3.0 "Three-Lane Elixir"`' in report
    assert "🤖 Build: `abc1234`" in report
    assert "Current war season id: 130" in report
    assert "Member role auto-grant: Manage Roles permission missing" in report
    assert "🧠 Context memory: 7 total (3 leader / 2 inference / 2 system)" in report


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
        patch("elixir.db.build_memory_context", return_value={}),
        patch("elixir.db.save_message") as mock_save,
        patch(
            "agent.intent_router.classify_intent",
            return_value={"route": "help", "confidence": 0.95, "rationale": "asking for help"},
        ),
        patch(
            "elixir.elixir_agent.respond_to_help_request",
            return_value={
                "event_type": "help_response",
                "content": "Ask me about your deck, war participation, or recent form.",
                "summary": "...",
            },
        ) as mock_help,
        patch("elixir.elixir_agent.respond_in_channel") as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    mock_help.assert_called_once()
    message.reply.assert_awaited_once_with(
        "Ask me about your deck, war participation, or recent form."
    )
    assert mock_save.call_args_list[1].kwargs["event_type"] == "interactive_help"
    mock_respond.assert_not_called()
    mock_process.assert_not_awaited()


def test_on_message_handles_roster_join_dates_directly():
    message = _make_message(200, "clan-ops", "<@999> Who are the members of the clan and when did they join?")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.helpers._common.bot", new=SimpleNamespace(user=SimpleNamespace(id=999))),
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
            "agent.intent_router.classify_intent",
            return_value={"route": "roster_join_dates", "confidence": 0.95, "rationale": "asking for join dates"},
        ),
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
            "agent.intent_router.classify_intent",
            return_value={"route": "kick_risk", "confidence": 0.95, "rationale": "asking about kicks"},
        ),
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
            "agent.intent_router.classify_intent",
            return_value={"route": "top_war_contributors", "confidence": 0.95, "rationale": "asking for top contributors"},
        ),
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


def test_build_war_status_report_summarizes_current_war_awareness():
    with (
        patch("elixir.db.get_current_war_status", return_value={
            "clan_name": "POAP KINGS",
            "war_state": "full",
            "season_id": 129,
            "week": 2,
            "phase_display": "Battle Day 2",
            "race_rank": 2,
            "fame": 15400,
            "clan_score": 4780,
            "period_points": 800,
        }),
        patch("elixir.db.get_current_war_day_state", return_value={
            "season_id": 129,
            "section_index": 1,
            "phase": "battle",
            "phase_display": "Battle Day 2",
            "time_left_text": "22h 29m",
            "war_day_key": "s00129-w01-p011",
            "engaged_count": 17,
            "finished_count": 9,
            "untouched_count": 8,
            "total_participants": 25,
            "top_fame_today": [
                {"member_ref": "King Levy", "fame_today": 800},
                {"member_ref": "Finn", "fame_today": 600},
            ],
            "used_none": [
                {"member_ref": "Vijay"},
                {"member_ref": "Ditika"},
            ],
        }),
        patch("elixir.db.get_war_week_summary", return_value={
            "participant_count": 23,
            "top_participants": [
                {"member_ref": "King Levy", "fame": 3200},
                {"member_ref": "Finn", "fame": 2900},
            ],
            "day_summaries": [
                {"phase": "battle", "phase_display": "Battle Day 1", "engaged_count": 20, "finished_count": 11, "top_fame_today": [{"member_ref": "King Levy"}]},
            ],
            "race": None,
        }),
        patch("elixir.db.get_war_season_summary", return_value={
            "races": 2,
            "total_clan_fame": 30100,
            "fame_per_active_member": 1204.0,
            "top_contributors": [
                {"member_ref": "King Levy", "total_fame": 6200},
                {"member_ref": "Finn", "total_fame": 5800},
            ],
            "nonparticipants": [{"member_ref": "Vijay"}],
        }),
        patch("elixir.db.list_recent_war_day_summaries", return_value=[
            {"phase": "battle", "phase_display": "Battle Day 2", "engaged_count": 17, "finished_count": 9, "top_fame_today": [{"member_ref": "King Levy"}]},
            {"phase": "battle", "phase_display": "Battle Day 1", "engaged_count": 20, "finished_count": 11, "top_fame_today": [{"member_ref": "Finn"}]},
        ]),
        patch("elixir.db.get_latest_clan_boat_defense_status", return_value=None),
    ):
        report = elixir._build_war_status_report(
            {"name": "POAP KINGS"},
            {"clans": [{}, {}, {}, {}, {}]},
        )

    assert report.startswith("**POAP KINGS War Status**")
    assert "Live: state full | season 129 | week 2 | Battle Day 2 | rank 2" in report
    assert "Clock: Battle Day 2 | time left 22h 29m | key `s00129-w01-p011`" in report
    assert "Engagement: 17 engaged | 9 finished all 4 | 8 untouched | 25 tracked" in report
    assert "Leaders today: King Levy 800, Finn 600" in report
    assert "Waiting on: Vijay, Ditika" in report
    assert "This season: 2 race(s) | total fame 30,100 | fame/member 1,204.00 | top King Levy 6,200, Finn 5,800" in report
    assert "Live feed: 5 clan(s) in the current river race" in report


def test_build_war_status_report_includes_live_finish_and_known_stakes():
    with (
        patch("elixir.db.get_current_war_status", return_value={
            "clan_name": "POAP KINGS",
            "war_state": "full",
            "season_id": 130,
            "week": 2,
            "phase_display": "Battle Day 3",
            "race_rank": 1,
            "fame": 10146,
            "clan_score": 160,
            "period_points": 10146,
            "race_completed": True,
            "finish_time": "20260315T095605.000Z",
            "race_completed_early": True,
            "trophy_stakes_known": True,
            "trophy_stakes_text": "100 trophies on the line",
        }),
        patch("elixir.db.get_current_war_day_state", return_value={}),
        patch("elixir.db.get_war_week_summary", return_value=None),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.list_recent_war_day_summaries", return_value=[]),
        patch("elixir.db.get_latest_clan_boat_defense_status", return_value=None),
    ):
        report = elixir._build_war_status_report(
            {"name": "POAP KINGS"},
            {"clans": [{}, {}, {}, {}, {}]},
        )

    assert "finished yes" in report
    assert "finish 20260315T095605.000Z" in report
    assert "completed early" in report
    assert "stakes 100 trophies on the line" in report


def test_build_db_status_report_lists_group_summaries():
    with patch("elixir.db.get_database_status", return_value={
        "db_path": "/tmp/elixir.db",
        "schema_version": 15,
        "db_size_bytes": 40960,
        "wal_size_bytes": 8192,
        "shm_size_bytes": 32768,
        "page_size": 4096,
        "page_count": 10,
        "freelist_count": 2,
        "journal_mode": "wal",
        "table_count": 3,
        "tables": [
            {"name": "messages", "row_count": 1200, "approx_bytes": 24576},
            {"name": "war_participant_snapshots", "row_count": 320, "approx_bytes": 12288},
            {"name": "members", "row_count": 50, "approx_bytes": 4096},
        ],
    }):
        report = elixir._build_db_status_report()

    assert report.startswith("**Elixir DB Status**")
    assert "File: `elixir.db` | schema v15 | size 40.0 KB | WAL 8.0 KB | SHM 32.0 KB" in report
    assert "Storage: page size 4,096 B | pages 10 | free pages 2 | journal wal | tables 3" in report
    assert (
        "Use `/elixir system storage` for the full rollup or "
        "`/elixir system storage view:<all|clan|war|memory>` for a focused section."
    ) in report
    assert "Clan: 1 tables | 50 rows | 4.0 KB" in report
    assert "War: 1 tables | 320 rows | 12.0 KB" in report
    assert "Memory: 1 tables | 1,200 rows | 24.0 KB" in report
    assert "  - members: 50 rows | 4.0 KB" in report
    assert "  - war_participant_snapshots: 320 rows | 12.0 KB" in report
    assert "  - messages: 1,200 rows | 24.0 KB" in report


def test_build_db_status_report_lists_table_counts_and_sizes_for_group():
    with patch("elixir.db.get_database_status", return_value={
        "db_path": "/tmp/elixir.db",
        "schema_version": 15,
        "db_size_bytes": 40960,
        "wal_size_bytes": 8192,
        "shm_size_bytes": 32768,
        "page_size": 4096,
        "page_count": 10,
        "freelist_count": 2,
        "journal_mode": "wal",
        "table_count": 3,
        "tables": [
            {"name": "messages", "row_count": 1200, "approx_bytes": 24576},
            {"name": "war_participation", "row_count": 320, "approx_bytes": 12288},
            {"name": "members", "row_count": 50, "approx_bytes": 4096},
        ],
    }):
        report = elixir._build_db_status_report(group="memory")

    assert report.startswith("**Elixir DB Status | Memory**")
    assert "Group: 1 tables | 1,200 rows | 24.0 KB" in report
    assert "messages: 1,200 rows | 24.0 KB" in report
    assert "war_participation" not in report


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


def test_reply_text_resolves_custom_emoji_shortcodes():
    guild = SimpleNamespace(emojis=[SimpleNamespace(name="elixir_trophy", id=987, animated=False)])
    message = _make_message(200, "ask-elixir", "nice")
    message.guild = guild

    asyncio.run(elixir._reply_text(message, "Huge climb today :elixir_trophy:"))

    message.reply.assert_awaited_once_with("Huge climb today <:elixir_trophy:987>")


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
    assert "- **POAP KINGS**: 21/50 active | **elders** 5 | **target elder band** 4-6 | **remaining elder capacity** 1" in report
    assert "⬆️ **Promote now (1):** King Levy (<@1474760692992180429>) — 220 donations, 4 war races, 90d tenure, seen 0d ago" in report
    assert "⚠️ **Borderline (1):** Finn — 120 donations, 2 war races, 20d tenure, seen 1d ago" in report
    assert "⬇️ **Demotion/kick watch (1):** Vijay — last seen 8 days ago; 0 donations this week" in report
    assert "💤 **No war decks this season (1):** Chanco" in report
    assert "🚤 **Current river race:** fame 12,345 | repair 120 | score 4,560" in report


def test_build_weekly_clan_recap_context_summarizes_week():
    with (
        patch("elixir.db.get_weekly_digest_summary", return_value={
            "window_days": 7,
            "roster": {"active_members": 21, "open_slots": 29, "avg_exp_level": 60.5, "avg_trophies": 7523.4, "donations_week_total": 1400},
            "war_score_trend": {"direction": "up", "score_change": 120, "trophy_change_total": 40, "races": 1, "avg_rank": 1.0, "avg_fame": 12345},
            "war_season_summary": {"season_id": 77, "races": 3, "total_clan_fame": 50234, "fame_per_active_member": 2392.1, "top_contributors": [{"member_ref": "King Levy", "total_fame": 3200}]},
            "recent_war_races": [{
                "season_id": 77,
                "week": 2,
                "our_rank": 1,
                "total_clans": 5,
                "our_fame": 12345,
                "trophy_change": 20,
                "created_date": "20260308T180000.000Z",
                "top_participants": [{"member_ref": "King Levy", "fame": 3200, "decks_used": 4}],
                "standings_preview": [{"rank": 1, "name": "POAP KINGS", "fame": 12345}],
            }],
            "trending_war_contributors": {"members": [{"member_ref": "Finn", "fame_delta": 400}]},
            "progression_highlights": [{"member_ref": "Vijay", "level_gain": 1, "pol_league_gain": 1, "best_trophies_gain": 120, "trophies_change": 95, "wins_gain": 18, "favorite_card": "Hog Rider"}],
            "trophy_risers": [{"name": "Vijay", "change": 95, "old_trophies": 7000, "new_trophies": 7095}],
            "trophy_drops": [],
            "hot_streaks": [{"member_ref": "Finn", "current_streak": 5, "summary": "8-2 over the last 10 battles (hot)."}],
            "top_donors": [{"member_ref": "Jamie", "donations_week": 220}],
            "recent_joins": [{"member_ref": "Newbie", "joined_date": "2026-03-08"}],
        }),
        patch("elixir.db.build_clan_trend_summary_context", return_value="=== CLAN TREND SUMMARY ===\nclan: POAP KINGS (#J2RGCRVG)"),
    ):
        report = elixir._build_weekly_clan_recap_context(
            {"name": "POAP KINGS", "tag": "#J2RGCRVG"},
            {"clan": {"fame": 13000, "repairPoints": 30, "clanScore": 4600, "participants": [{"tag": "#A"}]}},
        )

    assert "=== WEEKLY CLAN RECAP SNAPSHOT ===" in report
    assert "recent river races:" in report
    assert "=== PLAYER PROGRESSION HIGHLIGHTS ===" in report
    assert "=== CLAN TREND SUMMARY ===" in report
    assert "battle pulse heaters: Finn won 5 straight" in report
    assert "recent joins this week: Newbie" in report


def test_share_channel_result_rewrites_member_refs_before_posting():
    channel = AsyncMock()
    channel.id = 300
    channel.name = "announcements"
    channel.type = "text"

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def fake_format_member_reference(tag, conn=None, **_kwargs):
        return "King Levy" if tag == "#ABC123" else tag

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch("elixir.db.format_member_reference", side_effect=fake_format_member_reference),
        patch("elixir.prompts.resolve_channel_reference", return_value={"id": 300, "role": "announcements", "name": "#announcements"}),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        asyncio.run(
            elixir._share_channel_result(
                {
                    "event_type": "channel_share",
                    "share_content": "King Levy had a great week.",
                    "share_channel": "#announcements",
                    "member_tags": ["#ABC123"],
                },
                "clanops",
            )
        )

    mock_post.assert_awaited_once_with(channel, {"content": "King Levy had a great week."})
    assert mock_save.call_args.args[2] == "King Levy had a great week."
