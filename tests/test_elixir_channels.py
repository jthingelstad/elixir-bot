"""Tests for channel-role routing in elixir.py."""

import asyncio
import base64
import io
import json
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, PropertyMock, patch

import pytest
from PIL import Image

import elixir
import runtime.channel_router as channel_router
from engine.management import KICK_AT_RISK_DAYS
from runtime.activities import (
    list_registered_activities,
    manual_activity_choices,
    register_scheduled_activities,
    schedule_specs_from_registry,
)
from runtime.admin import COMMAND_SPECS, admin_command_requires_leader
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


def _make_message(
    channel_id, channel_name, content, *, mentions=None, roles=None, attachments=None
):
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
        attachments=attachments or [],
        mentions=mentions or [],
        role_mentions=[],
        id=555,
        reference=None,
        add_reaction=AsyncMock(),
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


def _root(bot, name):
    """Fetch a top-level command group by name — the surface is now split into
    two roots: /elixir (member: email + help) and /clanops (leader ops)."""
    return next(c for c in bot.tree.commands if c.name == name)


# --- shared on_message scaffolding -------------------------------------------
#
# `on_message` is the widest entry point in the runtime: one call reads channel
# config, writes two rows, may classify intent, may call the LLM, and replies.
# Every test of it therefore needs the same block of stubs, and that block --
# not the assertions -- used to be the bulk of this file (~1,200 lines of
# copy-pasted `with (patch...)`). `_on_message_env` is that block, once.

_UNSET = object()

# Pass as a stub's value to mean "patch it so nothing real runs, but give it no
# canned return" -- for the tests whose point is that it is never called.
NEVER_CALLED = object()

# The channel behaviors on_message tests route through. Copied from the real
# prompts.discord_channel_configs() shapes; ids 100/200 are synthetic stand-ins
# for a member lane and the leader lane.
MEMBER_CHAT_BEHAVIOR = {
    "id": 100,
    "name": "#member-chat",
    "role": "interactive",
    "workflow": "interactive",
    "mention_required": True,
    "allow_proactive": False,
}

ASK_ELIXIR_ID = 1482368505058955467
ASK_ELIXIR_BEHAVIOR = {
    "id": ASK_ELIXIR_ID,
    "name": "#ask-elixir",
    "lane": "ask-elixir",
    "workflow": "interactive",
    "reply_policy": "open_channel",
}

CLANOPS_BEHAVIOR = {
    "id": 200,
    "name": "#clan-ops",
    "role": "clanops",
    "workflow": "clanops",
    "mention_required": False,
    "allow_proactive": True,
}


def _behavior(base, **overrides):
    """A channel behavior with fields added/changed for one test."""
    return {**base, **overrides}


def _stub(stack, target, value, **patch_kwargs):
    """Enter `patch(target)`, wiring `value` as the return unless it is the
    NEVER_CALLED sentinel."""
    if value is NEVER_CALLED:
        return stack.enter_context(patch(target, **patch_kwargs))
    return stack.enter_context(patch(target, return_value=value, **patch_kwargs))


@contextmanager
def _on_message_env(
    behavior,
    *,
    mentioned=_UNSET,
    bot_user_id=_UNSET,
    history=_UNSET,
    memory_context=_UNSET,
    clan_context=_UNSET,
    classify=_UNSET,
    respond=_UNSET,
    share=False,
    reply_text=_UNSET,
):
    """Stub the process-level surface an `on_message` test needs.

    Always stubbed (no test of on_message can run without these):
      * `bot.process_commands` — the slash-command fall-through; tests assert it
        was or was not awaited.
      * `asyncio.to_thread` — runs the callable inline so the stubbed DB calls
        stay synchronous and assertable.
      * `_get_channel_behavior` — the channel config under test.
      * `db.upsert_discord_user` / `db.save_message` — the live DB is a real
        file next to the tests; nothing here may write to it.

    Opt-in stubs (pass the keyword to enable; omit to leave the real thing in
    place). Pass NEVER_CALLED for one to stub it with no canned return:
      mentioned, bot_user_id, history, memory_context, clan_context, classify,
      respond, share, reply_text.

    Yields a namespace of the mocks (`env.save`, `env.respond`, ...). Anything
    a single test needs beyond this stays a nested `patch()` at the call site.
    """

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with ExitStack() as stack:
        env = SimpleNamespace(
            process=stack.enter_context(
                patch.object(elixir.bot, "process_commands", new=AsyncMock())
            ),
            to_thread=stack.enter_context(
                patch("elixir.asyncio.to_thread", side_effect=fake_to_thread)
            ),
            behavior=_stub(stack, "elixir._get_channel_behavior", behavior),
            upsert=_stub(stack, "elixir.db.upsert_discord_user", NEVER_CALLED),
            save=_stub(stack, "elixir.db.save_message", NEVER_CALLED),
            mentioned=None,
            bot=None,
            history=None,
            memory=None,
            clan=None,
            classify=None,
            respond=None,
            share=None,
            reply_text=None,
        )
        if mentioned is not _UNSET:
            env.mentioned = _stub(stack, "elixir._is_bot_mentioned", mentioned)
        if bot_user_id is not _UNSET:
            env.bot = stack.enter_context(
                patch(
                    "runtime.helpers._common.bot",
                    new=SimpleNamespace(user=SimpleNamespace(id=bot_user_id)),
                )
            )
        if history is not _UNSET:
            env.history = _stub(stack, "elixir.db.list_thread_messages", history)
        if memory_context is not _UNSET:
            env.memory = _stub(stack, "elixir.db.build_memory_context", memory_context)
        if clan_context is not _UNSET:
            env.clan = stack.enter_context(
                patch(
                    "elixir._load_live_clan_context",
                    new=AsyncMock(return_value=clan_context),
                )
            )
        if classify is not _UNSET:
            env.classify = _stub(stack, "agent.intent_router.classify_intent", classify)
        if respond is not _UNSET:
            env.respond = _stub(stack, "elixir.elixir_agent.respond_in_channel", respond)
        if share:
            env.share = stack.enter_context(patch("elixir._share_channel_result", new=AsyncMock()))
        if reply_text is not _UNSET:
            env.reply_text = stack.enter_context(patch("elixir._reply_text", new=reply_text))
        yield env


def test_on_message_routes_interactive_channel_when_mentioned():
    message = _make_message(100, "member-chat", "<@999> how am I doing?")

    with _on_message_env(
        MEMBER_CHAT_BEHAVIOR,
        bot_user_id=999,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        respond={
            "event_type": "channel_response",
            "content": "You look solid.",
            "summary": "solid",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    assert env.respond.call_args.kwargs["workflow"] == "interactive"
    env.history.assert_called_once_with("channel_user:100:123", elixir.CHANNEL_CONVERSATION_LIMIT)
    message.reply.assert_awaited_once_with("You look solid.")
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_on_message_routes_ask_elixir_without_mention():
    message = _make_message(1482368505058955467, "ask-elixir", "what deck should I learn next?")
    sent_message = SimpleNamespace(id=987)
    message.reply = AsyncMock(return_value=sent_message)

    with _on_message_env(
        ASK_ELIXIR_BEHAVIOR,
        mentioned=False,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        respond={
            "event_type": "channel_response",
            "content": "Try a deck with faster cycles so you can learn matchups quicker.",
            "summary": "learn a faster deck",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    assert env.respond.call_args.kwargs["workflow"] == "interactive"
    assert env.respond.call_args.kwargs["channel_name"] == "#ask-elixir"
    env.history.assert_called_once_with(
        "channel_user:1482368505058955467:123", elixir.CHANNEL_CONVERSATION_LIMIT
    )
    message.reply.assert_awaited_once_with(
        "Try a deck with faster cycles so you can learn matchups quicker."
    )
    assistant_save = [call for call in env.save.call_args_list if call.args[1] == "assistant"][0]
    assert assistant_save.kwargs["discord_message_id"] == "987"
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_on_message_keeps_open_ask_elixir_not_for_bot_in_llm_path():
    message = _make_message(
        1482368505058955467, "ask-elixir", "Who is donating beast in our clan???"
    )
    message.reply = AsyncMock(return_value=SimpleNamespace(id=990))

    with _on_message_env(
        _behavior(ASK_ELIXIR_BEHAVIOR, memory_scope="public"),
        mentioned=False,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        classify={
            "route": "not_for_bot",
            "confidence": 0.95,
            "rationale": "asking clan members about donations",
        },
        respond={
            "event_type": "channel_response",
            "content": "I can check donation leaders from the current clan data.",
            "summary": "donations",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    env.classify.assert_called_once()
    assert env.classify.call_args.kwargs["allows_open_channel_reply"] is True
    env.respond.assert_called_once()
    message.reply.assert_awaited_once_with(
        "I can check donation leaders from the current clan data."
    )
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_on_message_ignores_blank_ask_elixir_mention_before_intent_routing():
    message = _make_message(1482368505058955467, "ask-elixir", "<@1477043197443182832>")

    with (
        _on_message_env(
            _behavior(ASK_ELIXIR_BEHAVIOR, memory_scope="public"),
            mentioned=True,
            history=NEVER_CALLED,
            classify=NEVER_CALLED,
            respond=NEVER_CALLED,
        ) as env,
        patch("elixir._strip_bot_mentions", return_value=""),
        patch("elixir.elixir_agent.respond_in_deck_review") as mock_review,
    ):
        asyncio.run(elixir.on_message(message))

    env.history.assert_not_called()
    env.classify.assert_not_called()
    mock_review.assert_not_called()
    env.respond.assert_not_called()
    env.save.assert_not_called()
    message.reply.assert_not_awaited()
    env.process.assert_not_awaited()


def test_on_message_routes_ask_elixir_image_only_screenshot():
    attachment = SimpleNamespace(
        filename="clan-chat.jpg",
        content_type="image/jpeg",
        size=9,
        read=AsyncMock(return_value=b"fakeimage"),
    )
    message = _make_message(
        1482368505058955467,
        "ask-elixir",
        "",
        attachments=[attachment],
    )
    sent_message = SimpleNamespace(id=988)
    message.reply = AsyncMock(return_value=sent_message)

    with _on_message_env(
        ASK_ELIXIR_BEHAVIOR,
        mentioned=False,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        classify={
            "route": "llm_chat",
            "confidence": 0.75,
            "rationale": "screenshot question",
        },
        respond={
            "event_type": "channel_response",
            "content": "I can read this clan chat screenshot.",
            "summary": "screenshot",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    attachment.read.assert_awaited_once()
    kwargs = env.respond.call_args.kwargs
    assert "Shared Clash Royale screenshot image" in kwargs["question"]
    assert "clan-chat.jpg" in kwargs["question"]
    assert kwargs["image_blocks"] == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "ZmFrZWltYWdl",
            },
        }
    ]
    user_save = [call for call in env.save.call_args_list if call.args[1] == "user"][0]
    assert "Shared Clash Royale screenshot image" in user_save.args[2]
    message.reply.assert_awaited_once_with("I can read this clan chat screenshot.")
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_on_message_corrects_mislabeled_screenshot_media_type():
    attachment = SimpleNamespace(
        filename="clan-chat.png",
        content_type="image/jpeg",
        size=16,
        read=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfakepng"),
    )
    message = _make_message(
        1482368505058955467,
        "ask-elixir",
        "",
        attachments=[attachment],
    )
    message.reply = AsyncMock(return_value=SimpleNamespace(id=988))

    with _on_message_env(
        ASK_ELIXIR_BEHAVIOR,
        mentioned=False,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        classify={
            "route": "llm_chat",
            "confidence": 0.75,
            "rationale": "screenshot question",
        },
        respond={
            "event_type": "channel_response",
            "content": "I can read this clan chat screenshot.",
            "summary": "screenshot",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    kwargs = env.respond.call_args.kwargs
    assert kwargs["image_blocks"][0]["source"]["media_type"] == "image/png"
    env.process.assert_not_awaited()


def test_on_message_passes_screenshot_to_deck_review():
    attachment = SimpleNamespace(
        filename="deck.png",
        content_type="image/png",
        size=7,
        read=AsyncMock(return_value=b"deckpic"),
    )
    message = _make_message(
        1482368505058955467,
        "ask-elixir",
        "Can you review this deck?",
        attachments=[attachment],
    )
    sent_message = SimpleNamespace(id=989)
    message.reply = AsyncMock(return_value=sent_message)

    with (
        _on_message_env(
            _behavior(ASK_ELIXIR_BEHAVIOR, memory_scope="public"),
            mentioned=False,
            history=[],
            memory_context={},
            classify={
                "route": "deck_review",
                "mode": "regular",
                "target_member": "self",
                "confidence": 0.92,
                "rationale": "asking for deck review",
            },
        ) as env,
        patch("elixir._extract_member_deck_target", return_value="#ABC123"),
        patch("elixir.db.get_member_profile", return_value={"current_name": "King Thing"}),
        patch(
            "elixir.elixir_agent.respond_in_deck_review",
            return_value={
                "event_type": "deck_review_response",
                "content": "This deck has a clear win condition.",
                "summary": "deck",
            },
        ) as mock_review,
    ):
        asyncio.run(elixir.on_message(message))

    attachment.read.assert_awaited_once()
    kwargs = mock_review.call_args.kwargs
    assert "Can you review this deck?" in kwargs["question"]
    assert "deck.png" in kwargs["question"]
    assert kwargs["target_member_tag"] == "#ABC123"
    assert kwargs["image_blocks"][0]["source"]["media_type"] == "image/png"
    assert kwargs["image_blocks"][0]["source"]["data"] == "ZGVja3BpYw=="
    message.reply.assert_awaited_once_with("This deck has a clear win condition.")
    env.process.assert_not_awaited()


def test_on_message_routes_reception_without_mention():
    message = _make_message(1476456514121109514, "reception", "how do I get verified?")

    with (
        _on_message_env(
            {
                "id": 1476456514121109514,
                "name": "#welcome",
                "lane": "reception",
                "workflow": "reception",
                "reply_policy": "open_channel",
                "memory_scope": "public",
            },
            mentioned=False,
            memory_context={},
            reply_text=AsyncMock(),
        ) as env,
        patch(
            "runtime.channel_router.cr_api.get_clan",
            return_value={"memberList": [{"tag": "#ABC123", "name": "King Levy"}]},
        ),
        patch(
            "runtime.channel_router.elixir_agent.respond_in_reception",
            return_value={
                "event_type": "reception_response",
                "content": "Set your server nickname to your Clash name and I can help verify you.",
            },
        ) as mock_respond,
    ):
        asyncio.run(elixir.on_message(message))

    assert mock_respond.call_args.kwargs["question"] == "how do I get verified?"
    env.reply_text.assert_awaited_once_with(
        message,
        "Set your server nickname to your Clash name and I can help verify you.",
    )
    env.process.assert_not_awaited()


def test_on_message_does_not_save_unsent_interactive_reply():
    message = _make_message(100, "member-chat", "<@999> how am I doing?")

    with _on_message_env(
        MEMBER_CHAT_BEHAVIOR,
        bot_user_id=999,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        respond={
            "event_type": "channel_response",
            "content": "You look solid.",
            "summary": "solid",
        },
        share=True,
        reply_text=AsyncMock(side_effect=RuntimeError("send failed")),
    ) as env:
        asyncio.run(elixir.on_message(message))

    assistant_saves = [call for call in env.save.call_args_list if call.args[1] == "assistant"]
    assert assistant_saves == []
    env.share.assert_not_awaited()
    message.reply.assert_awaited_once_with("Hit an error. Try again in a moment.")
    env.process.assert_not_awaited()


def test_on_raw_reaction_add_marks_actions_action_done():
    payload = SimpleNamespace(
        channel_id=1513758211206025227,
        message_id=987,
        user_id=123,
        emoji="✅",
        member=SimpleNamespace(bot=False, roles=[SimpleNamespace(id=elixir.LEADER_ROLE_ID)]),
    )

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1513758211206025227,
                "name": "#leader-actions",
                "lane": "actions",
            },
        ),
        patch(
            "runtime.prompt_feedback.db.decide_leader_action_by_message",
            return_value={
                "action_id": 42,
                "action_type": "in_game_relay",
                "status": "done",
            },
        ) as mock_decide,
        patch(
            "runtime.prompt_feedback.queue_leader_action_feedback_refresh"
        ) as mock_feedback_refresh,
        patch(
            "runtime.prompt_feedback.refresh_leader_action_card", new=AsyncMock()
        ) as mock_refresh_card,
    ):
        asyncio.run(elixir.on_raw_reaction_add(payload))

    mock_decide.assert_called_once_with(
        987,
        status="done",
        discord_user_id=123,
        emoji="✅",
    )
    mock_feedback_refresh.assert_called_once_with("in_game_relay")
    mock_refresh_card.assert_awaited_once()
    assert mock_refresh_card.await_args.args[1]["status"] == "done"


def test_actions_reply_records_action_note():
    message = _make_message(
        1513758211206025227,
        "actions",
        "boat defenses full already",
        roles=[SimpleNamespace(id=elixir.LEADER_ROLE_ID)],
    )
    message.reference = SimpleNamespace(message_id=987)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1513758211206025227,
                "name": "#leader-actions",
                "lane": "actions",
                "workflow": "channel_update",
                "reply_policy": "disabled",
                "memory_scope": "leadership",
            },
        ),
        patch("runtime.channel_router.db.upsert_discord_user"),
        patch(
            "runtime.channel_router.db.record_leader_action_note_by_message",
            return_value={
                "action_id": 1,
                "action_type": "in_game_relay",
            },
        ) as mock_note,
        patch("runtime.channel_router.db.save_message") as mock_save,
        patch(
            "runtime.channel_router.queue_leader_action_feedback_refresh"
        ) as mock_feedback_refresh,
    ):
        asyncio.run(elixir.on_message(message))

    mock_note.assert_called_once_with(
        987,
        note="boat defenses full already",
        discord_user_id=123,
        note_message_id=555,
    )
    mock_feedback_refresh.assert_called_once_with("in_game_relay")
    assert mock_save.call_args.kwargs["event_type"] == "leader_action_note"
    message.add_reaction.assert_awaited_once_with("✅")
    mock_process.assert_not_awaited()


def test_actions_leader_screenshot_is_observed():
    attachment = SimpleNamespace(
        filename="boat-defense.jpg",
        content_type="image/jpeg",
        size=9,
        read=AsyncMock(return_value=b"boatstate"),
    )
    message = _make_message(
        1513758211206025227,
        "actions",
        "",
        roles=[SimpleNamespace(id=elixir.LEADER_ROLE_ID)],
        attachments=[attachment],
    )
    message.reply = AsyncMock(return_value=SimpleNamespace(id=990))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1513758211206025227,
                "name": "#leader-actions",
                "lane": "actions",
                "workflow": "channel_update",
                "reply_policy": "disabled",
                "memory_scope": "leadership",
            },
        ),
        patch("runtime.channel_router.db.upsert_discord_user"),
        patch("runtime.channel_router.db.build_memory_context", return_value={}),
        patch("runtime.channel_router.db.save_message", return_value=111) as mock_save,
        patch(
            "runtime.channel_router.elixir_agent.analyze_leader_screenshot",
            return_value={
                "event_type": "leader_screenshot_observation",
                "summary": "Boat defenses still have visible open slots.",
                "content": "**👁️ Screenshot Read**\nVisible open boat-defense slots: at least 3.",
                "observation": {
                    "screenshot_type": "boat_defense",
                    "players": ["dez42"],
                    "actionable_facts": ["At least three open defense slots are visible."],
                    "uncertainty": None,
                },
            },
        ) as mock_analyze,
    ):
        asyncio.run(elixir.on_message(message))

    attachment.read.assert_awaited_once()
    question = mock_analyze.call_args.args[0]
    kwargs = mock_analyze.call_args.kwargs
    assert "Shared Clash Royale screenshot image" in question
    assert "boat-defense.jpg" in question
    assert kwargs["channel_name"] == "#leader-actions"
    assert kwargs["image_blocks"][0]["source"]["media_type"] == "image/jpeg"
    assert kwargs["image_blocks"][0]["source"]["data"] == "Ym9hdHN0YXRl"
    event_types = [call.kwargs.get("event_type") for call in mock_save.call_args_list]
    assert event_types == [
        "leader_screenshot_observation_input",
        "leader_screenshot_observation",
    ]
    message.reply.assert_awaited_once_with(
        "**👁️ Screenshot Read**\nVisible open boat-defense slots: at least 3."
    )
    mock_process.assert_not_awaited()


