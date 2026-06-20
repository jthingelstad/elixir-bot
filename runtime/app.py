"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import json
import os
import re
import logging
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
scheduler = AsyncIOScheduler(
    timezone=CHICAGO,
    # misfire_grace_time defaults to 1s, which silently drops any cron that
    # fires while the event loop is briefly busy. Give every job a few minutes
    # of grace and collapse missed runs into one. max_instances=1 prevents a
    # slow tick from overlapping its next run (now effective — see below).
    job_defaults={"misfire_grace_time": 300, "coalesce": True, "max_instances": 1},
)
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


# ── The `elixir` runtime surface ─────────────────────────────────────────────
# This module doubles as the top-level `elixir` module (see elixir.py), and
# scheduling (runtime.activities resolves job functions and config constants
# by name on this module), other runtime modules, and the test suite all
# address helpers and jobs through it. These imports ARE that surface — they
# replaced a dynamic __export_public copy loop, so keep them explicit.

from runtime.helpers import (  # noqa: E402,F401
    DISCORD_CHUNK_SIZE,
    DISCORD_MAX_MESSAGE_LEN,
    _DB_STATUS_MEMORY_TABLES,
    _author_msg_kwargs,
    _bot,
    _bot_role_id,
    _build_clan_status_report,
    _build_clan_status_short_report,
    _build_db_status_report,
    _build_help_report,
    _build_kick_risk_report,
    _build_member_deck_report,
    _build_member_war_decks_report,
    _build_roster_join_dates_report,
    _build_schedule_report,
    _build_status_report,
    _build_top_war_contributors_report,
    _build_war_status_report,
    _build_weekly_clan_recap_context,
    _canon_tag,
    _channel_conversation_scope,
    _channel_msg_kwargs,
    _channel_reply_target_name,
    _channel_scope,
    _chicago,
    _chunk_for_discord,
    _db_status_group_for_table,
    _db_status_group_label,
    _extract_member_deck_target,
    _fallback_channel_response,
    _fmt_bytes,
    _fmt_iso_short,
    _fmt_num,
    _fmt_relative,
    _format_relative_join_age,
    _get_channel_behavior,
    _get_singleton_channel,
    _get_singleton_channel_id,
    _is_bot_mentioned,
    _job_next_runs,
    _join_member_bits,
    _leader_role_id,
    _leader_role_mention,
    _leading_bot_mention_pattern,
    _load_live_clan_context,
    _log,
    _match_clan_member,
    _member_label,
    _recent_join_display_rows,
    _reply_text,
    _resolve_member_candidate,
    _runtime_app,
    _safe_create_task,
    _safe_reply,
    _schedule_specs,
    _scheduler,
    _share_channel_result,
    _status_badge,
    _strip_bot_mentions,
    _with_leader_ping,
)
from runtime.jobs._core import (  # noqa: E402,F401
    WAR_AWARENESS_MINUTE,
    WAR_POLL_MINUTE,
    WEEKLY_DISCORD_INVITE_RELAY_DAY,
    WEEKLY_DISCORD_INVITE_RELAY_HOUR,
    WEEKLY_RECAP_DAY,
    WEEKLY_RECAP_HOUR,
    _ask_elixir_daily_insight,
    _award_detection_tick,
    _build_ask_elixir_daily_insight_context,
    _clan_awareness_tick,
    _leadership_action_scan,
    _query_or_default,
    _summarize_member_rows,
    _war_awareness_tick,
    _war_poll_tick,
    _weekly_clan_recap,
    _weekly_discord_invite_relay,
)
from runtime.jobs._intel import (  # noqa: E402,F401
    PLAYER_INTEL_BATCH_SIZE,
    PLAYER_INTEL_REFRESH_HOURS,
    PLAYER_INTEL_REFRESH_MINUTES,
    PLAYER_INTEL_REQUEST_SPACING_SECONDS,
    PLAYER_INTEL_STALE_HOURS,
    _clan_wars_intel_report,
    _player_intel_refresh,
    _player_intel_refresh_minutes,
)
from runtime.jobs._maintenance import (  # noqa: E402,F401
    API_SENTINEL_POLL_MINUTES,
    _api_sentinel_tick,
    _build_maintenance_report,
    _card_catalog_sync,
    _db_maintenance_cycle,
    _format_size,
)
from runtime.jobs._memory import (  # noqa: E402,F401
    MEMORY_SYNTHESIS_DAY,
    MEMORY_SYNTHESIS_DRY_RUN,
    MEMORY_SYNTHESIS_HOUR,
    MEMORY_SYNTHESIS_POSTS_PER_CHANNEL,
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
    _memory_synthesis_cycle,
)
from runtime.jobs._signals import (  # noqa: E402,F401
    _WEEKLY_RECAP_HEADER_RE,
    _build_outcome_context,
    _build_system_signal_context,
    _channel_config_by_key,
    _deliver_signal_group,
    _deliver_signal_outcome,
    _format_weekly_recap_post,
    _mark_delivered_signals,
    _mark_signal_group_completed,
    _persist_signal_detector_cursors,
    _post_signal_memory,
    _post_system_signal_updates,
    _preauthored_system_signal_result,
    _progression_signal_batches,
    _publish_pending_system_signal_updates,
    _signal_group_needs_recap_memory,
    _store_recap_memories_for_signal_batch,
    _strip_weekly_recap_header,
    _system_signal_updates,
)
from runtime.jobs._site import (  # noqa: E402,F401
    SITE_CONTENT_HOUR,
    SITE_DATA_HOUR,
    _commit_site_content_or_raise,
    _normalize_poap_kings_publish_result,
    _notify_poapkings_publish,
    _poapkings_publish_context,
    _poapkings_publish_fallback,
    _promotion_channel_posts,
    _promotion_content_cycle,
    _promotion_discord_required_text,
    _promotion_reddit_required_token,
    _publish_poap_kings_site_or_raise,
    _site_content_cycle,
    _site_data_refresh,
    _unwrap_outer_bold,
    _validate_promote_content_or_raise,
    _write_site_content_or_raise,
)
from runtime.jobs._tournament import (  # noqa: E402,F401
    TOURNAMENT_BATTLE_LOG_SPACING_SECONDS,
    TOURNAMENT_POLL_MINUTES,
    _TOURNAMENT_JOB_ID,
    _tournament_recap,
    _tournament_watch_tick,
    start_tournament_watch,
    stop_tournament_watch,
)

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
        cleared_stale_jobs = await asyncio.to_thread(runtime_status.clear_stale_running_jobs)
        if cleared_stale_jobs:
            log.warning(
                "Cleared stale runtime job running state after restart: %s",
                ", ".join(sorted(cleared_stale_jobs)),
            )
        # AsyncIOScheduler awaits coroutine jobs on the bot's running event
        # loop, so register the tick coroutines directly. The old
        # call_soon_threadsafe shim was a BackgroundScheduler-era holdover that
        # returned instantly — APScheduler only ever saw the shim, so each
        # job's max_instances/coalesce guard applied to a no-op while the real
        # coroutine ran detached and could overlap itself.
        register_scheduled_activities(
            scheduler=scheduler,
            runtime_module=sys.modules[__name__],
            create_task=lambda job_callable: job_callable,
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
        try:
            from runtime.leader_action_ui import restore_leader_action_views
            await restore_leader_action_views(bot)
        except Exception as exc:
            log.warning("Leader action view restore failed: %s", exc)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #welcome."""
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


def main():
    return _process_service.main(TOKEN, bot)
