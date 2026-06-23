"""Discord posting and message normalization helpers."""

from __future__ import annotations

import logging
import re

import emoji

from runtime.helpers._common import DISCORD_MAX_MESSAGE_LEN, _chunk_for_discord

log = logging.getLogger("elixir")


def _chunk_discord_text(text: str, limit: int = 2000) -> list[str]:
    return _chunk_for_discord(text, size=limit - 10)


def _resolve_custom_emoji(text: str, guild) -> str:
    emoji_map = {e.name: e for e in (guild.emojis if guild else [])}

    def _is_unicode_shortcode(name: str) -> bool:
        shortcode = f":{name}:"
        return (
            emoji.emojize(shortcode, language="alias") != shortcode
            or emoji.emojize(shortcode) != shortcode
        )

    def _replace(m):
        name = m.group(1)
        custom = emoji_map.get(name)
        if custom:
            prefix = "a" if custom.animated else ""
            return f"<{prefix}:{custom.name}:{custom.id}>"
        if _is_unicode_shortcode(name):
            return m.group(0)
        if name.startswith("elixir_"):
            unicode_name = name.removeprefix("elixir_")
            if _is_unicode_shortcode(unicode_name):
                log.info(
                    "emoji shortcode normalized: :%s: is not custom, using Unicode :%s:",
                    name,
                    unicode_name,
                )
                return f":{unicode_name}:"
        log.info("emoji shortcode stripped: :%s: is not a guild custom emoji or Unicode shortcode", name)
        return ""

    cleaned = re.sub(r":([a-zA-Z][a-zA-Z0-9_]{1,31}):", _replace, text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


_POST_MERGE_STOPWORDS = {
    "about", "after", "again", "all", "also", "an", "and", "are", "around", "back", "been",
    "before", "between", "both", "but", "can", "clan", "day", "days", "discord",
    "everyone", "for", "from", "get", "getting", "has", "have", "help", "here",
    "into", "just", "keep", "kings", "lets", "live", "member", "members", "more", "much",
    "need", "news", "our", "out", "over", "poap", "post", "posts", "right", "same", "show",
    "still", "team", "that", "the", "their", "them", "there", "these", "this", "those",
    "through", "today", "topic", "update", "updates", "using", "want", "with", "your",
}


def _content_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9']+", (text or "").lower())
        if len(token) >= 4 and token not in _POST_MERGE_STOPWORDS
    }


def _should_merge_related_posts(posts: list[str]) -> bool:
    if len(posts) < 2 or len(posts) > 4:
        return False
    if sum(len(post) for post in posts) > 1200:
        return False
    term_sets = [_content_terms(post) for post in posts]
    non_empty = [terms for terms in term_sets if terms]
    if len(non_empty) < 2:
        return False
    shared = set.intersection(*non_empty)
    if len(shared) >= 2:
        return True
    overlaps = []
    for idx, left in enumerate(non_empty):
        for right in non_empty[idx + 1:]:
            baseline = max(1, min(len(left), len(right)))
            overlaps.append(len(left & right) / baseline)
    return bool(overlaps) and (sum(overlaps) / len(overlaps)) >= 0.34


def _normalize_entry_posts(content) -> list[str]:
    if isinstance(content, list):
        posts = [item.strip() for item in content if isinstance(item, str) and item.strip()]
        if _should_merge_related_posts(posts):
            return ["\n\n".join(posts)]
        return posts
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else []
    return [str(content)] if content is not None else []


def _entry_posts(entry: dict, field="content"):
    content = entry.get(field, entry.get("summary", ""))
    if not content:
        return []
    return _normalize_entry_posts(content)


async def _post_to_elixir(channel, entry: dict):
    guild = getattr(channel, "guild", None)
    sent_messages = []
    for post in _entry_posts(entry):
        post = _resolve_custom_emoji(post, guild)
        if len(post) > DISCORD_MAX_MESSAGE_LEN:
            for chunk in _chunk_for_discord(post):
                sent_messages.append(await channel.send(chunk))
        else:
            sent_messages.append(await channel.send(post))
    return sent_messages


async def compose_and_post(channel, *, lane: str, context: str, leadership: bool = False) -> bool:
    """Agent-compose an in-voice update from `context` and post it to `channel`.

    The v5-style replacement for the v4 awareness-delivery path (`_deliver_signal_
    group_via_awareness`) in scheduled clan jobs (awards, tournaments, weekly relay):
    one direct agent compose + post, reusing v5's copy extraction and meta-refusal
    guard. Returns True only on a confirmed send."""
    import asyncio

    if channel is None:
        return False
    try:
        import elixir_agent

        result = await asyncio.to_thread(
            elixir_agent.generate_channel_update,
            getattr(channel, "name", lane), lane, context, leadership=leadership,
        )
    except Exception:
        log.exception("compose_and_post: agent failed for lane %s", lane)
        return False
    from event_core.live.runtime import _extract_copy, _looks_like_meta

    copy = _extract_copy(result)
    if not copy or _looks_like_meta(copy):
        log.warning("compose_and_post: no usable copy for lane %s", lane)
        return False
    await _post_to_elixir(channel, {"content": copy})
    return True