def test_leader_screenshot_persists_structured_memories():
    attachment = SimpleNamespace(
        filename="clan-chat.png",
        content_type="image/png",
        size=9,
        read=AsyncMock(return_value=b"chatstate"),
    )
    message = _make_message(
        1513758211206025227,
        "actions",
        "",
        roles=[SimpleNamespace(id=elixir.LEADER_ROLE_ID)],
        attachments=[attachment],
    )
    message.reply = AsyncMock(return_value=SimpleNamespace(id=990))
    memories = [
        {
            "title": "Fullboat limited availability",
            "body": "Screenshot shows Fullboat said they will be camping for a week and may have limited signal.",
            "member_tag": "Fullboat",
            "confidence": 0.9,
            "tags": ["availability"],
        }
    ]

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1513758211206025227,
                "name": "#leader-actions",
                "lane": "actions",
                "workflow": "channel_update",
                "reply_policy": "disabled",
                "memory_scope": "leadership",
            },
        ),
        patch("runtime.channel_router.db.upsert_discord_user"),
        patch("runtime.channel_router.db.build_memory_context", return_value={}),
        patch("runtime.channel_router.db.save_message", return_value=111),
        patch(
            "runtime.channel_router.elixir_agent.analyze_leader_screenshot",
            return_value={
                "event_type": "leader_screenshot_observation",
                "summary": "Fullboat has limited availability.",
                "content": "**Read:** Fullboat is camping with limited signal.",
                "memories": memories,
            },
        ),
        patch(
            "runtime.channel_router._persist_screenshot_memories", return_value=1
        ) as mock_persist,
    ):
        asyncio.run(elixir.on_message(message))

    mock_persist.assert_called_once_with(
        memories,
        1513758211206025227,
        "actions",
        555,
    )
    message.reply.assert_awaited_once_with("**Read:** Fullboat is camping with limited signal.")
    mock_process.assert_not_awaited()


def test_persist_screenshot_memories_saves_elixir_inference_with_evidence():
    memories = [
        {
            "title": "Fullboat limited availability",
            "body": "Screenshot shows Fullboat said they will be camping for a week and may have limited signal.",
            "member_tag": "Fullboat",
            "confidence": 1.0,
            "tags": ["availability"],
        }
    ]

    with (
        patch("agent.tool_exec._resolve_member_tag", return_value="#ABC123") as mock_resolve,
        patch("memory_store.create_memory", return_value={"memory_id": 42}) as mock_create,
        patch("memory_store.attach_tags") as mock_tags,
        patch("memory_store.attach_evidence_ref") as mock_evidence,
    ):
        saved = channel_router._persist_screenshot_memories(
            memories,
            channel_id=1513758211206025227,
            workflow="actions",
            source_message_id=555,
        )

    assert saved == 1
    mock_resolve.assert_called_once_with("Fullboat")
    kwargs = mock_create.call_args.kwargs
    assert kwargs["source_type"] == "elixir_inference"
    assert kwargs["is_inference"] is True
    assert kwargs["confidence"] == 0.95
    assert kwargs["scope"] == "leadership"
    assert kwargs["member_tag"] == "#ABC123"
    assert kwargs["channel_id"] == "1513758211206025227"
    assert kwargs["event_type"] == "leader_screenshot_fact"
    assert kwargs["event_id"] == "555"
    assert kwargs["metadata"]["source"] == "leader_screenshot"
    mock_tags.assert_called_once_with(
        42,
        ["screenshot", "actions", "availability"],
        actor="elixir:actions-screenshot",
    )
    mock_evidence.assert_called_once()
    assert mock_evidence.call_args.kwargs["evidence_ref"] == "555"


def test_actions_leader_multi_screenshot_corrects_media_types():
    attachments = [
        SimpleNamespace(
            filename=f"IMG_333{idx}.png",
            content_type="image/jpeg",
            size=16,
            read=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfakepng"),
        )
        for idx in range(3)
    ]
    message = _make_message(
        1513758211206025227,
        "actions",
        "",
        roles=[SimpleNamespace(id=elixir.LEADER_ROLE_ID)],
        attachments=attachments,
    )
    message.reply = AsyncMock(return_value=SimpleNamespace(id=990))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1513758211206025227,
                "name": "#leader-actions",
                "lane": "actions",
                "workflow": "channel_update",
                "reply_policy": "disabled",
                "memory_scope": "leadership",
            },
        ),
        patch("runtime.channel_router.db.upsert_discord_user"),
        patch("runtime.channel_router.db.build_memory_context", return_value={}),
        patch("runtime.channel_router.db.save_message", return_value=111),
        patch(
            "runtime.channel_router.elixir_agent.analyze_leader_screenshot",
            return_value={
                "event_type": "leader_screenshot_observation",
                "summary": "Read multiple screenshots.",
                "content": "**👁️ Screenshot Read**\nI read all three.",
            },
        ) as mock_analyze,
    ):
        asyncio.run(elixir.on_message(message))

    kwargs = mock_analyze.call_args.kwargs
    assert len(kwargs["image_blocks"]) == 3
    assert [block["source"]["media_type"] for block in kwargs["image_blocks"]] == [
        "image/png",
        "image/png",
        "image/png",
    ]
    mock_process.assert_not_awaited()


def test_collect_screenshot_payload_resizes_large_images():
    raw = io.BytesIO()
    Image.new("RGB", (1400, 2600), (20, 80, 140)).save(raw, format="PNG")
    data = raw.getvalue()
    attachment = SimpleNamespace(
        filename="tall-screenshot.png",
        content_type="image/png",
        size=len(data),
        read=AsyncMock(return_value=data),
    )
    message = _make_message(
        1513758211206025227,
        "actions",
        "",
        roles=[SimpleNamespace(id=elixir.LEADER_ROLE_ID)],
        attachments=[attachment],
    )

    blocks, metadata = asyncio.run(channel_router._collect_screenshot_image_payload(message))

    attachment.read.assert_awaited_once()
    assert len(blocks) == 1
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    submitted = base64.b64decode(blocks[0]["source"]["data"])
    assert len(submitted) < len(data)
    assert metadata[0]["resized"] is True
    assert metadata[0]["reencoded"] is True
    assert (
        max(metadata[0]["width"], metadata[0]["height"]) == channel_router.MAX_SCREENSHOT_LONG_EDGE
    )


@pytest.mark.parametrize(
    "emoji,feedback_id,became_active_down,invites_retry",
    [
        pytest.param("\U0001f44e", 44, True, True, id="thumbs_down_invites_a_retry"),
        pytest.param("\U0001f44d", 45, False, False, id="thumbs_up_acknowledges_receipt_only"),
    ],
)
def test_on_raw_reaction_add_records_feedback(
    emoji, feedback_id, became_active_down, invites_retry
):
    """Both reactions record feedback and acknowledge with the same check mark;
    only the thumbs-down offers a retry and records that the retry was invited.
    A thumbs-up that replied would turn every bit of praise into more noise in
    the channel, so the negative assertions on the up case are the point.
    """
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=123,
        emoji=emoji,
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
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1482368505058955467,
                "name": "#ask-elixir",
                "lane": "ask-elixir",
            },
        ),
        patch(
            "runtime.prompt_feedback.db.get_message_by_discord_message_id",
            return_value=assistant_row,
        ),
        patch(
            "runtime.prompt_feedback.db.upsert_prompt_feedback",
            return_value={
                "prompt_feedback_id": feedback_id,
                "became_active_down": became_active_down,
            },
        ) as mock_upsert,
        patch("runtime.prompt_feedback.db.mark_prompt_feedback_retry_invited") as mock_mark,
        patch(
            "runtime.app.bot",
            new=SimpleNamespace(
                user=SimpleNamespace(id=999), get_channel=lambda _channel_id: channel
            ),
        ),
    ):
        asyncio.run(elixir.on_raw_reaction_add(payload))

    mock_upsert.assert_called_once()
    channel.fetch_message.assert_awaited_once_with(987)
    reacted_message.add_reaction.assert_awaited_once_with("\u2705")
    if invites_retry:
        reacted_message.reply.assert_awaited_once()
        mock_mark.assert_called_once_with(feedback_id, retry_message_id=654)
    else:
        reacted_message.reply.assert_not_awaited()
        mock_mark.assert_not_called()


