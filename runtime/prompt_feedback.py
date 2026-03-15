from __future__ import annotations

import asyncio

import db


THUMBS_UP = "\N{THUMBS UP SIGN}"
THUMBS_DOWN = "\N{THUMBS DOWN SIGN}"


def _feedback_content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        parts = [str(item).strip() for item in content if str(item).strip()]
        return "\n\n".join(parts)
    return str(content or "").strip()


def feedback_value_for_emoji(emoji) -> str | None:
    value = str(emoji or "").strip()
    if value == THUMBS_UP:
        return "up"
    if value == THUMBS_DOWN:
        return "down"
    return None


def should_collect_ask_elixir_feedback(channel_name: str, workflow: str, question: str, content) -> bool:
    from agent.workflows import _is_lightweight_ask_elixir_turn

    if (workflow or "").strip().lower() != "interactive":
        return False
    if (channel_name or "").strip().lower() != "#ask-elixir":
        return False
    if _is_lightweight_ask_elixir_turn(channel_name, question):
        return False
    return bool(_feedback_content_text(content))


async def add_feedback_reactions(sent_messages, *, channel_name: str, workflow: str, question: str, content) -> None:
    if not should_collect_ask_elixir_feedback(channel_name, workflow, question, content):
        return
    if len(sent_messages or []) != 1:
        return
    message = sent_messages[0]
    if not message:
        return
    try:
        await message.add_reaction(THUMBS_UP)
        await message.add_reaction(THUMBS_DOWN)
    except Exception:
        import runtime.app as app

        app.log.warning("Failed to pre-seed prompt feedback reactions for ask-elixir message", exc_info=True)


def _assistant_message_lookup(payload) -> tuple[dict | None, dict | None]:
    import runtime.app as app

    channel_config = app._get_channel_behavior(payload.channel_id)
    if not channel_config:
        return None, None
    if (channel_config.get("subagent") or "") != "ask-elixir":
        return channel_config, None
    assistant = db.get_message_by_discord_message_id(payload.message_id)
    return channel_config, assistant


async def _fetch_channel_and_message(payload):
    import runtime.app as app

    channel = app.bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await app.bot.fetch_channel(payload.channel_id)
        except Exception:
            return None, None
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return channel, None
    return channel, message


async def _post_retry_invitation(payload, *, prompt_feedback_id: int | None) -> None:
    channel, message = await _fetch_channel_and_message(payload)
    if channel is None or message is None:
        return
    content = (
        f"<@{payload.user_id}> if that missed, ask me again or tell me what felt off "
        "and I'll take another shot."
    )
    try:
        sent = await message.reply(content)
        if prompt_feedback_id:
            await asyncio.to_thread(
                db.mark_prompt_feedback_retry_invited,
                prompt_feedback_id,
                retry_message_id=getattr(sent, "id", None),
            )
    except Exception:
        import runtime.app as app

        app.log.warning("Failed to send ask-elixir retry invitation after thumbs-down", exc_info=True)


async def handle_raw_reaction_add(payload) -> None:
    import runtime.app as app

    if not payload or not payload.channel_id or not payload.message_id or not payload.user_id:
        return
    if app.bot.user and int(payload.user_id) == int(app.bot.user.id):
        return
    if getattr(getattr(payload, "member", None), "bot", False):
        return
    feedback_value = feedback_value_for_emoji(getattr(payload, "emoji", None))
    if not feedback_value:
        return

    channel_config, assistant = await asyncio.to_thread(_assistant_message_lookup, payload)
    if not channel_config or not assistant:
        return
    if assistant.get("author_type") != "assistant":
        return
    if (assistant.get("workflow") or "").strip().lower() != "interactive":
        return
    if (assistant.get("discord_user_id") or "") != str(payload.user_id):
        return

    feedback = await asyncio.to_thread(
        db.upsert_prompt_feedback,
        assistant_discord_message_id=payload.message_id,
        discord_user_id=payload.user_id,
        original_asker_discord_user_id=assistant.get("discord_user_id"),
        workflow=assistant.get("workflow"),
        channel_id=assistant.get("channel_id"),
        channel_name=channel_config.get("name"),
        feedback_value=feedback_value,
    )
    if feedback_value == "down" and feedback.get("became_active_down"):
        await _post_retry_invitation(payload, prompt_feedback_id=feedback.get("prompt_feedback_id"))


async def handle_raw_reaction_remove(payload) -> None:
    import runtime.app as app

    if not payload or not payload.channel_id or not payload.message_id or not payload.user_id:
        return
    if app.bot.user and int(payload.user_id) == int(app.bot.user.id):
        return
    feedback_value = feedback_value_for_emoji(getattr(payload, "emoji", None))
    if not feedback_value:
        return

    channel_config, assistant = await asyncio.to_thread(_assistant_message_lookup, payload)
    if not channel_config or not assistant:
        return
    if assistant.get("author_type") != "assistant":
        return
    if (assistant.get("workflow") or "").strip().lower() != "interactive":
        return
    if (assistant.get("discord_user_id") or "") != str(payload.user_id):
        return

    await asyncio.to_thread(
        db.clear_prompt_feedback,
        assistant_discord_message_id=payload.message_id,
        discord_user_id=payload.user_id,
        feedback_value=feedback_value,
    )


__all__ = [
    "THUMBS_DOWN",
    "THUMBS_UP",
    "_feedback_content_text",
    "add_feedback_reactions",
    "feedback_value_for_emoji",
    "handle_raw_reaction_add",
    "handle_raw_reaction_remove",
    "should_collect_ask_elixir_feedback",
]
