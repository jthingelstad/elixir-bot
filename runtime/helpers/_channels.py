import asyncio
import re

import db
import prompts

__all__ = [
    "_channel_scope", "_channel_conversation_scope", "_strip_bot_mentions",
    "_is_bot_mentioned", "_leading_bot_mention_pattern", "_get_channel_behavior",
    "_get_singleton_channel", "_get_singleton_channel_id", "_channel_reply_target_name",
    "_reply_text", "_share_channel_result",
]

from runtime.helpers._common import (
    _bot,
    _bot_role_id,
    _log,
    _post_to_elixir,
    _runtime_app,
)
from runtime.helpers._members import _apply_member_refs_to_result


def _channel_scope(channel) -> str:
    return f"channel:{channel.id}"


def _channel_conversation_scope(channel, discord_user_id) -> str:
    return f"channel_user:{channel.id}:{discord_user_id}"


def _strip_bot_mentions(text: str) -> str:
    text = (text or "").lstrip()
    pattern = _leading_bot_mention_pattern()
    if pattern is None:
        return text.strip()
    while True:
        match = pattern.match(text)
        if not match:
            break
        text = text[match.end():].lstrip()
    return text.strip()


def _is_bot_mentioned(message) -> bool:
    pattern = _leading_bot_mention_pattern()
    if pattern is None:
        return False
    return bool(pattern.match(getattr(message, "content", "") or ""))


def _leading_bot_mention_pattern():
    parts = []
    bot_user = getattr(_bot(), "user", None)
    bot_id = getattr(bot_user, "id", None)
    if bot_id:
        parts.append(rf"<@!?{bot_id}>")
    bot_role_id = _bot_role_id()
    if bot_role_id:
        parts.append(rf"<@&{bot_role_id}>")
    if not parts:
        return None
    return re.compile(rf"^\s*(?:{'|'.join(parts)})(?:\s+|$)")


def _get_channel_behavior(channel_id):
    return prompts.discord_channels_by_id().get(channel_id)


def _get_singleton_channel(subagent):
    return prompts.discord_singleton_subagent(subagent)


def _get_singleton_channel_id(subagent):
    return _get_singleton_channel(subagent)["id"]


def _channel_reply_target_name(channel_config):
    return channel_config.get("name") or f"channel:{channel_config['id']}"


async def _reply_text(message, content):
    def _discord_safe_content(text: str) -> str:
        text = text or ""

        def _replace_image(match):
            alt = (match.group(1) or "").strip()
            url = (match.group(2) or "").strip()
            return f"{alt}: {url}" if alt else url

        return re.sub(r"!\[([^\]]*)\]\((https?://[^)]+)\)", _replace_image, text)

    posts = []
    if isinstance(content, list):
        posts = [item for item in content if isinstance(item, str) and item.strip()]
    else:
        posts = [content]

    sent_messages = []
    for post in posts:
        safe_post = _discord_safe_content(post)
        safe_post = _runtime_app()._resolve_custom_emoji(safe_post, getattr(message, "guild", None))
        if len(safe_post) > 2000:
            for chunk in [safe_post[i:i + 1990] for i in range(0, len(safe_post), 1990)]:
                sent = await message.reply(chunk)
                if sent is not None:
                    sent_messages.append(sent)
        else:
            sent = await message.reply(safe_post)
            if sent is not None:
                sent_messages.append(sent)
    return sent_messages


async def _share_channel_result(result, workflow):
    result = await _apply_member_refs_to_result(result)
    if result.get("event_type") != "channel_share":
        return
    share_content = result.get("share_content", "")
    if not share_content:
        return
    target_ref = result.get("share_channel") or "#clan-events"
    target = prompts.resolve_channel_reference(target_ref)
    if not target:
        _log().warning("Unknown share target channel: %s", target_ref)
        return
    target_channel = _bot().get_channel(target["id"])
    if not target_channel:
        return
    await _post_to_elixir(target_channel, {"content": share_content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(target_channel),
        "assistant",
        share_content,
        channel_id=target_channel.id,
        channel_name=getattr(target_channel, "name", None),
        channel_kind=str(target_channel.type),
        workflow=workflow,
        event_type=result.get("event_type"),
    )