def test_on_raw_reaction_add_emits_warning_on_active_thumbs_down(caplog):
    """Thumbs-down must hit elixir-v5.log at WARNING so log-triage surfaces it.
    Re-reactions (became_active_down=False) should drop to INFO so we don't
    spam triage with toggle-and-back churn."""
    payload = SimpleNamespace(
        channel_id=1482368505058955467,
        message_id=987,
        user_id=123,
        emoji="👎",
        member=SimpleNamespace(bot=False),
    )
    assistant_row = {
        "discord_message_id": "987",
        "channel_id": "1482368505058955467",
        "discord_user_id": "123",
        "author_type": "assistant",
        "workflow": "interactive",
    }
    reacted_message = SimpleNamespace(
        add_reaction=AsyncMock(), reply=AsyncMock(return_value=SimpleNamespace(id=654))
    )
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=reacted_message))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1482368505058955467,
                "name": "#ask-elixir",
                "lane": "ask-elixir",
            },
        ),
        patch(
            "runtime.prompt_feedback.db.get_message_by_discord_message_id",
            return_value=assistant_row,
        ),
        patch(
            "runtime.prompt_feedback.db.upsert_prompt_feedback",
            return_value={"prompt_feedback_id": 99, "became_active_down": True},
        ),
        patch("runtime.prompt_feedback.db.mark_prompt_feedback_retry_invited"),
        patch(
            "runtime.app.bot",
            new=SimpleNamespace(
                user=SimpleNamespace(id=999), get_channel=lambda _channel_id: channel
            ),
        ),
    ):
        with caplog.at_level("INFO", logger="elixir"):
            asyncio.run(elixir.on_raw_reaction_add(payload))

    feedback_records = [r for r in caplog.records if "prompt_feedback" in r.message]
    assert len(feedback_records) == 1
    rec = feedback_records[0]
    assert rec.levelname == "WARNING"
    assert "thumbs_down" in rec.message
    assert "channel=#ask-elixir" in rec.message
    assert "workflow=interactive" in rec.message


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
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1482368505058955467,
                "name": "#ask-elixir",
                "lane": "ask-elixir",
            },
        ),
        patch(
            "runtime.prompt_feedback.db.get_message_by_discord_message_id",
            return_value=assistant_row,
        ),
        patch("runtime.prompt_feedback.db.upsert_prompt_feedback") as mock_upsert,
        patch(
            "runtime.app.bot",
            new=SimpleNamespace(user=SimpleNamespace(id=111), get_channel=lambda _channel_id: None),
        ),
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
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1482368505058955467,
                "name": "#ask-elixir",
                "lane": "ask-elixir",
            },
        ),
        patch(
            "runtime.prompt_feedback.db.get_message_by_discord_message_id",
            return_value=assistant_row,
        ),
        patch(
            "runtime.prompt_feedback.db.upsert_prompt_feedback",
            return_value={"prompt_feedback_id": 44, "became_active_down": False},
        ) as mock_upsert,
        patch("runtime.prompt_feedback.db.mark_prompt_feedback_retry_invited") as mock_mark,
        patch(
            "runtime.app.bot",
            new=SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _channel_id: None),
        ),
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
        patch(
            "elixir._get_channel_behavior",
            return_value={
                "id": 1482368505058955467,
                "name": "#ask-elixir",
                "lane": "ask-elixir",
            },
        ),
        patch(
            "runtime.prompt_feedback.db.get_message_by_discord_message_id",
            return_value=assistant_row,
        ),
        patch("runtime.prompt_feedback.db.clear_prompt_feedback") as mock_clear,
        patch(
            "runtime.app.bot",
            new=SimpleNamespace(user=SimpleNamespace(id=999), get_channel=lambda _channel_id: None),
        ),
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

    with _on_message_env(
        ASK_ELIXIR_BEHAVIOR,
        mentioned=False,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        classify={"route": "llm_chat", "confidence": 1.0, "rationale": "test"},
        respond={
            "event_type": "channel_response",
            "content": ["Part one.", "Part two."],
            "summary": "two-part answer",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    assistant_save = [call for call in env.save.call_args_list if call.args[1] == "assistant"][0]
    assert assistant_save.kwargs["discord_message_id"] == "2001"
    assert assistant_save.args[2] == "Part one.\n\nPart two."
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_is_bot_mentioned_requires_leading_mention():
    bot_user = SimpleNamespace(id=999)
    direct_message = _make_message(100, "member-chat", "<@999> how am I doing?")
    mid_message = _make_message(100, "member-chat", "how am I doing, <@999>?")

    with patch("runtime.helpers._common.bot", new=SimpleNamespace(user=bot_user)):
        assert elixir._is_bot_mentioned(direct_message) is True
        assert elixir._is_bot_mentioned(mid_message) is False


def test_strip_bot_mentions_removes_only_leading_mention():
    with (
        patch(
            "runtime.helpers._common.bot",
            new=SimpleNamespace(user=SimpleNamespace(id=999)),
        ),
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


def test_post_startup_message_posts_build_hash_to_elixir_log_webhook():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    proactive_channels = [
        {"id": 200, "name": "#leader-lounge", "workflow": "clanops"},
        {"id": 300, "name": "#ask-elixir", "workflow": "interactive"},
    ]

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[{"id": 200, "name": "#leader-lounge"}],
        ),
        patch("runtime.app.THINKING_CHANNEL_ID", None),
        patch("elixir.prompts.discord_channel_configs", return_value=proactive_channels),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.RELEASE_LABEL", 'v3.0 "Three-Lane Elixir"'),
        patch("elixir.elixir_agent.BUILD_HASH", "abc1234"),
        patch(
            "elixir.elixir_agent.generate_message",
            return_value=":elixir_hype: I just dropped into the arena and the king tower is awake.",
        ) as mock_generate,
        patch(
            "runtime.startup.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        sent = asyncio.run(elixir._post_startup_message())

    assert sent is True
    mock_generate.assert_called_once()
    posted = mock_log.await_args.args[0]
    assert posted.startswith("**Elixir Online**")
    assert 'Release: **v3.0 "Three-Lane Elixir"**' in posted
    assert "Build: **abc1234**" in posted
    assert "Host: **" in posted
    assert "king tower is awake" in posted
    assert "Channel audit: 2/2 active channels reachable and writable." in posted
    mock_post.assert_not_awaited()
    mock_save.assert_not_called()


def test_post_startup_message_fetches_channel_when_not_cached():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[{"id": 200, "name": "#leader-lounge"}],
        ),
        patch("runtime.app.THINKING_CHANNEL_ID", None),
        patch(
            "elixir.prompts.discord_channel_configs",
            return_value=[{"id": 200, "name": "#leader-lounge", "workflow": "clanops"}],
        ),
        patch.object(elixir.bot, "get_channel", return_value=None),
        patch.object(
            elixir.bot, "fetch_channel", new=AsyncMock(return_value=channel)
        ) as mock_fetch,
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.RELEASE_LABEL", 'v3.0 "Three-Lane Elixir"'),
        patch("elixir.elixir_agent.BUILD_HASH", "abc1234"),
        patch(
            "elixir.elixir_agent.generate_message",
            return_value="Elixir has entered the arena.",
        ) as mock_generate,
        patch(
            "runtime.startup.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        sent = asyncio.run(elixir._post_startup_message())

    assert sent is True
    assert mock_fetch.await_count == 2
    mock_fetch.assert_any_await(200)
    mock_generate.assert_called_once()
    mock_post.assert_not_awaited()
    mock_save.assert_not_called()


def test_post_startup_message_falls_back_to_clanops_when_webhook_unavailable():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[{"id": 200, "name": "#leader-lounge"}],
        ),
        patch(
            "elixir.prompts.discord_channel_configs",
            return_value=[{"id": 200, "name": "#leader-lounge", "workflow": "clanops"}],
        ),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir.db.list_channel_messages", return_value=[]),
        patch("elixir.elixir_agent.RELEASE_LABEL", 'v3.0 "Three-Lane Elixir"'),
        patch("elixir.elixir_agent.BUILD_HASH", "abc1234"),
        patch(
            "elixir.elixir_agent.generate_message",
            return_value="Elixir has entered the arena.",
        ),
        patch(
            "runtime.startup.elixir_log.post_event_async",
            new=AsyncMock(return_value=False),
        ),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch("elixir.db.save_message") as mock_save,
    ):
        sent = asyncio.run(elixir._post_startup_message())

    assert sent is True
    mock_post.assert_awaited_once()
    assert mock_save.call_args.kwargs["event_type"] == "startup_announcement"


def test_acquire_pid_file_overwrites_non_elixir_reused_pid(tmp_path):
    from runtime import process as runtime_process

    pid_file = tmp_path / "elixir.pid"
    pid_file.write_text("999")

    with (
        patch("runtime.process.os.getpid", return_value=1234),
        patch("runtime.process._process_exists", return_value=True),
        patch("runtime.process._pid_looks_like_elixir", return_value=False),
        patch("runtime.process.log.warning") as mock_warning,
    ):
        runtime_process._acquire_pid_file(str(pid_file))

    payload = json.loads(pid_file.read_text())
    assert payload["pid"] == 1234
    mock_warning.assert_called_once()


def test_acquire_pid_file_refuses_when_live_elixir_holds_it(tmp_path):
    """A live Elixir pid means refuse — never SIGTERM the launchd instance."""
    from runtime import process as runtime_process

    pid_file = tmp_path / "elixir.pid"
    pid_file.write_text(json.dumps({"pid": 999}))

    with (
        patch("runtime.process.os.getpid", return_value=1234),
        patch("runtime.process._process_exists", return_value=True),
        patch("runtime.process._pid_looks_like_elixir", return_value=True),
        pytest.raises(RuntimeError, match="already running"),
    ):
        runtime_process._acquire_pid_file(str(pid_file))

    # The existing owner's pid file must be left untouched.
    assert json.loads(pid_file.read_text())["pid"] == 999


def test_sigterm_handler_logs_termination_and_exits():
    from runtime import process as runtime_process

    with (
        patch("runtime.process.os.getpid", return_value=1234),
        patch("runtime.process.os.getppid", return_value=1),
        patch("runtime.process.os.getcwd", return_value="/repo"),
        patch("runtime.process.log.warning") as mock_warning,
        pytest.raises(SystemExit, match="143"),
    ):
        runtime_process._handle_termination(15, None)

    mock_warning.assert_called_once_with(
        "termination signal received signal=%s pid=%s ppid=%s cwd=%s",
        "SIGTERM",
        1234,
        1,
        "/repo",
    )


def test_startup_channel_audit_reports_missing_or_unwritable_channels():
    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")
    writable = SimpleNamespace(id=300, name="ask-elixir", type="text")
    blocked_perms = SimpleNamespace(view_channel=True, send_messages=False)
    blocked_channel = SimpleNamespace(
        id=400,
        name="actions",
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
        patch.object(
            type(elixir.bot),
            "user",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id=999),
        ),
        patch("runtime.app.THINKING_CHANNEL_ID", None),
        patch(
            "elixir.prompts.discord_channel_configs",
            return_value=[
                {"id": 200, "name": "#leader-lounge", "workflow": "clanops"},
                {"id": 300, "name": "#ask-elixir", "workflow": "interactive"},
                {"id": 400, "name": "#actions", "workflow": "channel_update"},
                {"id": 500, "name": "#missing", "workflow": "channel_update"},
            ],
        ),
    ):
        summary = asyncio.run(elixir._startup_channel_audit_summary())

    assert "Channel audit: 2/4 active channels reachable and writable." in summary
    assert "#actions not writable" in summary
    assert "#missing missing or unreachable" in summary


def test_startup_channel_audit_flags_missing_soft_perms():
    # The 2026-04-25 #welcome incident: bot could view + send but lacked
    # read_message_history, so message.reply() 403'd. Audit must catch this.
    perms_missing_history = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        read_message_history=False,
        add_reactions=True,
        use_external_emojis=True,
    )
    perms_missing_reactions = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        add_reactions=False,
        use_external_emojis=False,
    )
    perms_ok = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        add_reactions=True,
        use_external_emojis=True,
    )
    guild = SimpleNamespace(id=1, me=object())
    reception_channel = SimpleNamespace(
        id=200,
        name="reception",
        type="text",
        guild=guild,
        permissions_for=lambda m: perms_missing_history,
    )
    ask_channel = SimpleNamespace(
        id=300,
        name="ask-elixir",
        type="text",
        guild=guild,
        permissions_for=lambda m: perms_missing_reactions,
    )
    leader_channel = SimpleNamespace(
        id=400,
        name="leader-lounge",
        type="text",
        guild=guild,
        permissions_for=lambda m: perms_ok,
    )

    def fake_get_channel(channel_id):
        return {200: reception_channel, 300: ask_channel, 400: leader_channel}.get(channel_id)

    with (
        patch.object(elixir.bot, "get_channel", side_effect=fake_get_channel),
        patch.object(
            elixir.bot,
            "fetch_channel",
            new=AsyncMock(side_effect=RuntimeError("unused")),
        ),
        patch.object(
            type(elixir.bot),
            "user",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id=999),
        ),
        patch("runtime.app.THINKING_CHANNEL_ID", None),
        patch(
            "elixir.prompts.discord_channel_configs",
            return_value=[
                {"id": 200, "name": "#welcome", "workflow": "reception"},
                {"id": 300, "name": "#ask-elixir", "workflow": "interactive"},
                {"id": 400, "name": "#leader-lounge", "workflow": "clanops"},
            ],
        ),
    ):
        summary = asyncio.run(elixir._startup_channel_audit_summary())

    assert "Channel audit: 1/3 active channels reachable and writable." in summary
    assert "#welcome missing perms: read_message_history" in summary
    assert "#ask-elixir missing perms: add_reactions, use_external_emojis" in summary


def test_startup_channel_audit_checks_thinking_embed_and_thread_perms():
    # #thinking is env-configured (not in discord_channel_configs) and posts an
    # embed then opens a thread — the audit must include it and flag embed_links /
    # thread perms, and flag embed_links on the actions (#actions) lane.
    guild = SimpleNamespace(id=1, me=object())
    actions_perms = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        embed_links=False,
        read_message_history=True,
        add_reactions=True,
        use_external_emojis=True,
    )
    thinking_perms = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        create_public_threads=True,
        send_messages_in_threads=False,
        read_message_history=True,
        add_reactions=True,
        use_external_emojis=True,
    )
    actions_channel = SimpleNamespace(
        id=400,
        name="actions",
        type="text",
        guild=guild,
        permissions_for=lambda m: actions_perms,
    )
    thinking_channel = SimpleNamespace(
        id=777,
        name="thinking",
        type="text",
        guild=guild,
        permissions_for=lambda m: thinking_perms,
    )

    def fake_get_channel(channel_id):
        return {400: actions_channel, 777: thinking_channel}.get(channel_id)

    with (
        patch.object(elixir.bot, "get_channel", side_effect=fake_get_channel),
        patch.object(
            elixir.bot, "fetch_channel", new=AsyncMock(side_effect=RuntimeError("unused"))
        ),
        patch.object(
            type(elixir.bot),
            "user",
            new_callable=PropertyMock,
            return_value=SimpleNamespace(id=999),
        ),
        patch("runtime.app.THINKING_CHANNEL_ID", 777),
        patch(
            "elixir.prompts.discord_channel_configs",
            return_value=[
                {"id": 400, "name": "#actions", "workflow": "channel_update", "lane": "actions"}
            ],
        ),
    ):
        summary = asyncio.run(elixir._startup_channel_audit_summary())

    assert "Channel audit: 0/2 active channels reachable and writable." in summary
    assert "#actions missing perms: embed_links" in summary
    assert "#thinking missing perms: send_messages_in_threads" in summary


