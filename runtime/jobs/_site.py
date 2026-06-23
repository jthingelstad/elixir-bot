"""Clan-recruiting promotion content (Discord/Reddit copy).

POAP KINGS *website* publishing was removed 2026-06-21 — the site now has its own
standalone update script and Elixir no longer writes to it. What remains is the
`promotion-content` job: it composes recruiting copy and posts it to the
#recruiting Discord channel (Reddit copy is included in the same post for a human
to cross-post). No external website writes happen here anymore.
"""

__all__ = [
    "_promotion_discord_required_text", "_promotion_reddit_required_token",
    "_promotion_channel_posts", "_unwrap_outer_bold",
    "_validate_promote_content_or_raise", "_promotion_content_cycle",
]

import asyncio
import logging

import db
import elixir_agent
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id
from runtime import status as runtime_status
from runtime.helpers._common import _load_live_clan_context, _post_to_elixir


log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


def _promotion_discord_required_text(trophies):
    return f"Required Trophies: [{trophies}]"


def _promotion_reddit_required_token(trophies):
    return f"[{trophies}]"


def _promotion_channel_posts(promote):
    posts = []
    discord_body = (((promote or {}).get("discord") or {}).get("body") or "").strip()
    reddit = (promote or {}).get("reddit") or {}
    reddit_title = (reddit.get("title") or "").strip()
    reddit_body = (reddit.get("body") or "").strip()

    if discord_body:
        posts.append(
            "**Discord recruiting copy**\n"
            f"```text\n{discord_body}\n```"
        )
    if reddit_title or reddit_body:
        reddit_lines = ["**Reddit recruiting copy**"]
        if reddit_title:
            reddit_lines.append(f"Title: `{reddit_title}`")
        if reddit_body:
            reddit_lines.append(f"```text\n{reddit_body}\n```")
        posts.append("\n".join(reddit_lines))
    return posts


def _unwrap_outer_bold(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) >= 4:
        return stripped[2:-2].strip()
    return stripped


def _validate_promote_content_or_raise(promote, required_trophies=2000) -> None:
    discord_text = _promotion_discord_required_text(required_trophies)
    reddit_token = _promotion_reddit_required_token(required_trophies)

    discord = (promote or {}).get("discord") or {}
    discord_body = (discord.get("body") or "").strip()
    if discord_body:
        first_line = next((line.strip() for line in discord_body.splitlines() if line.strip()), "")
        first_line = _unwrap_outer_bold(first_line)
        if discord_text not in first_line:
            raise ValueError(
                f"discord.body first line must include exact text `{discord_text}`"
            )
        if not first_line.endswith(discord_text):
            raise ValueError(
                f"discord.body first line must end with exact text `{discord_text}`"
            )

    reddit = (promote or {}).get("reddit") or {}
    reddit_title = (reddit.get("title") or "").strip()
    reddit_body = (reddit.get("body") or "").strip()
    if (reddit_title or reddit_body) and reddit_token not in reddit_title:
        raise ValueError(
            f"reddit.title must include exact token `{reddit_token}`"
        )


async def _promotion_content_cycle():
    runtime_status.mark_job_start("promotion_content_cycle")
    try:
        promotion_channel_id = _get_singleton_channel_id("promotion")
    except Exception as exc:
        runtime_status.mark_job_failure("promotion_content_cycle", f"promotion channel config error: {exc}")
        return

    channel = _bot().get_channel(promotion_channel_id)
    if not channel:
        runtime_status.mark_job_failure("promotion_content_cycle", "promotion channel not found")
        return

    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.error("Promotion content refresh failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"refresh failed: {exc}")
        return

    if not clan.get("memberList"):
        runtime_status.mark_job_success("promotion_content_cycle", "no member data")
        return

    promote = await asyncio.to_thread(
        elixir_agent.generate_promote_content,
        clan,
        war_data=war,
        roster_data=None,
    )
    if not promote:
        runtime_status.mark_job_success("promotion_content_cycle", "no promotion content")
        return
    try:
        _validate_promote_content_or_raise(promote, required_trophies=clan.get("requiredTrophies", 2000))
    except Exception as exc:
        log.error("Promotion content validation failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("promotion_content_cycle", f"invalid promotion content: {exc}")
        return

    channel_posts = _promotion_channel_posts(promote)
    if not channel_posts:
        runtime_status.mark_job_success("promotion_content_cycle", "no promotion channel copy")
        return

    await _post_to_elixir(channel, {"content": channel_posts})
    ch = _channel_msg_kwargs(channel)
    for index, post in enumerate(channel_posts):
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel), "assistant", post,
            **ch, workflow="promotion",
            event_type="promotion_content_cycle" if index == 0 else "promotion_content_cycle_part",
        )
    runtime_status.mark_job_success("promotion_content_cycle", "Discord promotion content published")
