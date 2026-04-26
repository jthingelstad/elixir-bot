"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import json
import os
import re
import signal
import logging
import subprocess
import sys

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
from runtime import process as _process_service
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

HEARTBEAT_INTERVAL_MINUTES = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "60"))
ASK_ELIXIR_DAILY_INSIGHT_HOUR = int(os.getenv("ASK_ELIXIR_DAILY_INSIGHT_HOUR", "12"))
ASK_ELIXIR_DAILY_INSIGHT_MINUTE = int(os.getenv("ASK_ELIXIR_DAILY_INSIGHT_MINUTE", "0"))
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


def _has_leader_role(member) -> bool:
    if not LEADER_ROLE_ID:
        return True
    return any(getattr(role, "id", None) == LEADER_ROLE_ID for role in getattr(member, "roles", []))


def _is_clanops_channel(channel) -> bool:
    channel_config = _get_channel_behavior(getattr(channel, "id", 0))
    return bool(channel_config and channel_config.get("workflow") == "clanops")


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

from runtime.alerts import (  # noqa: E402,F401
    _ALERT_SIGNATURES,
    _admin_mention_ref,
    _alert_admin,
    _clear_alert,
    _clear_cr_api_failure_alert_if_recovered,
    _clear_llm_failure_alert_if_recovered,
    _cr_api_failure_signature,
    _cr_api_outage_signature,
    _is_hard_fail_llm_error,
    _llm_outage_signature,
    _maybe_alert_cr_api_failure,
    _maybe_alert_llm_failure,
    schedule_llm_failure_alert,
)
from runtime.discord_posting import (  # noqa: E402,F401
    _chunk_discord_text,
    _entry_posts,
    _normalize_entry_posts,
    _post_to_elixir,
    _resolve_custom_emoji,
)
from runtime.startup import (  # noqa: E402,F401
    _member_role_grant_status,
    _post_startup_message,
    _resolve_runtime_channel,
    _startup_channel_audit_summary,
)

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
        # Recover any deferred recap that didn't post before this restart.
        try:
            from runtime.jobs._tournament import resume_pending_tournament_recaps
            await resume_pending_tournament_recaps()
        except Exception as exc:
            log.warning("Pending tournament recap resume failed: %s", exc)
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


PID_FILE = _process_service.PID_FILE


def _read_pid_file() -> int | None:
    return _process_service._read_pid_file(PID_FILE)


def _write_pid_file() -> None:
    return _process_service._write_pid_file(PID_FILE, os_module=os)


def _process_exists(pid: int) -> bool:
    return _process_service._process_exists(pid, os_module=os)


def _process_command(pid: int) -> str:
    return _process_service._process_command(pid, subprocess_module=subprocess)


def _pid_looks_like_elixir(pid: int) -> bool:
    return _process_service._pid_looks_like_elixir(pid, process_command=_process_command)


def _wait_for_process_exit(pid: int, timeout_seconds: float = 5.0) -> bool:
    return _process_service._wait_for_process_exit(
        pid,
        timeout_seconds,
        process_exists=_process_exists,
    )


def _acquire_pid_file():
    return _process_service._acquire_pid_file(
        pid_file=PID_FILE,
        read_pid_file=_read_pid_file,
        write_pid_file=_write_pid_file,
        process_exists=_process_exists,
        pid_looks_like_elixir=_pid_looks_like_elixir,
        wait_for_process_exit=_wait_for_process_exit,
        os_module=os,
        signal_module=signal,
        logger=log,
    )


def _cleanup_pid_file():
    return _process_service._cleanup_pid_file(PID_FILE, os_module=os)


def main():
    return _process_service.main(
        TOKEN,
        bot,
        acquire_pid_file=_acquire_pid_file,
        cleanup_pid_file=_cleanup_pid_file,
    )