def test_on_message_replies_with_fallback_when_channel_agent_returns_none():
    message = _make_message(
        200,
        "clan-ops",
        "<@999> What is my current war participation rate over the last 4 weeks?",
    )

    with (
        _on_message_env(
            CLANOPS_BEHAVIOR,
            bot_user_id=999,
            history=[],
            memory_context={},
            clan_context=({"memberList": []}, {}),
            respond=None,
            share=True,
        ) as env,
        patch("elixir.db.record_prompt_failure", return_value=17) as mock_failure,
        patch(
            "elixir.runtime_status.snapshot",
            return_value={
                "llm": {
                    "last_error": "Error code: 429 rate_limit_exceeded",
                    "last_model": "claude-sonnet-4-6",
                    "last_call_at": "2026-03-07T19:12:00",
                }
            },
        ),
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
    env.share.assert_not_awaited()
    env.process.assert_not_awaited()


def test_on_message_logs_agent_failure_payload_details():
    message = _make_message(200, "clan-ops", "<@999> Who is on the hottest streak right now?")

    with (
        _on_message_env(
            CLANOPS_BEHAVIOR,
            bot_user_id=999,
            history=[],
            memory_context={},
            clan_context=({"memberList": []}, {}),
            respond={
                "_error": {
                    "kind": "schema_error",
                    "detail": "missing required field: content",
                    "phase": "repair_response",
                    "result_preview": '{"event_type":"channel_response"}',
                    "raw_json": {"event_type": "channel_response"},
                }
            },
            share=True,
        ) as env,
        patch("elixir.db.record_prompt_failure", return_value=18) as mock_failure,
        patch(
            "elixir.runtime_status.snapshot",
            return_value={
                "llm": {
                    "last_error": None,
                    "last_model": "claude-sonnet-4-6",
                    "last_call_at": "2026-03-11T07:00:00",
                }
            },
        ),
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
    env.share.assert_not_awaited()
    env.process.assert_not_awaited()


def test_on_message_ignores_unmentioned_clanops_chat():
    message = _make_message(200, "clan-ops", "I think we need to review promotions this week.")

    with _on_message_env(
        CLANOPS_BEHAVIOR,
        mentioned=False,
        history=[],
        memory_context={},
        clan_context=({"memberList": []}, {}),
        respond={
            "event_type": "channel_response",
            "content": "I can pull the current promotion candidates if you want.",
            "summary": "ops",
        },
        share=True,
    ) as env:
        asyncio.run(elixir.on_message(message))

    env.history.assert_not_called()
    env.save.assert_not_called()
    env.respond.assert_not_called()
    env.share.assert_not_awaited()
    message.reply.assert_not_awaited()
    env.process.assert_awaited_once_with(message)


def test_on_message_handles_explicit_member_deck_request_without_llm():
    message = _make_message(200, "clan-ops", "<@999> what cards are in @Vijay deck?")

    with (
        _on_message_env(
            CLANOPS_BEHAVIOR,
            mentioned=True,
            classify={
                "route": "deck_display",
                "confidence": 1.0,
                "rationale": "test",
            },
            respond=NEVER_CALLED,
        ) as env,
        patch(
            "elixir.db.resolve_member",
            return_value=[
                {
                    "player_tag": "#DEF456",
                    "current_name": "Vijay",
                    "member_ref": "Vijay",
                    "member_ref_with_handle": "Vijay (<@456>)",
                    "match_score": 850,
                    "match_source": "discord_display_exact",
                }
            ],
        ) as mock_resolve,
        patch(
            "elixir.db.get_member_current_deck",
            return_value={
                "fetched_at": "2026-03-07T12:00:00",
                "cards": [
                    {
                        "name": "Knight",
                        "level": 16,
                        "supports_evo": True,
                        "supports_hero": True,
                        "mode_status_label": "Evo + Hero unlocked",
                    },
                    {
                        "name": "Fireball",
                        "level": 16,
                        "supports_evo": False,
                        "supports_hero": False,
                        "mode_status_label": None,
                    },
                ],
            },
        ),
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
    assert env.save.call_count == 2
    assert env.save.call_args_list[1].kwargs["event_type"] == "member_deck_report"
    env.respond.assert_not_called()
    env.process.assert_not_awaited()


def test_on_message_keeps_interpretive_main_deck_questions_in_llm_path():
    message = _make_message(
        1482368505058955467,
        "ask-elixir",
        "What is the average level of the cards I use in my current main deck?",
    )

    with (
        _on_message_env(
            _behavior(ASK_ELIXIR_BEHAVIOR, memory_scope="public"),
            mentioned=False,
            history=[],
            memory_context={"channel": {"state": None, "episodes": []}},
            clan_context=({"memberList": []}, {}),
            respond={
                "event_type": "channel_response",
                "content": "LLM answer",
                "summary": "llm",
            },
            share=True,
        ) as env,
        patch("elixir.db.resolve_member") as mock_resolve,
        patch("elixir.db.get_member_current_deck") as mock_current_deck,
    ):
        asyncio.run(elixir.on_message(message))

    mock_current_deck.assert_not_called()
    mock_resolve.assert_not_called()
    env.respond.assert_called_once()
    message.reply.assert_awaited_once_with("LLM answer")
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_on_message_rewrites_member_refs_before_reply_and_save():
    message = _make_message(100, "member-chat", "<@999> how is King Levy doing?")

    def fake_format_member_reference(tag, conn=None, **_kwargs):
        return "King Levy" if tag == "#ABC123" else tag

    with (
        _on_message_env(
            MEMBER_CHAT_BEHAVIOR,
            mentioned=True,
            history=[],
            memory_context={},
            clan_context=({"memberList": []}, {}),
            respond={
                "event_type": "channel_response",
                "content": "King Levy is trending up.",
                "summary": "up",
                "member_tags": ["#ABC123"],
            },
            share=True,
        ) as env,
        patch(
            "elixir.db.format_member_reference",
            side_effect=fake_format_member_reference,
        ),
    ):
        asyncio.run(elixir.on_message(message))

    message.reply.assert_awaited_once_with("King Levy is trending up.")
    assert env.save.call_args_list[1].args[2] == "King Levy is trending up."
    env.respond.assert_called_once()
    env.share.assert_awaited_once()
    env.process.assert_not_awaited()


def test_slash_help_does_not_save_conversation_history():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = _root(bot, "clanops")
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


def test_command_surface_is_split_member_vs_leader():
    """/elixir is the member surface (email + help); /clanops holds the leader
    groups; memory/system/signal were dropped from Discord entirely."""
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    elixir = _root(bot, "elixir")
    clanops = _root(bot, "clanops")
    assert sorted(c.name for c in elixir.commands) == ["email", "help"]
    assert set(c.name for c in clanops.commands) == {
        "clan",
        "member",
        "relay",
        "activity",
        "tournament",
        "release",
        "help",
    }
    # dropped groups exist under neither root
    for root in (elixir, clanops):
        for gone in ("system", "memory", "signal"):
            assert root.get_command(gone) is None


def test_command_specs_cover_every_registered_leaf_command():
    """The registered leaf set must EQUAL COMMAND_SPECS — neither side may carry
    a command the other doesn't. Equality is what makes per-command "is
    /clanops relay status registered?" tests redundant: a leaf that stopped
    being registered breaks this, and so does one registered without a spec."""
    bot = _FakeBot()
    register_elixir_app_commands(bot)

    def leaf_paths(command, prefix=()):
        path = (*prefix, command.name)
        children = getattr(command, "commands", None)
        if children is None:
            return {path}
        return {leaf for child in children for leaf in leaf_paths(child, path)}

    registered = {path for root in bot.tree.commands for path in leaf_paths(root)}
    specified = {spec.discord_path for spec in COMMAND_SPECS.values()}

    assert len(registered) == 25
    assert registered == specified
    assert all(spec.event_type for spec in COMMAND_SPECS.values())


def test_member_help_is_ungated():
    """/elixir help is member-facing: no leader role, no #clanops channel needed,
    and it never emits the leader/channel gate messages."""
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    help_cmd = _root(bot, "elixir").get_command("help")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=999, name="general", type="text"),
        user=SimpleNamespace(id=42, name="member", display_name="Member", roles=[]),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    with patch("runtime.discord_commands.db.record_admin_command_invocation") as mock_record:
        asyncio.run(help_cmd.callback(interaction))

    response.defer.assert_awaited_once_with(ephemeral=True)
    sent = response.send_message.await_args.args[0]
    assert "/elixir email set" in sent
    assert "Leader role required" not in sent and "#clanops" not in sent
    mock_record.assert_called_once_with(
        "elixir.help",
        "elixir_help",
        discord_user_id=42,
        channel_id=999,
        write_requested=False,
        accepted=True,
    )


@pytest.mark.parametrize(
    ("name", "kwargs", "command_key", "event_type", "write_requested"),
    [
        ("set", {"address": "member@example.com"}, "email.set", "email_set", True),
        ("verify", {"code": "123456"}, "email.verify", "email_verify", True),
        ("show", {}, "email.show", "email_show", False),
    ],
)
def test_member_email_commands_record_telemetry_before_identity_resolution(
    name, kwargs, command_key, event_type, write_requested
):
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    email_group = _root(bot, "elixir").get_command("email")
    command = email_group.get_command(name)
    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=999, name="general", type="text"),
        user=SimpleNamespace(id=42, name="member", display_name="Member", roles=[]),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.discord_commands.db.record_admin_command_invocation") as mock_record,
        patch(
            "runtime.discord_commands.db.get_linked_member_for_discord_user",
            return_value=None,
        ),
    ):
        asyncio.run(command.callback(interaction, **kwargs))

    mock_record.assert_called_once_with(
        command_key,
        event_type,
        discord_user_id=42,
        channel_id=999,
        write_requested=write_requested,
        accepted=True,
    )


def test_slash_relay_status_allowed_in_actions():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = _root(bot, "clanops")
    relay_group = root.get_command("relay")
    status_command = relay_group.get_command("status")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=300, name="leader-actions", type="text"),
        user=SimpleNamespace(
            id=123,
            name="jamie",
            display_name="Jamie",
            roles=[SimpleNamespace(name="Leader")],
        ),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=False),
        patch(
            "runtime.app._get_channel_behavior",
            return_value={"name": "#leader-actions", "lane": "actions"},
        ),
        patch("runtime.app._has_leader_role", return_value=True),
        patch(
            "runtime.discord_commands.dispatch_admin_command",
            new=AsyncMock(return_value="relay report"),
        ) as mock_dispatch,
    ):
        asyncio.run(status_command.callback(interaction, view="pending", limit=5))

    mock_dispatch.assert_awaited_once_with(
        "relay.status",
        preview=False,
        short=False,
        args={"view": "pending", "limit": "5"},
    )
    response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content="relay report")
    followup.send.assert_not_awaited()


def test_slash_non_relay_command_still_rejected_in_actions():
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    root = _root(bot, "clanops")
    clan_group = root.get_command("clan")
    war_status_command = clan_group.get_command("war")

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=300, name="leader-actions", type="text"),
        user=SimpleNamespace(
            id=123,
            name="jamie",
            display_name="Jamie",
            roles=[SimpleNamespace(name="Leader")],
        ),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=False),
        patch(
            "runtime.app._get_channel_behavior",
            return_value={"name": "#leader-actions", "lane": "actions"},
        ),
        patch(
            "runtime.discord_commands.dispatch_admin_command",
            new=AsyncMock(return_value="war report"),
        ) as mock_dispatch,
        patch("runtime.discord_commands.db.record_admin_command_invocation") as mock_record,
    ):
        asyncio.run(war_status_command.callback(interaction))

    response.send_message.assert_awaited_once_with(
        "Use `/clanops ...` in `#clanops`.", ephemeral=True
    )
    response.defer.assert_not_awaited()
    mock_dispatch.assert_not_awaited()
    interaction.edit_original_response.assert_not_awaited()
    followup.send.assert_not_awaited()
    mock_record.assert_called_once_with(
        "clan.war",
        "war_status_report",
        discord_user_id=123,
        channel_id=300,
        write_requested=False,
        accepted=False,
    )


def test_dispatch_admin_command_handles_member_audit_discord():
    human = SimpleNamespace(
        id=555,
        bot=False,
        display_name="UnlinkedUser",
        nick=None,
        name="UnlinkedUser",
        global_name="UnlinkedUser",
        roles=[],
    )
    linked = SimpleNamespace(
        id=777,
        bot=False,
        display_name="King Levy",
        nick="King Levy",
        name="kinglevy",
        global_name="King Levy",
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
        patch(
            "db.list_members",
            return_value=[
                {
                    "player_tag": "#ABC",
                    "current_name": "King Levy",
                    "discord_user_id": "777",
                },
                {
                    "player_tag": "#DEF",
                    "current_name": "Lonely",
                    "discord_user_id": None,
                },
            ],
        ),
        patch("db.get_linked_member_for_discord_user", side_effect=fake_linked_lookup),
        patch("db.resolve_member", return_value=[]),
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "member.audit-discord", preview=False, short=False, args={}
            ),
        )

    assert "Active clan members: 2 (1 without a Discord link)" in result
    assert "Unlinked Discord users: 1" in result
    assert "Lonely" in result
    assert "UnlinkedUser" in result


