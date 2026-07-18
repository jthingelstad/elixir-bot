"""Startup audit and announcement helpers."""

from __future__ import annotations

import asyncio
import logging
import platform

import db
import elixir_agent
import prompts
from runtime import elixir_log
from runtime.helpers import _channel_msg_kwargs, _channel_scope

log = logging.getLogger("elixir")


def _member_role_grant_status() -> dict:
    from runtime import app as runtime_app

    status = {
        "configured": bool(runtime_app.MEMBER_ROLE_ID),
        "guild_found": False,
        "member_role_found": False,
        "bot_role_found": False,
        "manage_roles": None,
        "member_role_position": None,
        "bot_top_role_position": None,
        "ok": False,
        "reason": "member role not configured",
    }
    if not runtime_app.MEMBER_ROLE_ID:
        return status
    guild = runtime_app.bot.get_guild(runtime_app.GUILD_ID) if runtime_app.GUILD_ID else None
    if guild is None:
        status["reason"] = "guild not cached"
        return status
    status["guild_found"] = True
    member_role = guild.get_role(runtime_app.MEMBER_ROLE_ID)
    bot_role = guild.get_role(runtime_app.BOT_ROLE_ID) if runtime_app.BOT_ROLE_ID else None
    me = guild.me
    if member_role is None:
        status["reason"] = "member role not found"
        return status
    status["member_role_found"] = True
    status["member_role_position"] = member_role.position
    if bot_role is not None:
        status["bot_role_found"] = True
    if me is None:
        status["reason"] = "bot member not cached"
        return status
    status["manage_roles"] = me.guild_permissions.manage_roles
    status["bot_top_role_position"] = getattr(me.top_role, "position", None)
    if not status["manage_roles"]:
        status["reason"] = "Manage Roles permission missing"
        return status
    if getattr(me.top_role, "position", -1) <= member_role.position:
        status["reason"] = "bot role must be above member role"
        return status
    status["ok"] = True
    status["reason"] = "ok"
    return status


async def _resolve_runtime_channel(channel_id: int):
    from runtime import app as runtime_app

    channel = runtime_app.bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await runtime_app.bot.fetch_channel(channel_id)
    except Exception:
        log.warning("channel_fetch_failed channel_id=%s", channel_id, exc_info=True)
        return None


def _audit_targets(runtime_app) -> list[tuple[dict, list[str]]]:
    """(channel_config, extra-required perms) for every channel Elixir must post to.

    Beyond View + Send, some channels need perms the generic check used to miss:
    embed_links wherever Elixir posts embeds (leader-action cards on the arena-relay
    lane), and — for #thinking — embed_links plus thread perms (it posts an embed
    then opens a per-tick thread). #thinking is env-configured, NOT in
    discord_channel_configs, so it must be added explicitly or it audits as clean
    while silently 403'ing (the 2026-07-18 outage)."""
    targets: list[tuple[dict, list[str]]] = []
    for cfg in prompts.discord_channel_configs():
        if not cfg.get("workflow"):
            continue
        extra = ["embed_links"] if cfg.get("lane") == "arena-relay" else []
        targets.append((cfg, extra))
    thinking_id = getattr(runtime_app, "THINKING_CHANNEL_ID", None)
    if thinking_id:
        targets.append(
            (
                {"id": thinking_id, "name": "#thinking"},
                ["embed_links", "create_public_threads", "send_messages_in_threads"],
            )
        )
    return targets


