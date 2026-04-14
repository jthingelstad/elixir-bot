"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import atexit
import json
import os
import re
import signal
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api  # re-exported; accessed by runtime submodules
import db
import elixir_agent
import heartbeat  # re-exported; patched in tests
import prompts
from modules.poap_kings import site as poap_kings_site
from runtime.activities import format_scheduler_startup_summary, register_scheduled_activities
from runtime.admin import admin_command_requires_leader, dispatch_admin_command
from runtime.channel_router import route_message
from runtime.discord_commands import register_elixir_app_commands
from runtime import onboarding
from runtime import prompt_feedback
from runtime import status as runtime_status
from runtime.emoji import sync_emoji
from runtime.system_signals import queue_startup_system_signals

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
# Quiet noisy third-party loggers so operational signals stay readable.
# discord.py installs its own handler via utils.setup_logging() in client.run();
# we pass log_handler=None below to suppress it, and clear any handlers it may
# have attached at import time so messages don't double-print.
for _noisy in ("apscheduler", "apscheduler.scheduler", "apscheduler.executors.default", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
for _discord_logger in ("discord", "discord.client", "discord.gateway", "discord.http"):
    _dl = logging.getLogger(_discord_logger)
    _dl.handlers.clear()
    _dl.propagate = True
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
_dc = prompts.discord_config()
MEMBER_ROLE_ID = _dc.get("member_role", 0)
LEADER_ROLE_ID = _dc.get("leader_role", 0)
BOT_ROLE_ID = _dc.get("bot_role", 0)
GUILD_ID = int(_dc.get("guild_id", 0) or 0)
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))
CHANNEL_CONVERSATION_LIMIT = 20

HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "30"))
HEARTBEAT_JITTER_SECONDS = int(os.getenv("HEARTBEAT_JITTER_SECONDS", "900"))
ASK_ELIXIR_DAILY_INSIGHT_HOUR = int(os.getenv("ASK_ELIXIR_DAILY_INSIGHT_HOUR", "12"))
ASK_ELIXIR_DAILY_INSIGHT_MINUTE = int(os.getenv("ASK_ELIXIR_DAILY_INSIGHT_MINUTE", "0"))
ASK_ELIXIR_DAILY_INSIGHT_JITTER_SECONDS = int(os.getenv("ASK_ELIXIR_DAILY_INSIGHT_JITTER_SECONDS", "1800"))
PROMOTION_CONTENT_DAY = os.getenv("PROMOTION_CONTENT_DAY", "fri")
PROMOTION_CONTENT_HOUR = int(os.getenv("PROMOTION_CONTENT_HOUR", "9"))
ADMIN_DISCORD_ID = os.getenv("ADMIN_DISCORD_ID")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)
APP_GUILD = discord.Object(id=GUILD_ID) if GUILD_ID else None
SLASH_COMMANDS_SYNCED = False
_ALERT_SIGNATURES: dict[str, str | None] = {}

def _member_role_grant_status() -> dict:
    status = {
        "configured": bool(MEMBER_ROLE_ID),
        "guild_found": False,
        "member_role_found": False,
        "bot_role_found": False,
        "manage_roles": None,
        "member_role_position": None,
        "bot_top_role_position": None,
        "ok": False,
        "reason": "member role not configured",
    }
    if not MEMBER_ROLE_ID:
        return status
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild is None:
        status["reason"] = "guild not cached"
        return status
    status["guild_found"] = True
    member_role = guild.get_role(MEMBER_ROLE_ID)
    bot_role = guild.get_role(BOT_ROLE_ID) if BOT_ROLE_ID else None
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


def _admin_mention_ref() -> str:
    """Return admin display name with Discord @mention when ADMIN_DISCORD_ID is set."""
    name = db.format_member_reference("#20JJJ2CCRU")
    if not name or name == "#20JJJ2CCRU":
        name = "King Thing"
    if ADMIN_DISCORD_ID:
        return f"{name} (<@{ADMIN_DISCORD_ID}>)"
    return name