def test_dispatch_admin_command_handles_verify_discord():
    with (
        patch("runtime.admin._resolve_member_tag", return_value=("#ABC123", "King Levy")),
        patch(
            "runtime.onboarding.verify_discord_membership",
            new=AsyncMock(return_value="Verified Discord identity for King Levy."),
        ) as mock_verify,
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
    with patch(
        "runtime.admin._build_clan_list_report",
        return_value="**Clan List Full (2 active)**",
    ) as mock_report:
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
    with patch(
        "elixir._weekly_clan_recap",
        new=AsyncMock(
            side_effect=RuntimeError(
                "weekly recap post failed: missing Discord permissions in #weekly-digest"
            )
        ),
    ):
        result = asyncio.run(
            elixir.dispatch_admin_command(
                "activity.run",
                preview=False,
                short=False,
                args={"activity": "weekly-recap"},
            )
        )

    assert (
        result
        == "`weekly-recap` failed: weekly recap post failed: missing Discord permissions in #weekly-digest"
    )


def test_dispatch_admin_command_rejects_non_manual_activity():
    result = asyncio.run(
        elixir.dispatch_admin_command(
            "activity.run",
            preview=False,
            short=False,
            args={"activity": "war-attendance-snapshot"},
        )
    )

    assert result == "`war-attendance-snapshot` cannot be run manually."


def test_dispatch_admin_command_handles_activity_run():
    with patch(
        "runtime.admin._run_runtime_job",
        new=AsyncMock(return_value="Ran `site-content`."),
    ) as mock_job:
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
        patch(
            "runtime.onboarding.resolve_discord_member_input",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "runtime.admin.asyncio.to_thread",
            new=AsyncMock(side_effect=[("#ABC123", "King Levy")]),
        ) as mock_to_thread,
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
        patch(
            "runtime.onboarding.resolve_discord_member_input",
            new=AsyncMock(return_value=guild_member),
        ),
        patch(
            "runtime.admin.asyncio.to_thread",
            new=AsyncMock(side_effect=[("#VGJJLC9PR", "Ditaka"), None]),
        ) as mock_to_thread,
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


@pytest.mark.parametrize(
    "admin_key,args,expected_reply,expected_db_call,expected_memory_fn,expected_memory_kwargs",
    [
        pytest.param(
            "member.set",
            {"member": "King Levy", "field": "note", "value": "Reliable war leader."},
            "Set note for King Levy.",
            ("set_member_note", "#ABC123", None, "Reliable war leader."),
            "upsert_member_note_memory",
            {
                "member_tag": "#ABC123",
                "member_label": "King Levy",
                "note": "Reliable war leader.",
                "created_by": "leader:admin-command",
                "metadata": {"command": "set-note"},
            },
            id="set_note_writes_contextual_memory",
        ),
        pytest.param(
            "member.clear",
            {"member": "King Levy", "field": "note"},
            "Cleared note for King Levy.",
            ("clear_member_note", "#ABC123", None),
            "archive_member_note_memory",
            {"member_tag": "#ABC123", "actor": "leader:admin-command"},
            id="clear_note_archives_contextual_memory",
        ),
    ],
)
def test_dispatch_admin_command_note_writes_and_mirrors_to_memory(
    admin_key,
    args,
    expected_reply,
    expected_db_call,
    expected_memory_fn,
    expected_memory_kwargs,
):
    """A leader note is written twice on purpose: to the member row AND to
    durable contextual memory, in that order, off one command. The memory
    mirror is what makes the note reachable by the brain later, so the second
    to_thread call is as load-bearing as the first."""
    from runtime import admin as runtime_admin

    with patch(
        "runtime.admin.asyncio.to_thread",
        new=AsyncMock(side_effect=[("#ABC123", "King Levy"), None, None]),
    ) as mock_to_thread:
        result = asyncio.run(
            elixir.dispatch_admin_command(admin_key, preview=False, short=False, args=args)
        )

    assert result == expected_reply
    db_fn, *db_args = expected_db_call
    assert mock_to_thread.await_args_list[1].args == (getattr(elixir.db, db_fn), *db_args)
    assert mock_to_thread.await_args_list[2].args == (getattr(runtime_admin, expected_memory_fn),)
    assert mock_to_thread.await_args_list[2].kwargs == expected_memory_kwargs


def test_resolve_member_tag_accepts_name_with_tag_label():
    from runtime import admin as runtime_admin

    with patch(
        "db.resolve_member",
        return_value=[
            {
                "player_tag": "#VGJJLC9PR",
                "match_score": 1000,
                "member_ref_with_handle": "Ditaka",
            }
        ],
    ) as mock_resolve:
        tag, label = runtime_admin._resolve_member_tag("Ditaka (#VGJJLC9PR)")

    assert tag == "#VGJJLC9PR"
    assert label == "Ditaka"
    mock_resolve.assert_called_once_with("#VGJJLC9PR", limit=3, conn=None)


def test_resolve_member_tag_rejects_empty_and_overlong_inputs():
    import pytest

    from runtime import admin as runtime_admin

    with pytest.raises(ValueError, match="required"):
        runtime_admin._resolve_member_tag("")
    with pytest.raises(ValueError, match="required"):
        runtime_admin._resolve_member_tag("   ")
    with pytest.raises(ValueError, match="64 characters"):
        runtime_admin._resolve_member_tag("x" * 100)


def test_admin_command_requires_leader_classification():
    assert admin_command_requires_leader("member.set") is True
    assert admin_command_requires_leader("clan.status") is False


def test_dispatch_admin_command_handles_war_status():
    with (
        patch(
            "elixir._load_live_clan_context",
            new=AsyncMock(return_value=({"name": "POAP KINGS"}, {"clans": [{}, {}]})),
        ) as mock_load,
        patch(
            "elixir._build_war_status_report",
            return_value="**POAP KINGS War Status**\n- Live: Battle Day 2",
        ) as mock_report,
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


# Every leader slash command that is a thin shell over dispatch_admin_command:
# find the leaf, defer ephemerally, dispatch, edit the deferred response with
# whatever came back. Four tests asserted that same shape with different nouns.
#
# (group, leaf, callback kwargs, leader role required, admin key, expected args)
SLASH_DISPATCH_CASES = [
    pytest.param(
        "clan",
        "members",
        {"detail": "full"},
        False,
        "clan.members",
        {"detail": "full"},
        "full list",
        id="clan_members_passes_detail_flag",
    ),
    pytest.param(
        "clan",
        "war",
        {},
        False,
        "clan.war",
        {},
        "war report",
        id="clan_war_takes_no_options",
    ),
    pytest.param(
        "member",
        "set",
        {"member": "King Levy", "field": "discord", "value": "@kinglevy"},
        True,
        "member.set",
        {"member": "King Levy", "field": "discord", "value": "@kinglevy"},
        "linked",
        id="member_set_passes_identity_through",
    ),
    pytest.param(
        "activity",
        "run",
        {"activity": "weekly-recap", "preview": False},
        True,
        "activity.run",
        {"activity": "weekly-recap"},
        "job failed",
        id="activity_run_defers_before_dispatching",
    ),
]


@pytest.mark.parametrize(
    "group_name,leaf_name,callback_kwargs,needs_leader,admin_key,expected_args,reply_text",
    SLASH_DISPATCH_CASES,
)
def test_slash_command_dispatches_to_admin(
    group_name,
    leaf_name,
    callback_kwargs,
    needs_leader,
    admin_key,
    expected_args,
    reply_text,
):
    """A /clanops leaf defers ephemerally FIRST, then dispatches, then edits the
    deferred response. The defer-before-dispatch order is the point: admin work
    can outrun Discord's 3s interaction window, and a late first response is an
    'application did not respond' error the leader sees instead of the answer."""
    bot = _FakeBot()
    register_elixir_app_commands(bot)
    group = _root(bot, "clanops").get_command(group_name)
    command = group.get_command(leaf_name)

    response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        channel=SimpleNamespace(id=200, name="clan-ops", type="text"),
        user=SimpleNamespace(
            id=123,
            name="jamie",
            display_name="Jamie",
            roles=[SimpleNamespace(name="Leader")] if needs_leader else [],
        ),
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )

    with (
        patch("runtime.app._is_clanops_channel", return_value=True),
        patch("runtime.app._has_leader_role", return_value=needs_leader),
        patch(
            "runtime.discord_commands.dispatch_admin_command",
            new=AsyncMock(return_value=reply_text),
        ) as mock_dispatch,
    ):
        asyncio.run(command.callback(interaction, **callback_kwargs))

    mock_dispatch.assert_awaited_once_with(
        admin_key,
        preview=False,
        short=False,
        args=expected_args,
    )
    response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.edit_original_response.assert_awaited_once_with(content=reply_text)
    followup.send.assert_not_awaited()


def test_cr_api_auth_failure_alert_posts_once_per_signature():
    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[
                {
                    "id": 200,
                    "name": "#leader-lounge",
                    "lane": "leader-lounge",
                    "workflow": "clanops",
                }
            ],
        ),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch(
            "elixir.db.format_member_reference",
            return_value="King Thing (<@704062105258557511>)",
        ),
        patch("elixir.db.save_message") as mock_save,
        patch(
            "runtime.alerts.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
        patch(
            "elixir.runtime_status.snapshot",
            return_value={
                "api": {
                    "last_ok": False,
                    "last_status_code": 403,
                    "last_error": "403 Client Error: Forbidden",
                    "last_endpoint": "clan",
                    "last_entity_key": "J2RGCRVG",
                }
            },
        ),
    ):
        elixir._CR_API_ALERT_SIGNATURE = None
        first = asyncio.run(elixir._maybe_alert_cr_api_failure("live clan refresh"))
        second = asyncio.run(elixir._maybe_alert_cr_api_failure("live clan refresh"))

    try:
        assert first is True
        assert second is False
        mock_log.assert_awaited_once()
        posted = mock_log.await_args.args[0]
        assert "King Thing" in posted
        assert "<@" not in posted
        assert "live clan refresh" in posted
        assert "IP allowlist" in posted or "key or its IP allowlist" in posted
        mock_post.assert_not_awaited()
        mock_save.assert_not_called()
    finally:
        elixir._CR_API_ALERT_SIGNATURE = None


def test_cr_api_outage_alert_posts_after_consecutive_failures():
    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[
                {
                    "id": 200,
                    "name": "#leader-lounge",
                    "lane": "leader-lounge",
                    "workflow": "clanops",
                }
            ],
        ),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch(
            "elixir.db.format_member_reference",
            return_value="King Thing (<@704062105258557511>)",
        ),
        patch("elixir.db.save_message") as mock_save,
        patch(
            "runtime.alerts.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
        patch(
            "elixir.runtime_status.snapshot",
            return_value={
                "api": {
                    "last_ok": False,
                    "last_status_code": 500,
                    "last_error": "500 Server Error: Internal Server Error",
                    "last_endpoint": "clan",
                    "last_entity_key": "J2RGCRVG",
                    "consecutive_error_count": 3,
                }
            },
        ),
    ):
        elixir._CR_API_OUTAGE_ALERT_SIGNATURE = None
        sent = asyncio.run(elixir._maybe_alert_cr_api_failure("player intel refresh"))

    try:
        assert sent is True
        mock_log.assert_awaited_once()
        posted = mock_log.await_args.args[0]
        assert "failed 3 times in a row" in posted
        assert "<@" not in posted
        assert "player intel refresh" in posted
        mock_post.assert_not_awaited()
        mock_save.assert_not_called()
    finally:
        elixir._CR_API_OUTAGE_ALERT_SIGNATURE = None


def test_llm_outage_alert_fires_on_first_hard_fail_error():
    """Hard-fail LLM errors (quota, auth, billing) don't clear on retry, so
    the alert must fire on the first occurrence rather than waiting for three
    consecutive failures — that's what made the 2026-04-19 Anthropic monthly-
    budget trip slip past the admin."""
    from runtime import app as runtime_app

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[
                {
                    "id": 200,
                    "name": "#leader-lounge",
                    "lane": "leader-lounge",
                    "workflow": "clanops",
                }
            ],
        ),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch(
            "elixir.db.format_member_reference",
            return_value="King Thing (<@704062105258557511>)",
        ),
        patch("elixir.db.save_message") as mock_save,
        patch(
            "runtime.alerts.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
        patch(
            "runtime.app.runtime_status.snapshot",
            return_value={
                "llm": {
                    "last_ok": False,
                    "last_error": "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'You have reached your specified API usage limits. You will regain access on 2026-05-01 at 00:00 UTC.'}}",
                    "last_workflow": "channel_update",
                    "last_model": "claude-haiku-4-5-20251001",
                    "consecutive_error_count": 1,
                }
            },
        ),
    ):
        runtime_app._ALERT_SIGNATURES.pop("llm_outage", None)
        sent = asyncio.run(runtime_app._maybe_alert_llm_failure("channel update"))

    try:
        assert sent is True, "hard-fail error should alert on first occurrence"
        mock_log.assert_awaited_once()
        posted = mock_log.await_args.args[0]
        assert "King Thing" in posted
        assert "<@" not in posted
        assert "channel update" in posted
        assert "usage limits" in posted.lower()
        mock_post.assert_not_awaited()
        mock_save.assert_not_called()
    finally:
        runtime_app._ALERT_SIGNATURES.pop("llm_outage", None)


def test_llm_outage_alert_waits_for_three_consecutive_soft_errors():
    """Transient errors (timeouts, 5xx) still use the 3-consecutive threshold
    so a flaky connection doesn't spam the admin channel."""
    from runtime import app as runtime_app

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    def snapshot_with_count(count):
        return {
            "llm": {
                "last_ok": False,
                "last_error": "Connection timeout after 60s",
                "last_workflow": "channel_update",
                "last_model": "claude-haiku-4-5-20251001",
                "consecutive_error_count": count,
            }
        }

    with (
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[
                {
                    "id": 200,
                    "name": "#leader-lounge",
                    "lane": "leader-lounge",
                    "workflow": "clanops",
                }
            ],
        ),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch(
            "elixir.db.format_member_reference",
            return_value="King Thing (<@704062105258557511>)",
        ),
        patch("elixir.db.save_message"),
        patch(
            "runtime.alerts.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
    ):
        runtime_app._ALERT_SIGNATURES.pop("llm_outage", None)
        try:
            with patch(
                "runtime.app.runtime_status.snapshot",
                return_value=snapshot_with_count(2),
            ):
                early = asyncio.run(runtime_app._maybe_alert_llm_failure("channel update"))
            with patch(
                "runtime.app.runtime_status.snapshot",
                return_value=snapshot_with_count(3),
            ):
                third = asyncio.run(runtime_app._maybe_alert_llm_failure("channel update"))
        finally:
            runtime_app._ALERT_SIGNATURES.pop("llm_outage", None)

    assert early is False, "soft errors must not alert on 2nd failure"
    assert third is True, "soft errors alert on 3rd consecutive failure"
    mock_log.assert_awaited_once()
    assert "<@" not in mock_log.await_args.args[0]
    mock_post.assert_not_awaited()


def test_schedule_llm_failure_alert_is_noop_when_loop_unavailable():
    """schedule_llm_failure_alert is called from sync code (agent/core.py).
    When the bot's loop is not running (tests, scripts, shutdown), it must
    silently no-op instead of raising."""
    from runtime import app as runtime_app

    fake_bot = SimpleNamespace(loop=None)
    with patch.object(runtime_app, "bot", fake_bot):
        runtime_app.schedule_llm_failure_alert("channel update")

    stopped_loop = SimpleNamespace(is_closed=lambda: True, is_running=lambda: False)
    fake_bot = SimpleNamespace(loop=stopped_loop)
    with patch.object(runtime_app, "bot", fake_bot):
        runtime_app.schedule_llm_failure_alert("channel update")


