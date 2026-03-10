"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import atexit
import json
import os
import re
import signal
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api
import db
import site_content
import elixir_agent
import heartbeat
import prompts
from runtime.admin import admin_command_requires_leader, dispatch_admin_command, parse_admin_command
from runtime.channel_router import route_message
from runtime.discord_commands import register_elixir_app_commands
from runtime import onboarding
from runtime import status as runtime_status
from runtime.system_signals import queue_startup_system_signals

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
_dc = prompts.discord_config()
MEMBER_ROLE_ID = _dc.get("member_role", 0)
LEADER_ROLE_ID = _dc.get("leader_role", 0)
BOT_ROLE_ID = _dc.get("bot_role", 0)
GUILD_ID = int(_dc.get("guild_id", 0) or 0)
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))
CLANOPS_PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("CLANOPS_PROACTIVE_COOLDOWN_SECONDS", "900"))
CHANNEL_CONVERSATION_LIMIT = 20

# Active hours for the heartbeat (Chicago time). Outside this window, heartbeat is skipped.
HEARTBEAT_START_HOUR = int(os.getenv("HEARTBEAT_START_HOUR", "7"))
HEARTBEAT_END_HOUR = int(os.getenv("HEARTBEAT_END_HOUR", "22"))
HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "47"))
HEARTBEAT_JITTER_SECONDS = int(os.getenv("HEARTBEAT_JITTER_SECONDS", "300"))
PROMOTION_CONTENT_DAY = os.getenv("PROMOTION_CONTENT_DAY", "fri")
PROMOTION_CONTENT_HOUR = int(os.getenv("PROMOTION_CONTENT_HOUR", "9"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)
APP_GUILD = discord.Object(id=GUILD_ID) if GUILD_ID else None
SLASH_COMMANDS_SYNCED = False
_CR_API_ALERT_SIGNATURE = None
_CR_API_OUTAGE_ALERT_SIGNATURE = None


def _format_hour_label(hour: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}{suffix}"


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


def _clear_cr_api_failure_alert_if_recovered() -> None:
    global _CR_API_ALERT_SIGNATURE, _CR_API_OUTAGE_ALERT_SIGNATURE
    api = (runtime_status.snapshot().get("api") or {})
    if api.get("last_ok") is True:
        _CR_API_ALERT_SIGNATURE = None
        _CR_API_OUTAGE_ALERT_SIGNATURE = None


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
    global _CR_API_ALERT_SIGNATURE, _CR_API_OUTAGE_ALERT_SIGNATURE

    channel_configs = prompts.discord_channels_by_role("clanops")
    if not channel_configs:
        log.warning("CR API auth failure alert skipped: no clanops channel configured")
        return False
    channel = bot.get_channel(channel_configs[0]["id"])
    if not channel:
        log.warning("CR API auth failure alert skipped: clanops channel not found")
        return False

    api = runtime_status.snapshot().get("api") or {}
    king_thing_ref = await asyncio.to_thread(
        db.format_member_reference,
        "#20JJJ2CCRU",
        "name_with_mention",
    )
    if not king_thing_ref or king_thing_ref == "#20JJJ2CCRU":
        king_thing_ref = "@King Thing"
    sent = False

    auth_signature = _cr_api_failure_signature()
    if auth_signature and auth_signature != _CR_API_ALERT_SIGNATURE:
        content = (
            f"{king_thing_ref} Clash Royale API access just failed during {context}.\n"
            f"Last status: {api.get('last_status_code') or 'n/a'} on `{api.get('last_endpoint') or 'unknown'}` "
            f"for `{api.get('last_entity_key') or '-'}`.\n"
            "This usually means the CR API key or its IP allowlist needs to be updated."
        )
        await _post_to_elixir(channel, {"content": content})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            content,
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="clanops",
            event_type="cr_api_auth_failure",
        )
        _CR_API_ALERT_SIGNATURE = auth_signature
        sent = True

    outage_signature = _cr_api_outage_signature()
    if outage_signature and outage_signature != _CR_API_OUTAGE_ALERT_SIGNATURE:
        consecutive_failures = int(api.get("consecutive_error_count") or 0)
        content = (
            f"{king_thing_ref} Clash Royale API has failed {consecutive_failures} times in a row during {context}.\n"
            f"Last status: {api.get('last_status_code') or 'n/a'} on `{api.get('last_endpoint') or 'unknown'}` "
            f"for `{api.get('last_entity_key') or '-'}`.\n"
            f"Last error: `{(api.get('last_error') or 'unknown error')[:180]}`"
        )
        await _post_to_elixir(channel, {"content": content})
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            content,
            channel_id=channel.id,
            channel_name=getattr(channel, "name", None),
            channel_kind=str(channel.type),
            workflow="clanops",
            event_type="cr_api_outage",
        )
        _CR_API_OUTAGE_ALERT_SIGNATURE = outage_signature
        sent = True

    return sent


def _has_leader_role(member) -> bool:
    if not LEADER_ROLE_ID:
        return True
    return any(getattr(role, "id", None) == LEADER_ROLE_ID for role in getattr(member, "roles", []))


def _is_clanops_channel(channel) -> bool:
    channel_config = _get_channel_behavior(getattr(channel, "id", 0))
    return bool(channel_config and channel_config.get("role") == "clanops")