async def _alert_admin(content: str, event_type: str, signature: str) -> bool:
    """Post a deduped alert to the clanops channel. Returns True if a message was sent."""
    if _ALERT_SIGNATURES.get(event_type) == signature:
        return False

    channel_configs = prompts.discord_channels_by_workflow("clanops")
    if not channel_configs:
        log.warning("Admin alert skipped (%s): no clanops channel configured", event_type)
        return False
    channel = bot.get_channel(channel_configs[0]["id"])
    if not channel:
        log.warning("Admin alert skipped (%s): clanops channel not found", event_type)
        return False

    await _post_to_elixir(channel, {"content": content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        content,
        **_channel_msg_kwargs(channel),
        workflow="clanops",
        event_type=event_type,
    )
    _ALERT_SIGNATURES[event_type] = signature
    return True


def _clear_alert(*event_types: str) -> None:
    for et in event_types:
        _ALERT_SIGNATURES.pop(et, None)


# ── CR API alerts ─────────────────────────────────────────────────────────


def _clear_cr_api_failure_alert_if_recovered() -> None:
    api = (runtime_status.snapshot().get("api") or {})
    if api.get("last_ok") is True:
        _clear_alert("cr_api_auth_failure", "cr_api_outage")


def _cr_api_failure_signature() -> str | None:
    api = (runtime_status.snapshot().get("api") or {})
    if api.get("last_ok") is not False:
        return None
    status_code = api.get("last_status_code")
    if status_code not in {401, 403}:
        return None
    last_error = (api.get("last_error") or "").strip()
    endpoint = api.get("last_endpoint") or "unknown"
    entity_key = api.get("last_entity_key") or "-"
    return f"{status_code}|{endpoint}|{entity_key}|{last_error[:160]}"


def _cr_api_outage_signature() -> str | None:
    api = (runtime_status.snapshot().get("api") or {})
    if api.get("last_ok") is not False:
        return None
    if int(api.get("consecutive_error_count") or 0) < 3:
        return None
    status_code = api.get("last_status_code")
    last_error = (api.get("last_error") or "").strip()
    endpoint = api.get("last_endpoint") or "unknown"
    entity_key = api.get("last_entity_key") or "-"
    return f"{status_code}|{endpoint}|{entity_key}|{last_error[:160]}|{api.get('consecutive_error_count')}"


async def _maybe_alert_cr_api_failure(context: str) -> bool:
    api = runtime_status.snapshot().get("api") or {}
    admin_ref = await asyncio.to_thread(_admin_mention_ref)
    sent = False

    auth_sig = _cr_api_failure_signature()
    if auth_sig:
        content = (
            f"{admin_ref} Clash Royale API access just failed during {context}.\n"
            f"Last status: {api.get('last_status_code') or 'n/a'} on `{api.get('last_endpoint') or 'unknown'}` "
            f"for `{api.get('last_entity_key') or '-'}`.\n"
            "This usually means the CR API key or its IP allowlist needs to be updated."
        )
        sent = await _alert_admin(content, "cr_api_auth_failure", auth_sig) or sent

    outage_sig = _cr_api_outage_signature()
    if outage_sig:
        consecutive_failures = int(api.get("consecutive_error_count") or 0)
        content = (
            f"{admin_ref} Clash Royale API has failed {consecutive_failures} times in a row during {context}.\n"
            f"Last status: {api.get('last_status_code') or 'n/a'} on `{api.get('last_endpoint') or 'unknown'}` "
            f"for `{api.get('last_entity_key') or '-'}`.\n"
            f"Last error: `{(api.get('last_error') or 'unknown error')[:180]}`"
        )
        sent = await _alert_admin(content, "cr_api_outage", outage_sig) or sent

    return sent


# ── LLM alerts ────────────────────────────────────────────────────────────


def _clear_llm_failure_alert_if_recovered() -> None:
    llm = (runtime_status.snapshot().get("llm") or {})
    if llm.get("last_ok") is True:
        _clear_alert("llm_outage")


def _llm_outage_signature() -> str | None:
    llm = (runtime_status.snapshot().get("llm") or {})
    if llm.get("last_ok") is not False:
        return None
    if int(llm.get("consecutive_error_count") or 0) < 3:
        return None
    last_error = (llm.get("last_error") or "").strip()
    workflow = llm.get("last_workflow") or "unknown"
    model = llm.get("last_model") or "unknown"
    return f"{workflow}|{model}|{last_error[:160]}"


async def _maybe_alert_llm_failure(context: str) -> bool:
    sig = _llm_outage_signature()
    if not sig:
        return False
    llm = runtime_status.snapshot().get("llm") or {}
    admin_ref = await asyncio.to_thread(_admin_mention_ref)
    consecutive = int(llm.get("consecutive_error_count") or 0)
    content = (
        f"{admin_ref} LLM API has failed {consecutive} times in a row during {context}.\n"
        f"Workflow: `{llm.get('last_workflow') or 'unknown'}`, model: `{llm.get('last_model') or 'unknown'}`.\n"
        f"Last error: `{(llm.get('last_error') or 'unknown error')[:180]}`"
    )
    return await _alert_admin(content, "llm_outage", sig)


async def _resolve_runtime_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        log.warning("channel_fetch_failed channel_id=%s", channel_id, exc_info=True)
        return None


async def _startup_channel_audit_summary() -> str:
    active_channels = [
        channel
        for channel in prompts.discord_channel_configs()
        if channel.get("workflow")
    ]
    if not active_channels:
        return "Channel audit: no active configured channels found."

    ok_names = []
    issues = []
    bot_member_cache = {}

    for channel_config in active_channels:
        channel = await _resolve_runtime_channel(channel_config["id"])
        channel_name = channel_config.get("name") or f"#{channel_config['id']}"
        if channel is None:
            issues.append(f"{channel_name} missing or unreachable")
            continue

        guild = getattr(channel, "guild", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if guild is not None and callable(permissions_for) and getattr(bot, "user", None):
            me = getattr(guild, "me", None)
            if me is None and hasattr(guild, "get_member"):
                cache_key = getattr(guild, "id", channel_config["id"])
                me = bot_member_cache.get(cache_key)
                if me is None:
                    me = guild.get_member(bot.user.id)
                    bot_member_cache[cache_key] = me
            if me is not None:
                perms = permissions_for(me)
                can_view = getattr(perms, "view_channel", True)
                can_send = getattr(perms, "send_messages", True)
                if not can_view:
                    issues.append(f"{channel_name} not visible")
                    continue
                if not can_send:
                    issues.append(f"{channel_name} not writable")
                    continue

        ok_names.append(channel_name)

    if not issues:
        return f"Channel audit: {len(ok_names)}/{len(active_channels)} active channels reachable and writable."

    ok_text = (
        f"Channel audit: {len(ok_names)}/{len(active_channels)} active channels reachable and writable."
    )
    issue_text = "Issues: " + "; ".join(issues[:6])
    if len(issues) > 6:
        issue_text += f"; +{len(issues) - 6} more"
    return f"{ok_text}\n{issue_text}"


async def _post_startup_message() -> bool:
    channel_configs = prompts.discord_channels_by_workflow("clanops")
    if not channel_configs:
        log.warning("Startup message skipped: no leadership channel configured")
        return False

    channel_id = channel_configs[0]["id"]
    channel = await _resolve_runtime_channel(channel_id)
    if not channel:
        log.warning("Startup message skipped: leadership channel not found or unreachable")
        return False

    recent_posts = await asyncio.to_thread(
        db.list_channel_messages,
        channel.id,
        5,
        "assistant",
    )
    startup_context = (
        "This is a startup check-in for the private clan leadership channel.\n"
        "Write only the fun Clash Royale-inspired body that follows a fixed startup header.\n"
        "Keep it to 1-2 short sentences.\n"
        "Sound alive, sharp, and a little playful, like Elixir just entered the arena and is ready to work.\n"
        "Do not repeat the build hash or invent version numbers.\n"
        "Do not repeat the release label or invent alternate codenames.\n"
        "Do not mention hidden mechanics, JSON, prompts, models, or scheduler internals.\n"
        "This is not a public announcement. It is a leadership-facing startup signal.\n"
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

    channel_audit = await _startup_channel_audit_summary()
    content = (
        "**Elixir Online**\n"
        f"Release: `{elixir_agent.RELEASE_LABEL}`\n"
        f"Build: `{elixir_agent.BUILD_HASH}`\n"
        f"{fun_line.strip()}\n"
        f"{channel_audit}"
    )
    try:
        await _post_to_elixir(channel, {"content": content})
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


def _has_leader_role(member) -> bool:
    if not LEADER_ROLE_ID:
        return True
    return any(getattr(role, "id", None) == LEADER_ROLE_ID for role in getattr(member, "roles", []))


def _is_clanops_channel(channel) -> bool:
    channel_config = _get_channel_behavior(getattr(channel, "id", 0))
    return bool(channel_config and channel_config.get("workflow") == "clanops")


def _chunk_discord_text(text: str, limit: int = 2000) -> list[str]:
    return _chunk_for_discord(text, size=limit - 10)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_custom_emoji(text: str, guild) -> str:
    """Replace :emoji_name: shortcodes with <:emoji_name:id> for guild custom emoji."""
    if not guild or not guild.emojis:
        return text
    emoji_map = {e.name: e for e in guild.emojis}
    def _replace(m):
        name = m.group(1)
        e = emoji_map.get(name)
        if e:
            prefix = "a" if e.animated else ""
            return f"<{prefix}:{e.name}:{e.id}>"
        return m.group(0)
    return re.sub(r":([a-zA-Z0-9_]{2,32}):", _replace, text)


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


async def _post_to_elixir(channel, entry: dict):
    """Post an entry's content to a configured Discord channel."""
    guild = getattr(channel, "guild", None)
    for post in _entry_posts(entry):
        post = _resolve_custom_emoji(post, guild)
        if len(post) > DISCORD_MAX_MESSAGE_LEN:
            for chunk in _chunk_for_discord(post):
                await channel.send(chunk)
        else:
            await channel.send(post)


def _entry_posts(entry: dict, field="content"):
    content = entry.get(field, entry.get("summary", ""))
    if not content:
        return []
    return _normalize_entry_posts(content)


def _preview_text(value, limit=500):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(value)
    return text[:limit]


def _normalize_prompt_failure_question(question):
    text = (question or "").strip()
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"<@&\d+>", " ", text)
    return " ".join(text.split())


def _log_prompt_failure(*, question, workflow, failure_type, failure_stage, channel, author,
                        discord_message_id=None, detail=None, result_preview=None, raw_json=None):
    llm = runtime_status.snapshot().get("llm") or {}
    clean_question = _normalize_prompt_failure_question(question)
    try:
        failure_id = db.record_prompt_failure(
            clean_question,
            failure_type,
            failure_stage,
            workflow=workflow,
            channel_id=getattr(channel, "id", None),
            channel_name=getattr(channel, "name", None),
            discord_user_id=getattr(author, "id", None),
            discord_message_id=discord_message_id,
            detail=detail,
            result_preview=result_preview,
            llm_last_error=llm.get("last_error"),
            llm_last_model=llm.get("last_model"),
            llm_last_call_at=llm.get("last_call_at"),
            raw_json=raw_json,
        )
        log.warning(
            "prompt_failure id=%s workflow=%s type=%s stage=%s channel_id=%s author_id=%s question=%r detail=%r llm_model=%s llm_error=%r",
            failure_id,
            workflow,
            failure_type,
            failure_stage,
            getattr(channel, "id", None),
            getattr(author, "id", None),
            _preview_text(clean_question, limit=180),
            _preview_text(detail, limit=240),
            llm.get("last_model"),
            _preview_text(llm.get("last_error"), limit=240),
        )
    except Exception as exc:
        log.error("prompt failure logging error: %s", exc)


def __export_public(module):
    names = getattr(module, "__all__", None) or [
        name for name in vars(module) if not name.startswith("__")
    ]
    protected = {"BOT_ROLE_ID", "CHICAGO", "LEADER_ROLE_ID", "bot", "log", "scheduler"}
    for name in names:
        if name in protected:
            continue
        globals()[name] = getattr(module, name)
    return names


from runtime import helpers as _helpers_module
from runtime import jobs as _jobs_module

__all__ = [name for name in globals() if not name.startswith("__")]
for _module in (_helpers_module, _jobs_module):
    __export_public(_module)

__all__ = [name for name in globals() if not name.startswith("__")]
register_elixir_app_commands(bot)

@bot.event
async def on_ready():
    global SLASH_COMMANDS_SYNCED
    log.info("Elixir online as %s", bot.user)
    prompts.ensure_valid_discord_channel_config()
    await asyncio.to_thread(queue_startup_system_signals)
    role_status = _member_role_grant_status()
    if role_status["configured"] and not role_status["ok"]:
        log.warning(
            "Member role auto-grant unavailable: %s (manage_roles=%s, bot_top_role_position=%s, member_role_position=%s)",
            role_status["reason"],
            role_status["manage_roles"],
            role_status["bot_top_role_position"],
            role_status["member_role_position"],
        )
    if not SLASH_COMMANDS_SYNCED:
        try:
            if APP_GUILD is not None:
                # Clear stale global commands from older releases when we are
                # intentionally operating with a guild-scoped slash surface.
                await bot.tree.sync()
                await bot.tree.sync(guild=APP_GUILD)
                log.info("Synced /elixir commands to guild %s and cleared stale global commands", GUILD_ID)
            else:
                await bot.tree.sync()
                log.info("Synced global /elixir commands")
            SLASH_COMMANDS_SYNCED = True
        except Exception as exc:
            log.error("Slash command sync failed: %s", exc)
    # Sync custom emoji
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await sync_emoji(guild)
    if not scheduler.running:
        def _job_runner(job_callable):
            return lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(job_callable())
            )

        register_scheduled_activities(
            scheduler=scheduler,
            runtime_module=sys.modules[__name__],
            create_task=_job_runner,
        )
        scheduler.start()
        startup_posted = await _post_startup_message()
        if not startup_posted:
            log.warning("Startup announcement was not posted to leadership")
        log.info("Scheduler started — %s", format_scheduler_startup_summary(sys.modules[__name__]))
        # Resume tournament watch if one was active before restart
        try:
            active_tournament = await asyncio.to_thread(db.get_active_tournament)
            if active_tournament:
                from runtime.jobs import start_tournament_watch
                start_tournament_watch()
                log.info(
                    "Resumed tournament watch for %s (%s)",
                    active_tournament.get("name", "?"),
                    active_tournament["tournament_tag"],
                )
        except Exception as exc:
            log.warning("Tournament watch resume check failed: %s", exc)
        # Best-effort startup card catalog sync
        try:
            from runtime.jobs import _card_catalog_sync
            bot.loop.create_task(_card_catalog_sync())
        except Exception as exc:
            log.warning("Startup card catalog sync failed: %s", exc)
        # Re-register persistent daily quiz view
        try:
            from modules.card_training.views import restore_daily_view
            await restore_daily_view(bot)
        except Exception as exc:
            log.warning("Daily quiz view restore failed: %s", exc)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #reception."""
    await onboarding.handle_member_join(member)


@bot.event
async def on_member_update(before, after):
    """Detect nickname changes and grant member role when name matches a clan member."""
    await onboarding.handle_member_update(before, after)


@bot.event
async def on_message(message):
    await route_message(message)


@bot.event
async def on_raw_reaction_add(payload):
    await prompt_feedback.handle_raw_reaction_add(payload)


@bot.event
async def on_raw_reaction_remove(payload):
    await prompt_feedback.handle_raw_reaction_remove(payload)


PID_FILE = os.path.join(os.path.dirname(__file__), "elixir.pid")


def _read_pid_file() -> int | None:
    try:
        with open(PID_FILE) as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw
    if isinstance(payload, dict):
        pid = payload.get("pid")
    else:
        pid = payload
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def _write_pid_file() -> None:
    payload = {
        "pid": os.getpid(),
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cwd": os.getcwd(),
        "entrypoint": "elixir.py",
    }
    with open(PID_FILE, "w") as f:
        json.dump(payload, f)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _pid_looks_like_elixir(pid: int) -> bool:
    command = _process_command(pid).lower()
    if not command:
        return False
    markers = {
        "elixir.py",
        "runtime.app",
        os.path.basename(os.path.dirname(__file__)).lower(),
    }
    return any(marker and marker in command for marker in markers)


def _wait_for_process_exit(pid: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.1)
    return not _process_exists(pid)


def _acquire_pid_file():
    """Write current PID to file, killing any stale process first."""
    if os.path.exists(PID_FILE):
        old_pid = _read_pid_file()
        if old_pid and old_pid != os.getpid() and _process_exists(old_pid):
            if _pid_looks_like_elixir(old_pid):
                try:
                    os.kill(old_pid, signal.SIGTERM)
                except PermissionError as exc:
                    raise RuntimeError(
                        f"Existing Elixir process {old_pid} could not be terminated."
                    ) from exc
                if not _wait_for_process_exit(old_pid):
                    raise RuntimeError(
                        f"Existing Elixir process {old_pid} did not exit after SIGTERM."
                    )
                log.info("Stopped prior Elixir process %d", old_pid)
            else:
                log.warning(
                    "Ignoring stale pid file %s pointing to non-Elixir process %d",
                    PID_FILE,
                    old_pid,
                )
    _write_pid_file()


def _cleanup_pid_file():
    """Remove PID file on clean shutdown."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    _acquire_pid_file()
    atexit.register(_cleanup_pid_file)
    bot.run(TOKEN, log_handler=None)