def test_llm_outage_alert_dedupes_on_signature():
    """Same error signature on repeat calls must not re-post."""
    from runtime import app as runtime_app

    channel = SimpleNamespace(id=200, name="leader-lounge", type="text")

    with (
        patch(
            "elixir.prompts.discord_channels_by_workflow",
            return_value=[
                {
                    "id": 200,
                    "name": "#leader-lounge",
                    "lane": "leader-lounge",
                    "workflow": "clanops",
                }
            ],
        ),
        patch.object(elixir.bot, "get_channel", return_value=channel),
        patch("elixir._post_to_elixir", new=AsyncMock()) as mock_post,
        patch(
            "elixir.db.format_member_reference",
            return_value="King Thing (<@704062105258557511>)",
        ),
        patch("elixir.db.save_message"),
        patch(
            "runtime.alerts.elixir_log.post_event_async",
            new=AsyncMock(return_value=True),
        ) as mock_log,
        patch(
            "runtime.app.runtime_status.snapshot",
            return_value={
                "llm": {
                    "last_ok": False,
                    "last_error": "invalid_request_error: usage limits reached",
                    "last_workflow": "weekly_recap",
                    "last_model": "claude-haiku-4-5-20251001",
                    "consecutive_error_count": 1,
                }
            },
        ),
    ):
        runtime_app._ALERT_SIGNATURES.pop("llm_outage", None)
        try:
            first = asyncio.run(runtime_app._maybe_alert_llm_failure("weekly recap"))
            second = asyncio.run(runtime_app._maybe_alert_llm_failure("weekly recap"))
        finally:
            runtime_app._ALERT_SIGNATURES.pop("llm_outage", None)

    assert first is True
    assert second is False, "second call with same signature must not re-post"
    mock_log.assert_awaited_once()
    assert "<@" not in mock_log.await_args.args[0]
    mock_post.assert_not_awaited()


def test_build_schedule_report_lists_every_scheduled_activity():
    """One `_build_schedule_report()` call, every line it owes.

    This was four tests that built the identical report from the identical
    scheduler stub and each asserted a different slice of it. The report is one
    string; assert the whole contract in one place so a rendering change fails
    once with the full picture instead of four times with a quarter each.
    """
    scheduler = SimpleNamespace(running=True, get_jobs=lambda: [])

    with (
        patch("elixir.scheduler", scheduler),
        patch.object(elixir, "PROMOTION_CONTENT_DAY", "fri"),
        patch.object(elixir, "PROMOTION_CONTENT_HOUR", 9),
        patch.object(elixir, "WEEKLY_RECAP_DAY", "mon"),
        patch.object(elixir, "WEEKLY_RECAP_HOUR", 9),
        patch.object(elixir, "ENGINE_TICK_MINUTES", 10),
        patch.object(elixir, "AWARENESS_LOOP_MINUTE", 5),
    ):
        report = elixir._build_schedule_report()

    # promotion-content sync: lane, Discord destination, and cadence
    assert "recruiting" in report
    assert "promotion-content" in report
    assert "Discord: #recruiting" in report
    assert "Every Fri at 09:00 CT." in report

    # weekly clan recap
    assert "weekly-recap" in report
    assert "Every Mon at 09:00 CT." in report

    # engine tick renders its interval, not a fixed string
    assert "elixir-log" in report
    assert "engine-tick" in report
    assert "Every 10 minutes." in report

    # the awareness loop names its owner and its member-facing lane selection
    assert "awareness-loop" in report
    assert "Daily at 09:05, 21:05 CT." in report
    assert "member-facing lanes selected by the validated awareness plan" in report


def test_activity_registry_has_unique_keys_and_required_fields():
    activities = list_registered_activities()

    assert activities
    keys = [activity.activity_key for activity in activities]
    assert len(keys) == len(set(keys))
    assert all(
        activity.activity_role in {"observer", "communicator", "observer+communicator"}
        for activity in activities
    )
    assert all(activity.owner_lane for activity in activities)
    assert all(activity.job_id for activity in activities)
    assert all(activity.job_function for activity in activities)
    assert all(activity.schedule_kind in {"interval", "cron"} for activity in activities)
    assert all(activity.delivery_targets for activity in activities)


def test_activity_registry_exposes_war_and_promotion_visibility():
    specs = {spec["activity_key"]: spec for spec in schedule_specs_from_registry(elixir)}

    assert specs["engine-tick"]["owner_lane"] == "elixir-log"
    assert specs["engine-tick"]["activity_role"] == "observer"
    assert specs["engine-tick"]["schedule"] == "Every 10 minutes."
    assert "war-poll" not in specs
    assert specs["awareness-loop"]["activity_role"] == "observer+communicator"
    assert specs["awareness-loop"]["schedule"] == "Daily at 09:05, 21:05 CT."
    assert "daily-clan-insight" in specs
    assert specs["daily-clan-insight"]["owner_lane"] == "ask-elixir"
    assert specs["daily-clan-insight"]["activity_role"] == "communicator"
    assert "Discord: #ask-elixir" in specs["daily-clan-insight"]["delivery_targets"]
    assert specs["daily-clan-insight"]["schedule"] == "Daily at 12:00 CT."
    assert "weekly-leadership-review" in specs
    assert specs["weekly-leadership-review"]["owner_lane"] == "actions"
    assert specs["weekly-leadership-review"]["activity_role"] == "observer+communicator"
    assert "weekly-discord-invite-relay" in specs
    assert specs["weekly-discord-invite-relay"]["owner_lane"] == "actions"
    assert specs["weekly-discord-invite-relay"]["activity_role"] == "communicator"
    assert specs["weekly-discord-invite-relay"]["schedule"] == "Daily at 13:00 CT."
    assert (
        "Discord: #actions in-game-relay nudge card (quiet periods only)"
        in specs["weekly-discord-invite-relay"]["delivery_targets"]
    )
    assert "db-maintenance" in specs
    assert specs["db-maintenance"]["owner_lane"] == "elixir-log"
    assert specs["db-maintenance"]["activity_role"] == "observer+communicator"
    assert "Discord webhook: #elixir-log" in specs["db-maintenance"]["delivery_targets"]
    assert "api-sentinel" in specs
    assert specs["api-sentinel"]["owner_lane"] == "leader-lounge"
    assert specs["api-sentinel"]["activity_role"] == "observer+communicator"
    assert specs["api-sentinel"]["schedule"] == "Every 240 minutes."
    assert (
        # The sentinel has been record-only since v13; the old string claimed a
        # #leaders post that no longer happens, and this test pinned the lie.
        "None — record-only since v13; drift is read by AGENT-TEAM/error-watch.md"
        in specs["api-sentinel"]["delivery_targets"]
    )
    assert "promotion-content" in specs
    assert specs["promotion-content"]["activity_role"] == "communicator"
    assert "Discord: #recruiting" in specs["promotion-content"]["delivery_targets"]


def test_activity_registry_registers_scheduler_jobs_from_one_source():
    added = []

    class _Scheduler:
        def add_job(self, func, schedule_kind, id, **kwargs):
            added.append(
                {
                    "func": func,
                    "schedule_kind": schedule_kind,
                    "id": id,
                    "kwargs": kwargs,
                }
            )

    registered = register_scheduled_activities(
        scheduler=_Scheduler(),
        runtime_module=elixir,
        create_task=lambda fn: fn,
    )

    job_ids = {item["id"] for item in added}
    expected = {
        activity.activity_key
        for activity in list_registered_activities()
        if activity.enabled_by_default
    }
    assert {item["activity_key"] for item in registered} == expected
    assert "engine-tick" in job_ids
    assert "awareness-loop" in job_ids
    assert "daily-clan-insight" in job_ids
    assert "weekly-leadership-review" in job_ids
    assert "weekly-discord-invite-relay" in job_ids
    assert "promotion-content" in job_ids
    assert "api-sentinel" in job_ids
    # Retired generation-specific jobs are not registered.
    assert "v5-reactive-tick" not in job_ids
    assert "war-poll" not in job_ids
    assert "clan-awareness" not in job_ids
    assert "war-awareness" not in job_ids


def test_api_sentinel_tick_is_record_only_no_leader_posts():
    # The sentinel now RECORDS drift into api_sentinel_observations (the product
    # team's data source + the game-level stream's feed) and no longer posts to
    # #leader-lounge — the clan-facing news moved to the #announcements stream.
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.jobs._maintenance.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "runtime.jobs._maintenance.db.bootstrap_api_sentinel_baseline",
            return_value={
                "bootstrapped": True,
                "payloads": 3,
                "observations": 9,
            },
        ),
        patch(
            "runtime.jobs._maintenance.cr_api.get_events",
            return_value=[{"eventTag": "#E", "title": "Event"}],
        ),
        patch("runtime.jobs._maintenance.runtime_status.mark_job_start") as mock_start,
        patch("runtime.jobs._maintenance.runtime_status.mark_job_success") as mock_success,
        patch("runtime.jobs._maintenance.runtime_status.mark_job_failure") as mock_failure,
    ):
        # No pending-signal listing and no signal posting happen anymore; if the
        # tick tried, these attributes wouldn't even be patched here.
        asyncio.run(elixir._api_sentinel_tick())

    mock_start.assert_called_once_with("api_sentinel")
    mock_success.assert_called_once()
    mock_failure.assert_not_called()


def test_manual_activity_choices_exclude_internal_war_poll():
    choices = manual_activity_choices()
    values = {value for _, value in choices}

    assert "daily-clan-insight" in values
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
        patch(
            "elixir.runtime_status.snapshot",
            return_value={
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
            },
        ),
        patch(
            "elixir.db.get_system_status",
            return_value={
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
                "current_season_id": 130,
                "llm_cost_7d": {
                    "calls": 12,
                    "failures": 1,
                    "cost_usd": 3.5,
                },
                "awareness_7d": {
                    "ticks": 20,
                    "signals_in": 8,
                    "posts_delivered": 6,
                    "failed_ticks": 0,
                    "delivery_failed": 0,
                },
                "contextual_memory": {
                    "latest_memory_at": "2026-03-08T10:20:00",
                    "total": 7,
                    "leader_notes": 3,
                    "inferences": 2,
                    "system_notes": 2,
                },
            },
        ),
        patch(
            "elixir._member_role_grant_status",
            return_value={
                "configured": True,
                "ok": False,
                "reason": "Manage Roles permission missing",
                "bot_top_role_position": 2,
                "member_role_position": 3,
                "manage_roles": False,
            },
        ),
    ):
        report = elixir._build_status_report()

    assert "🛠️ Jobs:" not in report
    assert '🏷️ Release: `v3.0 "Three-Lane Elixir"`' in report
    assert "🤖 Build: `abc1234`" in report
    assert "Current war season id: 130" in report
    assert "Member role auto-grant: Manage Roles permission missing" in report
    assert "🧠 Context memory: 7 total (3 leader / 2 inference / 2 system)" in report
    assert "💸 Claude spend: 7d $3.50 across 12 call(s), projected $15.00/mo; failures 1" in report
    assert (
        "👁️ Awareness 7d: 20 tick(s), 8 signal(s), 6 post(s), failed ticks 0, delivery failures 0"
        in report
    )


def test_on_message_handles_interactive_help_directly():
    message = _make_message(100, "member-chat", "help")

    with (
        _on_message_env(
            MEMBER_CHAT_BEHAVIOR,
            mentioned=True,
            memory_context={},
            classify={
                "route": "help",
                "confidence": 0.95,
                "rationale": "asking for help",
            },
            respond=NEVER_CALLED,
        ) as env,
        patch(
            "elixir.elixir_agent.respond_to_help_request",
            return_value={
                "event_type": "help_response",
                "content": "Ask me about your deck, war participation, or recent form.",
                "summary": "...",
            },
        ) as mock_help,
    ):
        asyncio.run(elixir.on_message(message))

    mock_help.assert_called_once()
    message.reply.assert_awaited_once_with(
        "Ask me about your deck, war participation, or recent form."
    )
    assert env.save.call_args_list[1].kwargs["event_type"] == "interactive_help"
    env.respond.assert_not_called()
    env.process.assert_not_awaited()


# Every table-driven report route, as production declares it. The route tuple
# (allowed workflows, builder, event name) lives in runtime/channel_router.py --
# reading it here instead of restating it means a new route is covered the day
# it is added, and a renamed event name cannot pass a stale copy in the test.
ALL_REPORT_ROUTES = {
    **channel_router._REPORT_ROUTES,
    **channel_router._LOGGED_REPORT_ROUTES,
}

# The only per-route things a test has to supply: a plausible question and the
# text its builder returns. Everything else comes from the spec above.
REPORT_ROUTE_FIXTURES = {
    "roster_join_dates": (
        "Who are the members of the clan and when did they join?",
        "**Clan Roster + Join Dates**\n1. King Levy (coLeader) — joined 2024-01-15",
    ),
    "kick_risk": (
        "Who is at risk of being kicked based on participation thresholds?",
        "**Kick Risk (Inactive 7+ Days)**\n- Vijay — last seen 8 days ago",
    ),
    "top_war_contributors": (
        "Who are the top 5 contributors to clan wars this season?",
        "**Top War Contributors (Season 130)**\n1. King Levy — 3,200 fame across 4 race(s)",
    ),
    "status_report": (
        "What is your current status?",
        "**Elixir Status**\nUptime 4h — last engine tick 2 minutes ago",
    ),
    "schedule_report": (
        "What is on the schedule this week?",
        "**Scheduled Activities**\n- weekly-recap — Every Mon at 09:00 CT.",
    ),
}


def test_report_route_fixtures_cover_every_production_route():
    """The parametrization below is only as good as this table. A route added to
    _REPORT_ROUTES / _LOGGED_REPORT_ROUTES with no fixture here would silently
    go untested through on_message — which is exactly how status_report and
    schedule_report went untested until 2026-08-06."""
    assert set(REPORT_ROUTE_FIXTURES) == set(ALL_REPORT_ROUTES)