def _chunk_discord_text(text: str, limit: int = 2000) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    return [text[i:i + (limit - 10)] for i in range(0, len(text), limit - 10)]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _post_to_elixir(channel, entry: dict):
    """Post an entry's content to a configured Discord channel."""
    for post in _entry_posts(entry):
        if len(post) > 2000:
            for chunk in [post[i:i+1990] for i in range(0, len(post), 1990)]:
                await channel.send(chunk)
        else:
            await channel.send(post)


def _entry_posts(entry: dict, field="content"):
    content = entry.get(field, entry.get("summary", ""))
    if not content:
        return []
    if isinstance(content, list):
        return [item.strip() for item in content if isinstance(item, str) and item.strip()]
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else []
    return [str(content)]


def _preview_text(value, limit=500):
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            text = repr(value)
    return text[:limit]


def _normalize_prompt_failure_question(question):
    text = (question or "").strip()
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"<@&\d+>", " ", text)
    return " ".join(text.split())


def _log_prompt_failure(*, question, workflow, failure_type, failure_stage, channel, author,
                        discord_message_id=None, detail=None, result_preview=None, raw_json=None):
    openai = runtime_status.snapshot().get("openai") or {}
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
            openai_last_error=openai.get("last_error"),
            openai_last_model=openai.get("last_model"),
            openai_last_call_at=openai.get("last_call_at"),
            raw_json=raw_json,
        )
        log.warning(
            "prompt_failure id=%s workflow=%s type=%s stage=%s channel_id=%s author_id=%s question=%r detail=%r openai_model=%s openai_error=%r",
            failure_id,
            workflow,
            failure_type,
            failure_stage,
            getattr(channel, "id", None),
            getattr(author, "id", None),
            _preview_text(clean_question, limit=180),
            _preview_text(detail, limit=240),
            openai.get("last_model"),
            _preview_text(openai.get("last_error"), limit=240),
        )
    except Exception as exc:
        log.error("prompt failure logging error: %s", exc)


def __export_public(module):
    names = getattr(module, "__all__", None) or [
        name for name in vars(module) if not name.startswith("__")
    ]
    for name in names:
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
                await bot.tree.sync(guild=APP_GUILD)
                log.info("Synced /elixir commands to guild %s", GUILD_ID)
            else:
                await bot.tree.sync()
                log.info("Synced global /elixir commands")
            SLASH_COMMANDS_SYNCED = True
        except Exception as exc:
            log.error("Slash command sync failed: %s", exc)
    if not scheduler.running:
        # Single heartbeat job replaces both the 4x/day observations and hourly member check
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_heartbeat_tick())
            ),
            "interval",
            minutes=HEARTBEAT_INTERVAL_MINUTES,
            jitter=HEARTBEAT_JITTER_SECONDS,
            id="heartbeat",
        )
        # Daily site publish for poapkings.com: refresh data, generate content, commit/push.
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_site_content_cycle())
            ),
            "cron",
            hour=SITE_CONTENT_HOUR,
            minute=0,
            id="site_content_cycle",
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_player_intel_refresh())
            ),
            "interval",
            minutes=PLAYER_INTEL_REFRESH_MINUTES,
            id="player_intel_refresh",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_clanops_weekly_review())
            ),
            "cron",
            day_of_week=CLANOPS_WEEKLY_REVIEW_DAY,
            hour=CLANOPS_WEEKLY_REVIEW_HOUR,
            minute=0,
            id="clanops_weekly_review",
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_weekly_clan_recap())
            ),
            "cron",
            day_of_week=WEEKLY_RECAP_DAY,
            hour=WEEKLY_RECAP_HOUR,
            minute=0,
            id="weekly_clan_recap",
        )
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_promotion_content_cycle())
            ),
            "cron",
            day_of_week=PROMOTION_CONTENT_DAY,
            hour=PROMOTION_CONTENT_HOUR,
            minute=0,
            id="promotion_content_cycle",
        )
        scheduler.start()
        log.info("Scheduler started — heartbeat every %d minutes with up to %ds jitter (active %dam-%dpm Chicago), "
                 "site publish at %s, player intel refresh every %d minutes, clanops review %s at %02d:00, weekly recap %s at %02d:00, "
                 "promotion sync %s at %02d:00",
                 HEARTBEAT_INTERVAL_MINUTES, HEARTBEAT_JITTER_SECONDS, HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR,
                 _format_hour_label(SITE_CONTENT_HOUR), PLAYER_INTEL_REFRESH_MINUTES,
                 CLANOPS_WEEKLY_REVIEW_DAY, CLANOPS_WEEKLY_REVIEW_HOUR,
                 WEEKLY_RECAP_DAY, WEEKLY_RECAP_HOUR,
                 PROMOTION_CONTENT_DAY, PROMOTION_CONTENT_HOUR)
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


def _is_legacy_clanops_command_text(text: str) -> bool:
    return bool(
        _is_help_request(text)
        or _is_status_request(text)
        or _is_schedule_request(text)
        or _clan_status_mode(text)
        or _is_clan_list_request(text)
        or _extract_profile_target(text)
        or parse_admin_command(text, require_prefix=True)
    )


def _build_clanops_command_hint() -> str:
    return (
        "Use `/elixir ...` for private clanops commands or mention me with `@Elixir do ...` "
        "for public room commands."
    )


@bot.event
async def on_message(message):
    await route_message(message)


PID_FILE = os.path.join(os.path.dirname(__file__), "elixir.pid")


def _acquire_pid_file():
    """Write current PID to file, killing any stale process first."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            log.info("Killed stale process %d", old_pid)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # PID invalid, process gone, or not ours
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


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
    bot.run(TOKEN)