async def _startup_channel_audit_summary() -> str:
    from runtime import app as runtime_app

    active_targets = _audit_targets(runtime_app)
    if not active_targets:
        return "Channel audit: no active configured channels found."
    ok_names = []
    issues = []
    bot_member_cache = {}
    for channel_config, extra_required in active_targets:
        channel = await _resolve_runtime_channel(channel_config["id"])
        channel_name = channel_config.get("name") or f"#{channel_config['id']}"
        if channel is None:
            issues.append(f"{channel_name} missing or unreachable")
            continue
        guild = getattr(channel, "guild", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if (
            guild is not None
            and callable(permissions_for)
            and getattr(runtime_app.bot, "user", None)
        ):
            me = getattr(guild, "me", None)
            if me is None and hasattr(guild, "get_member"):
                cache_key = getattr(guild, "id", channel_config["id"])
                me = bot_member_cache.get(cache_key)
                if me is None:
                    me = guild.get_member(runtime_app.bot.user.id)
                    bot_member_cache[cache_key] = me
            if me is not None:
                perms = permissions_for(me)
                if not getattr(perms, "view_channel", True):
                    issues.append(f"{channel_name} not visible")
                    continue
                if not getattr(perms, "send_messages", True):
                    issues.append(f"{channel_name} not writable")
                    continue
                # embed_links / thread perms are hard for the channels that need
                # them: a missing embed_links 403s every card even when Send is fine.
                missing = [p for p in extra_required if not getattr(perms, p, True)]
                for soft in ("read_message_history", "add_reactions", "use_external_emojis"):
                    if not getattr(perms, soft, True):
                        missing.append(soft)
                if missing:
                    issues.append(f"{channel_name} missing perms: {', '.join(missing)}")
                    continue
        ok_names.append(channel_name)
    ok_text = (
        f"Channel audit: {len(ok_names)}/{len(active_targets)} "
        "active channels reachable and writable."
    )
    if not issues:
        return ok_text
    issue_text = "Issues: " + "; ".join(issues[:6])
    if len(issues) > 6:
        issue_text += f"; +{len(issues) - 6} more"
    return f"{ok_text}\n{issue_text}"


async def _post_startup_message() -> bool:
    from runtime import app as runtime_app

    channel_configs = prompts.discord_channels_by_workflow("clanops")
    channel = None
    recent_posts = []
    if channel_configs:
        channel_id = channel_configs[0]["id"]
        channel = await _resolve_runtime_channel(channel_id)
        if channel:
            recent_posts = await asyncio.to_thread(
                db.list_channel_messages, channel.id, 5, "assistant"
            )
    startup_context = (
        "This is a startup check-in for the private Elixir operations log.\n"
        "Write only the fun Clash Royale-inspired body that follows a fixed startup header.\n"
        "Keep it to 1-2 short sentences.\n"
        "Sound alive, sharp, and a little playful, like Elixir just entered the arena and is ready to work.\n"
        "Do not repeat the build hash or invent version numbers.\n"
        "Do not repeat the release label or invent alternate codenames.\n"
        "Do not mention hidden mechanics, JSON, prompts, models, or scheduler internals.\n"
        "This is not a public announcement. It is an operational visibility signal.\n"
        "Custom Discord emoji are welcome if they fit naturally.\n\n"
        f"Running release: {elixir_agent.RELEASE_LABEL}\n"
        f"Running build hash: {elixir_agent.BUILD_HASH}\n"
        "Required facts already handled outside your text:\n"
        "- Elixir is online\n"
        f"- Release will be shown exactly as `{elixir_agent.RELEASE_LABEL}`\n"
        f"- Build hash will be shown exactly as `{elixir_agent.BUILD_HASH}`"
    )
    try:
        fun_line = await asyncio.to_thread(
            elixir_agent.generate_message,
            "clanops_startup",
            startup_context,
            recent_posts=recent_posts,
        )
    except Exception as exc:
        log.error("Startup message generation failed: %s", exc)
        fun_line = None
    if not fun_line:
        fun_line = ":elixir_hype: Elixir is in the arena and the decks are shuffled. Leadership view is live."
    hostname = platform.node() or "unknown"
    channel_audit = await _startup_channel_audit_summary()
    content = (
        "**Elixir Online**\n"
        f"Release: **{elixir_agent.RELEASE_LABEL}** \u00b7 Build: **{elixir_agent.BUILD_HASH}** \u00b7 Host: **{hostname}**\n"
        f"{fun_line.strip()}\n"
        f"{channel_audit}"
    )
    if await elixir_log.post_event_async(content):
        return True
    if not channel_configs:
        log.warning(
            "Startup message skipped: no leadership channel configured and elixir-log webhook unavailable"
        )
        return False
    if not channel:
        log.warning(
            "Startup message skipped: leadership channel not found or unreachable and elixir-log webhook unavailable"
        )
        return False
    try:
        await runtime_app._post_to_elixir(channel, {"content": content})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            content,
            **_channel_msg_kwargs(channel),
            workflow="clanops",
            event_type="startup_announcement",
        )
        return True
    except Exception as exc:
        log.error("Startup message post failed: %s", exc, exc_info=True)
        return False