@pytest.mark.parametrize("route", sorted(ALL_REPORT_ROUTES))
def test_on_message_handles_report_route_directly(route):
    """A classified report route must build its report and reply with it — no
    LLM channel call — and store the reply under the route's own event_type.

    Driven off the production route tables so all five routes are covered:
    _REPORT_ROUTES (build + reply) and _LOGGED_REPORT_ROUTES (build + log +
    reply) run different code in _dispatch_intent but owe the same observable
    result, so one test asserts both.
    """
    spec = ALL_REPORT_ROUTES[route]
    question, canned_report = REPORT_ROUTE_FIXTURES[route]
    assert "clanops" in spec.workflows, "fixture routes through the clanops lane"
    message = _make_message(200, "clan-ops", question)

    with (
        _on_message_env(
            CLANOPS_BEHAVIOR,
            mentioned=True,
            classify={"route": route, "confidence": 0.95, "rationale": question},
            respond=NEVER_CALLED,
        ) as env,
        patch(f"elixir.{spec.builder}", return_value=canned_report) as mock_build,
    ):
        asyncio.run(elixir.on_message(message))

    mock_build.assert_called_once_with()
    message.reply.assert_awaited_once_with(canned_report)
    assert env.save.call_args_list[1].kwargs["event_type"] == spec.event
    env.respond.assert_not_called()
    env.process.assert_not_awaited()


def test_build_clan_status_report_summarizes_operational_clan_state():
    with (
        patch(
            "elixir.db.get_clan_roster_summary",
            return_value={
                "active_members": 21,
                "avg_collection_level": 1577,
                "avg_trophies": 7523.4,
                "donations_week_total": 1340,
            },
        ),
        patch(
            "elixir.db.list_members",
            return_value=[
                {
                    "name": "King Levy",
                    "member_ref": "King Levy (<@1474760692992180429>)",
                    "donations_week": 220,
                    "trophies": 9000,
                    "clan_rank": 1,
                },
                {
                    "name": "Finn",
                    "member_ref": "Finn",
                    "donations_week": 180,
                    "trophies": 8500,
                    "clan_rank": 2,
                },
                {
                    "name": "Vijay",
                    "member_ref": "Vijay",
                    "donations_week": 140,
                    "trophies": 8100,
                    "clan_rank": 3,
                },
            ],
        ),
        patch(
            "elixir.db.get_current_war_status",
            return_value={
                "clan_name": "POAP KINGS",
                "season_id": 77,
                "week": 2,
                "war_state": "riverRace",
                "race_rank": 1,
                "fame": 12345,
                "repair_points": 120,
                "clan_score": 4560,
            },
        ),
        patch(
            "elixir.db.get_war_season_summary",
            return_value={
                "races": 2,
                "total_clan_fame": 23456,
                "fame_per_active_member": 1116.95,
                "top_contributors": [
                    {
                        "member_ref": "King Levy (<@1474760692992180429>)",
                        "total_points": 3200,
                    },
                    {"member_ref": "Finn", "total_points": 3100},
                ],
                "nonparticipants": [{"member_ref": "Vijay"}],
            },
        ),
        patch(
            "elixir.db.get_members_at_risk",
            return_value={"members": [{"member_ref": "Vijay"}]},
        ),
        patch(
            "elixir.db.get_members_on_losing_streak",
            return_value=[{"member_ref": "Finn", "current_streak": 3}],
        ),
        patch("elixir.db.list_recent_joins", return_value=[{"member_ref": "New Guy"}]),
        patch(
            "elixir.db.get_current_war_day_state",
            return_value={
                "total_participants": 21,
                "used_all_4": [{}, {}],
                "used_some": [{}, {}, {}],
                "used_none": [{}, {}],
            },
        ),
    ):
        report = elixir._build_clan_status_report(
            {
                "name": "POAP KINGS",
                "members": 21,
                "clanScore": 55555,
                "clanWarTrophies": 3210,
                "requiredTrophies": 5000,
                "donationsPerWeek": 1400,
            },
            {"clans": [{}, {}, {}, {}, {}]},
        )

    assert report.startswith("**POAP KINGS Status**")
    assert "Roster: 21/50 members | 29 open" in report
    assert "weekly donations 1,400" in report
    assert "top donors King Levy (<@1474760692992180429>) 220, Finn 180, Vijay 140" in report
    assert "War now: season 77 | week 2 | state riverRace | boat-rank 1" in report
    assert (
        "Watch list: 1 with no war decks this season | 1 at risk | 1 on cold streaks | 1 joined in last 30d"
        in report
    )
    assert "War today: 2 used all 4 decks | 3 used some | 2 unused" in report
    assert "Recent joins: New Guy (join timing unknown)" in report
    assert "Cold streaks: Finn lost 3 straight" in report


def test_build_war_status_report_summarizes_current_war_awareness():
    with (
        patch(
            "elixir.db.get_current_war_status",
            return_value={
                "clan_name": "POAP KINGS",
                "war_state": "full",
                "season_id": 129,
                "week": 2,
                "phase_display": "Battle Day 2",
                "race_rank": 2,
                "fame": 15400,
                "clan_score": 4780,
                "period_points": 800,
            },
        ),
        patch(
            "elixir.db.get_current_war_day_state",
            return_value={
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
                "top_points_total": [
                    {"member_ref": "King Levy", "points_today": 800},
                    {"member_ref": "Finn", "points_today": 600},
                ],
                "used_none": [
                    {"member_ref": "Vijay"},
                    {"member_ref": "Ditika"},
                ],
            },
        ),
        patch(
            "elixir.db.get_war_week_summary",
            return_value={
                "participant_count": 23,
                "top_participants": [
                    {"member_ref": "King Levy", "points": 3200},
                    {"member_ref": "Finn", "points": 2900},
                ],
                "day_summaries": [
                    {
                        "phase": "battle",
                        "phase_display": "Battle Day 1",
                        "engaged_count": 20,
                        "finished_count": 11,
                        "top_points_today": [{"member_ref": "King Levy"}],
                    },
                ],
                "race": None,
            },
        ),
        patch(
            "elixir.db.get_war_season_summary",
            return_value={
                "races": 2,
                "total_clan_fame": 30100,
                "fame_per_active_member": 1204.0,
                "top_contributors": [
                    {"member_ref": "King Levy", "total_points": 6200},
                    {"member_ref": "Finn", "total_points": 5800},
                ],
                "nonparticipants": [{"member_ref": "Vijay"}],
            },
        ),
        patch(
            "elixir.db.list_recent_war_day_summaries",
            return_value=[
                {
                    "phase": "battle",
                    "phase_display": "Battle Day 2",
                    "engaged_count": 17,
                    "finished_count": 9,
                    "top_points_today": [{"member_ref": "King Levy"}],
                },
                {
                    "phase": "battle",
                    "phase_display": "Battle Day 1",
                    "engaged_count": 20,
                    "finished_count": 11,
                    "top_points_today": [{"member_ref": "Finn"}],
                },
            ],
        ),
        patch("elixir.db.get_latest_clan_boat_defense_status", return_value=None),
    ):
        report = elixir._build_war_status_report(
            {"name": "POAP KINGS"},
            {"clans": [{}, {}, {}, {}, {}]},
        )

    assert report.startswith("**POAP KINGS War Status**")
    assert "Live: state full | season 129 | week 2 | Battle Day 2 | boat-rank 2" in report
    assert "Clock: Battle Day 2 | time left 22h 29m | key `s00129-w01-p011`" in report
    assert "Engagement: 17 engaged | 9 finished all 4 | 8 untouched | 25 tracked" in report
    assert "Season points leaders (War Champ race): King Levy 800, Finn 600" in report
    assert "Waiting on: Vijay, Ditika" in report
    assert (
        "This season: 2 race(s) | total fame 30,100 | fame/member 1,204.00 | top King Levy 6,200, Finn 5,800"
        in report
    )
    assert "Live feed: 5 clan(s) in the current river race" in report


def test_build_war_status_report_includes_live_finish_and_known_stakes():
    with (
        patch(
            "elixir.db.get_current_war_status",
            return_value={
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
            },
        ),
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
    with patch(
        "elixir.db.get_database_status",
        return_value={
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
                {
                    "name": "war_participant_snapshots",
                    "row_count": 320,
                    "approx_bytes": 12288,
                },
                {"name": "members", "row_count": 50, "approx_bytes": 4096},
            ],
        },
    ):
        report = elixir._build_db_status_report()

    assert report.startswith("**Elixir DB Status**")
    assert "File: `elixir.db` | schema v15 | size 40.0 KB | WAL 8.0 KB | SHM 32.0 KB" in report
    assert "Storage: page size 4,096 B | pages 10 | free pages 2 | journal wal | tables 3" in report
    assert "Clan: 1 tables | 50 rows | 4.0 KB" in report
    assert "War: 1 tables | 320 rows | 12.0 KB" in report
    assert "Memory: 1 tables | 1,200 rows | 24.0 KB" in report
    assert "  - members: 50 rows | 4.0 KB" in report
    assert "  - war_participant_snapshots: 320 rows | 12.0 KB" in report
    assert "  - messages: 1,200 rows | 24.0 KB" in report


def test_build_db_status_report_lists_table_counts_and_sizes_for_group():
    with patch(
        "elixir.db.get_database_status",
        return_value={
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
        },
    ):
        report = elixir._build_db_status_report(group="memory")

    assert report.startswith("**Elixir DB Status | Memory**")
    assert "Group: 1 tables | 1,200 rows | 24.0 KB" in report
    assert "messages: 1,200 rows | 24.0 KB" in report
    assert "war_participation" not in report


def test_build_clan_status_report_uses_non_war_risk_watchlist():
    with (
        patch(
            "elixir.db.get_clan_roster_summary",
            return_value={
                "active_members": 21,
                "avg_collection_level": 1577,
                "avg_trophies": 7523.4,
            },
        ),
        patch("elixir.db.list_members", return_value=[]),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}) as mock_risk,
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
        patch("elixir.db.list_recent_joins", return_value=[]),
        patch("elixir.db.get_war_deck_status_today", return_value={}),
    ):
        elixir._build_clan_status_report({"name": "POAP KINGS", "members": 21}, {})

        # No threshold knobs: get_members_at_risk deletes them on arrival and
        # engine.management owns the real values.
        mock_risk.assert_called_once_with(season_id=None, conn=ANY)


def test_build_clan_status_report_formats_recent_joins_as_relative_days():
    joined_date = (datetime.now(elixir.CHICAGO).date() - timedelta(days=3)).isoformat()
    with (
        patch(
            "elixir.db.get_clan_roster_summary",
            return_value={
                "active_members": 21,
                "avg_collection_level": 1577,
                "avg_trophies": 7523.4,
            },
        ),
        patch("elixir.db.list_members", return_value=[]),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}),
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
        patch(
            "elixir.db.list_recent_joins",
            return_value=[{"member_ref": "Ditika", "joined_date": joined_date}],
        ),
        patch("elixir.db.get_war_deck_status_today", return_value={}),
    ):
        report = elixir._build_clan_status_report({"name": "POAP KINGS", "members": 21}, {})

    assert "Recent joins: Ditika (3 days ago)" in report


def test_build_clan_status_report_prefers_live_recent_join_delta():
    today = datetime.now(elixir.CHICAGO).date().isoformat()
    with (
        patch(
            "elixir.db.get_clan_roster_summary",
            return_value={
                "active_members": 21,
                "avg_collection_level": 1577,
                "avg_trophies": 7523.4,
            },
        ),
        patch("elixir.db.list_members", return_value=[]),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch(
            "elixir.db.get_war_season_summary",
            return_value={
                "races": 1,
                "total_clan_fame": 1000,
                "fame_per_active_member": 50.0,
                "top_contributors": [],
                "nonparticipants": [],
            },
        ),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}),
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
        patch(
            "elixir.db.list_recent_joins",
            return_value=[{"member_ref": "Vijay", "joined_date": "2026-03-07"}],
        ),
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

    assert (
        "Watch list: 0 with no war decks this season | 0 at risk | 0 on cold streaks | 1 joined in last 30d"
        in report
    )
    assert "Recent joins: Ditika (today)" in report
    assert "Vijay" not in report.split("Recent joins: ", 1)[1]


def test_load_live_clan_context_attaches_same_cycle_recent_joins():
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("elixir.asyncio.to_thread", side_effect=fake_to_thread),
        patch(
            "elixir.cr_api.get_clan",
            return_value={
                "name": "POAP KINGS",
                "memberList": [
                    {"tag": "#AAA", "name": "Existing"},
                    {"tag": "#BBB", "name": "Ditika"},
                ],
            },
        ),
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
        patch(
            "elixir.cr_api.get_clan",
            return_value={
                "name": "POAP KINGS",
                "memberList": [
                    {"tag": "#AAA", "name": "Existing"},
                    {"tag": "#BBB", "name": "Also Existing"},
                ],
            },
        ),
        patch(
            "elixir.db.get_active_roster_map",
            return_value={"#AAA": "Existing", "#BBB": "Also Existing"},
        ),
        patch("elixir.db.snapshot_members"),
        patch("elixir.cr_api.get_current_war", return_value={}),
    ):
        clan, war = asyncio.run(elixir._load_live_clan_context())

    assert war == {}
    assert "_elixir_recent_joins" not in clan


def test_build_roster_join_dates_report_uses_human_fallback_for_missing_dates():
    with patch(
        "elixir.db.list_members",
        return_value=[
            {"current_name": "raquaza", "role": "coLeader", "joined_date": None},
            {
                "current_name": "King Levy",
                "role": "leader",
                "joined_date": "2024-01-15",
            },
        ],
    ):
        report = elixir._build_roster_join_dates_report()

    assert "raquaza (coLeader) — join date not tracked yet" in report
    assert "King Levy (leader) — joined 2024-01-15" in report


def test_build_kick_risk_report_uses_inactivity_only():
    with patch(
        "elixir.db.get_members_at_risk",
        return_value={
            "members": [
                {
                    "member_ref": "Vijay",
                    "reasons": [
                        {"type": "inactive", "detail": "last seen 8 days ago"},
                        {"type": "low_donations", "detail": "0 donations this week"},
                    ],
                }
            ]
        },
    ) as mock_risk:
        report = elixir._build_kick_risk_report()

        # The report no longer passes threshold knobs. It used to send
        # inactivity_days=7 — which storage.war_analytics deletes on arrival —
        # and then print "Inactive 7+ Days", so the header contradicted the
        # members listed beneath it, who are at_risk from KICK_AT_RISK_DAYS.
        # Asserting those arguments was asserting a no-op.
        mock_risk.assert_called_once()
        assert "inactivity_days" not in mock_risk.call_args.kwargs
        assert "min_donations_week" not in mock_risk.call_args.kwargs

    # The header states the engine's threshold, whatever it currently is.
    assert report == (
        f"**Kick Risk (Inactive {KICK_AT_RISK_DAYS}+ Days)**\n- Vijay — last seen 8 days ago"
    )


def test_build_top_war_contributors_report_formats_season_leaders():
    with patch(
        "elixir.db.get_war_season_summary",
        return_value={
            "season_id": 130,
            "top_contributors": [
                {"member_ref": "King Levy", "total_points": 3200, "races_played": 4},
                {"member_ref": "Vijay", "total_points": 2800, "races_played": 4},
            ],
        },
    ) as mock_summary:
        report = elixir._build_top_war_contributors_report()

        mock_summary.assert_called_once_with(top_n=5, conn=ANY)
    assert report == (
        "**Top War Contributors (Season 130)**\n"
        "1. King Levy — 3,200 points across 4 race(s)\n"
        "2. Vijay — 2,800 points across 4 race(s)"
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
        patch(
            "elixir.db.get_clan_roster_summary",
            return_value={
                "active_members": 21,
                "avg_collection_level": 1577,
                "avg_trophies": 7523.4,
            },
        ),
        patch(
            "elixir.db.get_current_war_status",
            return_value={
                "clan_name": "POAP KINGS",
                "season_id": 77,
                "week": 2,
                "race_rank": 1,
                "fame": 12345,
            },
        ),
        patch(
            "elixir.db.get_war_season_summary",
            return_value={
                "fame_per_active_member": 1116.95,
                "top_contributors": [
                    {
                        "member_ref": "King Levy (<@1474760692992180429>)",
                        "total_points": 3200,
                    },
                    {"member_ref": "Finn", "total_points": 3100},
                ],
            },
        ),
        patch(
            "elixir.db.get_members_at_risk",
            return_value={"members": [{"member_ref": "Vijay"}]},
        ),
        patch(
            "elixir.db.get_members_on_losing_streak",
            return_value=[{"member_ref": "Finn", "current_streak": 3}],
        ),
    ):
        report = elixir._build_clan_status_short_report({"name": "POAP KINGS", "members": 21}, {})

    assert report.startswith("**POAP KINGS Status (Short)**")
    assert "Roster: 21/50 | open 29" in report
    assert "War: season 77 | week 2 | boat-rank 1 | boat-fame 12,345 (weekly)" in report
    assert (
        "Season: fame/member 1,117.0 | top King Levy (<@1474760692992180429>) 3,200, Finn 3,100"
        in report
    )
    assert "Watch: 1 at risk | 1 on cold streaks" in report


def test_build_clan_status_short_report_uses_non_war_risk_watchlist():
    with (
        patch(
            "elixir.db.get_clan_roster_summary",
            return_value={
                "active_members": 21,
                "avg_collection_level": 1577,
                "avg_trophies": 7523.4,
            },
        ),
        patch("elixir.db.get_current_war_status", return_value={"clan_name": "POAP KINGS"}),
        patch("elixir.db.get_war_season_summary", return_value=None),
        patch("elixir.db.get_members_at_risk", return_value={"members": []}) as mock_risk,
        patch("elixir.db.get_members_on_losing_streak", return_value=[]),
    ):
        elixir._build_clan_status_short_report({"name": "POAP KINGS", "members": 21}, {})

        # No threshold knobs: get_members_at_risk deletes them on arrival and
        # engine.management owns the real values.
        mock_risk.assert_called_once_with(season_id=None, conn=ANY)


def test_recap_context_leads_with_public_story_arcs():
    """The recap context includes the week's public-scope memory arcs so the
    recap can tell continuing member stories instead of narrating stats.
    viewer_scope='public' is load-bearing: leadership arcs must never reach
    a public channel's prompt."""
    arcs = [
        {
            "memory_id": 1,
            "title": "Vijay's comeback",
            "summary": "Three weeks of climb sealed with a 4/4 colosseum.",
            "member_tag": "#VJ1",
        },
    ]
    summaries = [
        {
            "memory_id": 2,
            "title": "Race week 3 recap",
            "summary": "Finished 2nd, best fame total this season.",
            "member_tag": None,
        },
    ]
    calls = []

    def fake_list_memories(**kwargs):
        calls.append(kwargs)
        if (kwargs.get("filters") or {}).get("source_type") == "elixir_synthesis":
            return arcs
        return summaries

    with (
        patch("memory_store.list_memories", side_effect=fake_list_memories),
        patch("elixir.db.get_weekly_recap_summary", return_value={"window_days": 7}),
        patch("elixir.db.build_clan_trend_summary_context", return_value=""),
    ):
        context = elixir._build_weekly_clan_recap_context({"name": "POAP KINGS"}, {})

    assert "=== THIS WEEK'S STORY ARCS" in context
    assert "Vijay's comeback" in context
    assert "(member #VJ1)" in context
    assert "Race week 3 recap" in context
    # Every memory query for this public context must be public-scoped.
    assert calls and all(kwargs.get("viewer_scope") == "public" for kwargs in calls)


def test_recap_context_omits_arc_block_when_no_public_memories():
    with (
        patch("memory_store.list_memories", return_value=[]),
        patch("elixir.db.get_weekly_recap_summary", return_value={"window_days": 7}),
        patch("elixir.db.build_clan_trend_summary_context", return_value=""),
    ):
        context = elixir._build_weekly_clan_recap_context({"name": "POAP KINGS"}, {})
    assert "STORY ARCS" not in context


def test_recap_context_uses_shared_game_mode_capability():
    capability = {
        "capability": "clan_game_modes",
        "contract_version": 1,
        "windows": {
            "7d": {
                "modes": {
                    "ranked": {
                        "label": "Ranked",
                        "battles": 42,
                        "members_active": 7,
                        "win_rate": 0.571,
                        "top_members": [
                            {"member_ref": "Alpha", "battles": 12},
                            {"member_ref": "Bravo", "battles": 10},
                        ],
                    }
                }
            }
        },
    }
    with (
        patch("memory_store.list_memories", return_value=[]),
        patch("elixir.db.get_weekly_recap_summary", return_value={"window_days": 7}),
        patch("elixir.db.build_clan_trend_summary_context", return_value=""),
        patch(
            "runtime.helpers._reports.game_mode_capability.get_clan_game_mode_windows",
            return_value=capability,
        ),
    ):
        context = elixir._build_weekly_clan_recap_context({"name": "POAP KINGS"}, {})

    assert "game-mode activity beyond Trophy Road" in context
    assert "Ranked: 42 battles across 7 member(s), 57% win rate" in context
    assert "most active: Alpha, Bravo" in context


def test_build_weekly_clan_recap_context_summarizes_week():
    with (
        patch("memory_store.list_memories", return_value=[]),
        patch(
            "elixir.db.get_weekly_recap_summary",
            return_value={
                "window_days": 7,
                "roster": {
                    "active_members": 21,
                    "open_slots": 29,
                    "avg_collection_level": 1577,
                    "avg_trophies": 7523.4,
                    "donations_week_total": 1400,
                },
                "war_score_trend": {
                    "direction": "up",
                    "score_change": 120,
                    "trophy_change_total": 40,
                    "races": 1,
                    "avg_rank": 1.0,
                    "avg_fame": 12345,
                },
                "war_season_summary": {
                    "season_id": 77,
                    "races": 3,
                    "total_clan_fame": 50234,
                    "fame_per_active_member": 2392.1,
                    "top_contributors": [{"member_ref": "King Levy", "total_points": 3200}],
                },
                "recent_war_races": [
                    {
                        "season_id": 77,
                        "week": 2,
                        "our_rank": 1,
                        "total_clans": 5,
                        "our_fame": 12345,
                        "trophy_change": 20,
                        "created_date": "20260308T180000.000Z",
                        "top_participants": [
                            {"member_ref": "King Levy", "points": 3200, "decks_used": 4}
                        ],
                        "standings_preview": [{"rank": 1, "name": "POAP KINGS", "fame": 12345}],
                    }
                ],
                "trending_war_contributors": {
                    "members": [{"member_ref": "Finn", "fame_delta": 400}]
                },
                "progression_highlights": [
                    {
                        "member_ref": "Vijay",
                        "level_gain": 1,
                        "pol_league_gain": 1,
                        "best_trophies_gain": 120,
                        "trophies_change": 95,
                        "wins_gain": 18,
                        "favorite_card": "Hog Rider",
                    }
                ],
                "trophy_risers": [
                    {
                        "name": "Vijay",
                        "change": 95,
                        "old_trophies": 7000,
                        "new_trophies": 7095,
                    }
                ],
                "trophy_drops": [],
                "hot_streaks": [
                    {
                        "member_ref": "Finn",
                        "current_streak": 5,
                        "summary": "8-2 over the last 10 battles (hot).",
                    }
                ],
                "top_donors": [{"member_ref": "Jamie", "donations_week": 220}],
                "recent_joins": [{"member_ref": "Newbie", "joined_date": "2026-03-08"}],
            },
        ),
        patch(
            "elixir.db.build_clan_trend_summary_context",
            return_value="=== CLAN TREND SUMMARY ===\nclan: POAP KINGS (#J2RGCRVG)",
        ),
    ):
        report = elixir._build_weekly_clan_recap_context(
            {"name": "POAP KINGS", "tag": "#J2RGCRVG"},
            {
                "clan": {
                    "fame": 13000,
                    "repairPoints": 30,
                    "clanScore": 4600,
                    "participants": [{"tag": "#A"}],
                }
            },
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
        patch(
            "elixir.db.format_member_reference",
            side_effect=fake_format_member_reference,
        ),
        patch(
            "elixir.prompts.resolve_channel_reference",
            return_value={"id": 300, "role": "announcements", "name": "#announcements"},
        ),
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


def _make_inbound_dm_message(discord_user_id, content):
    """A DM as discord.py delivers it: the channel is built via
    DMChannel._from_message, so recipients=[] and channel.recipient is None.
    Regression guard for the bug where route_message keyed DM detection on
    `.recipient` (always None inbound) and silently dropped every member reply."""
    import discord

    state = SimpleNamespace(user=SimpleNamespace(id=1))
    dm_channel = discord.DMChannel._from_message(state, channel_id=999)
    assert dm_channel.recipient is None  # documents the exact trap
    author = SimpleNamespace(
        bot=False,
        id=discord_user_id,
        name="sam.storie",
        display_name="Sam Storie",
        global_name="Sam Storie",
    )
    return SimpleNamespace(author=author, channel=dm_channel, guild=None, content=content)


def test_inbound_dm_routes_to_outreach():
    message = _make_inbound_dm_message(922500832962957343, "sure — storie@gmail.com")

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("runtime.channel_router.asyncio.to_thread", side_effect=fake_to_thread),
        patch("runtime.channel_router.db.upsert_discord_user"),
        patch("runtime.app._handle_outreach_dm", new=AsyncMock()) as mock_handle,
        patch.object(elixir.bot, "process_commands", new=AsyncMock()) as mock_process,
    ):
        asyncio.run(channel_router.route_message(message))

    mock_handle.assert_awaited_once_with(message)
    mock_process.assert_not_awaited()  # a DM must never fall through to command processing


def test_mention_overrides_not_for_bot_to_llm_chat():
    """An explicit @-mention must always engage the bot, even in clanops/#leaders
    and even when the classifier calls it not_for_bot (the 2026-07-26 miss where a
    leader tagged Elixir with an LOA note and it stayed silent)."""
    from runtime.channel_router import _normalize_open_channel_intent

    base = {"route": "not_for_bot", "confidence": 0.95, "rationale": "team note"}

    # @-mention in clanops/#leaders -> must fall back to chat, not stay silent.
    mentioned_ctx = {
        "mentioned": True,
        "workflow": "clanops",
        "allows_open_channel_reply": False,
        "raw_question": "1spaceO2 and pigsareus are away a week; don't flag them",
    }
    out = _normalize_open_channel_intent(mentioned_ctx, dict(base))
    assert out["route"] == "llm_chat"
    assert out["fallback_reason"] == "mention_not_for_bot_override"

    # Not mentioned in a mention-only clanops channel -> unchanged (stays silent).
    quiet_ctx = {
        "mentioned": False,
        "workflow": "clanops",
        "allows_open_channel_reply": False,
        "raw_question": "human chatter",
    }
    assert _normalize_open_channel_intent(quiet_ctx, dict(base))["route"] == "not_for_bot"

    # Open interactive lane (not mentioned) keeps the existing open-channel rescue.
    open_ctx = {
        "mentioned": False,
        "workflow": "interactive",
        "allows_open_channel_reply": True,
        "raw_question": "who donates the most?",
    }
    out_open = _normalize_open_channel_intent(open_ctx, dict(base))
    assert out_open["route"] == "llm_chat"
    assert out_open["fallback_reason"] == "open_channel_not_for_bot_override"


def test_report_route_builders_exist_on_app():
    """The table-driven report routes resolve their builder by name via getattr,
    so a renamed/removed builder would fail at RUNTIME (mid-conversation) rather
    than at import. Pin every builder name to a real attribute on runtime.app."""
    import runtime.app as app
    from runtime.channel_router import _LOGGED_REPORT_ROUTES, _REPORT_ROUTES

    specs = {**_REPORT_ROUTES, **_LOGGED_REPORT_ROUTES}
    assert specs, "report route tables should not be empty"
    missing = [
        f"{route} -> app.{spec.builder}"
        for route, spec in specs.items()
        if not hasattr(app, spec.builder)
    ]
    assert not missing, f"report route builders missing on runtime.app: {missing}"

    # Every route key must be a real route the intent router can emit.
    from runtime.intent_registry import ROUTE_KEYS

    unknown = [route for route in specs if route not in ROUTE_KEYS]
    assert not unknown, f"report routes not in ROUTE_KEYS: {unknown}"


def test_missing_required_secrets_detects_blank_and_unset(monkeypatch):
    """Elixir refuses to boot when a required secret is unset or blank, naming
    every missing one — instead of failing later inside discord.py / the first
    LLM call."""
    from runtime import status as runtime_status

    for name in runtime_status.REQUIRED_SECRETS:
        monkeypatch.setenv(name, "present")
    assert runtime_status.missing_required_secrets() == []

    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_API_KEY", "   ")  # blank counts as missing
    missing = runtime_status.missing_required_secrets()
    assert "DISCORD_TOKEN" in missing and "CLAUDE_API_KEY" in missing
    assert "CR_API_KEY" not in missing


def test_status_snapshot_env_keys_are_stable(monkeypatch):
    """The snapshot env keys are consumed by the status report badges, so the
    key names must not drift when the env manifest changes."""
    from runtime import status as runtime_status

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("CLAUDE_API_KEY", "x")
    monkeypatch.delenv("CR_API_KEY", raising=False)
    env = runtime_status.snapshot()["env"]
    assert set(env) == {
        "has_discord_token",
        "has_claude_api_key",
        "has_cr_api_key",
        "has_elixir_log_webhook",
    }
    assert env["has_discord_token"] is True
    assert env["has_cr_api_key"] is False
