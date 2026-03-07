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
from runtime import status as runtime_status

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("elixir")

CHICAGO = pytz.timezone("America/Chicago")
TOKEN = os.getenv("DISCORD_TOKEN")
_dc = prompts.discord_config()
MEMBER_ROLE_ID = _dc.get("member_role", 0)
LEADER_ROLE_ID = _dc.get("leader_role", 0)
BOT_ROLE_ID = _dc.get("bot_role", 0)
POAPKINGS_REPO = os.path.expanduser(os.getenv("POAPKINGS_REPO_PATH", "../poapkings.com"))
CLANOPS_PROACTIVE_COOLDOWN_SECONDS = int(os.getenv("CLANOPS_PROACTIVE_COOLDOWN_SECONDS", "900"))
CHANNEL_CONVERSATION_LIMIT = 20

# Active hours for the heartbeat (Chicago time). Outside this window, heartbeat is skipped.
HEARTBEAT_START_HOUR = int(os.getenv("HEARTBEAT_START_HOUR", "7"))
HEARTBEAT_END_HOUR = int(os.getenv("HEARTBEAT_END_HOUR", "22"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=CHICAGO)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _post_to_elixir(channel, entry: dict):
    """Post an entry's content to a configured Discord channel."""
    content = entry.get("content", entry.get("summary", ""))
    if not content:
        return
    if len(content) > 2000:
        for chunk in [content[i:i+1990] for i in range(0, len(content), 1990)]:
            await channel.send(chunk)
    else:
        await channel.send(content)


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

@bot.event
async def on_ready():
    log.info("Elixir online as %s", bot.user)
    prompts.ensure_valid_discord_channel_config()
    if not scheduler.running:
        # Single hourly heartbeat replaces both the 4x/day observations and hourly member check
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_heartbeat_tick())
            ),
            "interval",
            hours=1,
            id="heartbeat",
        )
        # Morning data refresh for poapkings.com
        scheduler.add_job(
            lambda: bot.loop.call_soon_threadsafe(
                lambda: bot.loop.create_task(_site_data_refresh())
            ),
            "cron",
            hour=SITE_DATA_HOUR,
            minute=0,
            id="site_data_refresh",
        )
        # Evening content cycle for poapkings.com
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
            hours=PLAYER_INTEL_REFRESH_HOURS,
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
        scheduler.start()
        log.info("Scheduler started — hourly heartbeat (active %dam-%dpm Chicago), "
                 "site data refresh at %dam, content cycle at %dpm, player intel refresh every %dh, clanops review %s at %02d:00",
                 HEARTBEAT_START_HOUR, HEARTBEAT_END_HOUR,
                 SITE_DATA_HOUR, SITE_CONTENT_HOUR, PLAYER_INTEL_REFRESH_HOURS,
                 CLANOPS_WEEKLY_REVIEW_DAY, CLANOPS_WEEKLY_REVIEW_HOUR)
    else:
        log.info("Reconnected — scheduler already running, skipping re-init")


@bot.event
async def on_member_join(member):
    """Welcome new Discord members in #reception."""
    await asyncio.to_thread(
        db.upsert_discord_user,
        member.id,
        username=member.name,
        global_name=getattr(member, "global_name", None),
        display_name=member.display_name,
    )
    channel = bot.get_channel(_get_singleton_channel_id("onboarding"))
    if not channel:
        return
    msg = await asyncio.to_thread(
        elixir_agent.generate_message,
        "discord_member_join",
        f"A new user '{member.display_name}' ({member.mention}) just joined the Discord server. "
        f"Welcome them in #reception and explain how to set their server nickname "
        f"to match their Clash Royale in-game name to get verified.",
    )
    if msg:
        await channel.send(msg)
    else:
        await channel.send(
            f"Welcome to the server, {member.mention}! Set your server nickname "
            f"to your Clash Royale name and I'll get you verified."
        )


@bot.event
async def on_member_update(before, after):
    """Detect nickname changes and grant member role when name matches a clan member."""
    if before.nick == after.nick:
        return
    if not after.nick:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        after.id,
        username=after.name,
        global_name=getattr(after, "global_name", None),
        display_name=after.display_name,
    )

    # Only act if they don't already have the member role
    if not MEMBER_ROLE_ID:
        return
    member_role = after.guild.get_role(MEMBER_ROLE_ID)
    if not member_role or member_role in after.roles:
        return

    match = await asyncio.to_thread(_match_clan_member, after.nick)
    channel = bot.get_channel(_get_singleton_channel_id("onboarding"))

    if not match:
        if channel:
            msg = await asyncio.to_thread(
                elixir_agent.generate_message,
                "nickname_no_match",
                f"User {after.mention} set their nickname to '{after.nick}' but it doesn't "
                f"match anyone in the clan roster. Let them know and suggest they check "
                f"the spelling or join the clan first. Channel: #reception.",
            )
            await channel.send(msg or f"Hmm {after.mention}, I don't see **{after.nick}** in our roster.")
        return

    tag, cr_name = match
    await asyncio.to_thread(
        db.link_discord_user_to_member,
        after.id,
        tag,
        username=after.name,
        display_name=after.display_name,
        source="verified_nickname_match",
    )
    try:
        await after.add_roles(member_role, reason=f"Matched clan member: {cr_name} ({tag})")
    except discord.Forbidden:
        log.error("Cannot assign member role — check bot permissions and role hierarchy")
        if channel:
            msg = await asyncio.to_thread(
                elixir_agent.generate_message,
                "role_grant_failed",
                f"Matched user {after.mention} to clan member '{cr_name}' ({tag}) but "
                f"couldn't assign the member role due to permissions. Let them know "
                f"a leader will help. Channel: #reception.",
            )
            await channel.send(msg or f"I matched **{cr_name}** but couldn't assign the role.")
        return

    if channel:
        msg = await asyncio.to_thread(
            elixir_agent.generate_message,
            "nickname_matched",
            f"User {after.mention} set their nickname to '{cr_name}' which matches "
            f"clan member tag {tag}. They've been granted the member role. "
            f"Welcome them and let them know they have full access. Channel: #reception.",
        )
        await channel.send(msg or f"Welcome aboard, {cr_name}! You now have full access.")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await asyncio.to_thread(
        db.upsert_discord_user,
        message.author.id,
        username=message.author.name,
        global_name=getattr(message.author, "global_name", None),
        display_name=message.author.display_name,
    )
    channel_config = _get_channel_behavior(message.channel.id)
    mentioned = _is_bot_mentioned(message)
    if not channel_config:
        await bot.process_commands(message)
        return

    role = channel_config.get("role")
    workflow = channel_config.get("workflow")
    scope = _channel_scope(message.channel)
    conversation_scope = _channel_conversation_scope(message.channel, message.author.id)
    raw_question = _strip_bot_mentions(message.content) if mentioned else message.content.strip()

    # Non-responsive singleton channels are outbound only.
    if not channel_config.get("respond_allowed", True):
        return

    if role == "onboarding" and not mentioned:
        return

    clan_status_mode = _clan_status_mode(raw_question)

    if role in {"clanops", "interactive"} and _is_help_request(raw_question):
        log.info(
            "message_route route=help channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        help_content = await asyncio.to_thread(_build_help_report, role)
        workflow_name = "clanops" if role == "clanops" else workflow
        event_type = f"{role}_help"
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow_name,
            discord_message_id=message.id,
        )
        await _reply_text(message, help_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            help_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow_name,
            event_type=event_type,
        )
        return

    if role in {"clanops", "interactive"} and _is_roster_join_dates_request(raw_question):
        log.info(
            "message_route route=roster_join_dates_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        roster_content = await asyncio.to_thread(_build_roster_join_dates_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, roster_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            roster_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="roster_join_dates_report",
        )
        return

    deck_target = None
    if role in {"clanops", "interactive"} and _is_member_deck_request(raw_question):
        deck_target = await asyncio.to_thread(_extract_member_deck_target, raw_question, message)
    if role in {"clanops", "interactive"} and deck_target:
        log.info(
            "message_route route=member_deck_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s deck_target=%r raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            deck_target,
            raw_question,
            message.content,
        )
        deck_content = await asyncio.to_thread(_build_member_deck_report, deck_target)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, deck_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            deck_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="member_deck_report",
        )
        return

    if role == "clanops" and _is_kick_risk_request(raw_question):
        log.info(
            "message_route route=kick_risk_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        kick_risk_content = await asyncio.to_thread(_build_kick_risk_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, kick_risk_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            kick_risk_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="kick_risk_report",
        )
        return

    if role == "clanops" and _is_top_war_contributors_request(raw_question):
        log.info(
            "message_route route=top_war_contributors_report channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        top_war_content = await asyncio.to_thread(_build_top_war_contributors_report)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            discord_message_id=message.id,
        )
        await _reply_text(message, top_war_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            top_war_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow=workflow,
            event_type="top_war_contributors_report",
        )
        return

    if role == "clanops" and (_is_status_request(raw_question) or _is_schedule_request(raw_question) or clan_status_mode):
        route = (
            "clan_status_report" if clan_status_mode == "full"
            else "clan_status_short_report" if clan_status_mode == "short"
            else "schedule_report" if _is_schedule_request(raw_question)
            else "status_report"
        )
        log.info(
            "message_route route=%s channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s raw_question=%r original=%r",
            route,
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            raw_question,
            message.content,
        )
        clan = {}
        war = {}
        if clan_status_mode:
            try:
                clan, war = await _load_live_clan_context()
            except Exception as exc:
                log.warning("Clan status refresh failed: %s", exc)
        if clan_status_mode == "full":
            report_builder = _build_clan_status_report
            report_args = (clan, war)
            event_type = "clan_status_report"
        elif clan_status_mode == "short":
            report_builder = _build_clan_status_short_report
            report_args = (clan, war)
            event_type = "clan_status_short_report"
        elif _is_schedule_request(raw_question):
            report_builder = _build_schedule_report
            report_args = ()
            event_type = "schedule_report"
        else:
            report_builder = _build_status_report
            report_args = ()
            event_type = "status_report"
        status_content = await asyncio.to_thread(report_builder, *report_args)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            raw_question,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        await _reply_text(message, status_content)
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "assistant",
            status_content,
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            event_type=event_type,
        )
        return

    proactive = role == "clanops" and not mentioned
    if proactive and not _clanops_cooldown_elapsed(message.channel.id):
        await asyncio.to_thread(
            db.save_message,
            conversation_scope,
            "user",
            message.content.strip(),
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", None),
            channel_kind=str(message.channel.type),
            discord_user_id=message.author.id,
            username=message.author.name,
            display_name=message.author.display_name,
            workflow="clanops",
            discord_message_id=message.id,
        )
        return

    if not mentioned and not proactive:
        await bot.process_commands(message)
        return

    if role == "onboarding":
        async with message.channel.typing():
            try:
                clan = await asyncio.to_thread(cr_api.get_clan)
                question = raw_question
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )
                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow="reception",
                    discord_message_id=message.id,
                )
                result = await asyncio.to_thread(
                    elixir_agent.respond_in_reception,
                    question=question,
                    author_name=message.author.display_name,
                    clan_data=clan,
                    memory_context=memory_context,
                )
                if result is None:
                    _log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="agent_none",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                if not isinstance(result, dict):
                    _log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=_preview_text(result),
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                content = result.get("content", result.get("summary", ""))
                if not content:
                    _log_prompt_failure(
                        question=question,
                        workflow="reception",
                        failure_type="empty_result",
                        failure_stage="respond_in_reception",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=_preview_text(result),
                        raw_json=result,
                    )
                    await message.reply("Having a hiccup. Try again in a sec.")
                    return
                await _reply_text(message, content)
                await asyncio.to_thread(
                    db.save_message,
                    scope,
                    "assistant",
                    content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    workflow="reception",
                    event_type=result.get("event_type"),
                )
            except Exception as e:
                log.error("reception error: %s", e)
                _log_prompt_failure(
                    question=raw_question,
                    workflow="reception",
                    failure_type="exception",
                    failure_stage="on_message_reception",
                    channel=message.channel,
                    author=message.author,
                    discord_message_id=message.id,
                    detail=str(e),
                )
                await message.reply("Hit an error. Try again in a moment.")
        return

    if workflow in {"interactive", "clanops"}:
        log.info(
            "message_route route=channel_llm channel_id=%s author_id=%s mentioned=%s role=%s workflow=%s proactive=%s raw_question=%r original=%r",
            message.channel.id,
            message.author.id,
            mentioned,
            role,
            workflow,
            proactive,
            raw_question,
            message.content,
        )
        async with message.channel.typing():
            try:
                clan, war = await _load_live_clan_context()
                question = raw_question
                conversation_history = await asyncio.to_thread(
                    db.list_thread_messages,
                    conversation_scope,
                    CHANNEL_CONVERSATION_LIMIT,
                )
                memory_context = await asyncio.to_thread(
                    db.build_memory_context,
                    discord_user_id=message.author.id,
                    channel_id=message.channel.id,
                )

                await asyncio.to_thread(
                    db.save_message,
                    conversation_scope,
                    "user",
                    question,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                        workflow=workflow,
                        discord_message_id=message.id,
                    )

                result = await asyncio.to_thread(
                    elixir_agent.respond_in_channel,
                    question=question,
                    author_name=message.author.display_name,
                    channel_name=_channel_reply_target_name(channel_config),
                    workflow=workflow,
                    clan_data=clan,
                    war_data=war,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                    proactive=proactive,
                )
                if result is None:
                    _log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="agent_none",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                    )
                    if mentioned:
                        await message.reply(_fallback_channel_response(raw_question, workflow))
                    return
                if not isinstance(result, dict):
                    log.error("%s channel error: invalid result type %s", workflow, type(result).__name__)
                    _log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="invalid_result_type",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        detail=type(result).__name__,
                        result_preview=_preview_text(result),
                    )
                    if mentioned:
                        await message.reply(_fallback_channel_response(raw_question, workflow))
                    return

                content = result.get("content", result.get("summary", ""))
                if not content:
                    log.error("%s channel error: empty result payload %s", workflow, result)
                    _log_prompt_failure(
                        question=raw_question,
                        workflow=workflow,
                        failure_type="empty_result",
                        failure_stage="respond_in_channel",
                        channel=message.channel,
                        author=message.author,
                        discord_message_id=message.id,
                        result_preview=_preview_text(result),
                        raw_json=result,
                    )
                    if mentioned:
                        await message.reply(_fallback_channel_response(raw_question, workflow))
                    return
                await _share_channel_result(result, workflow)

                await asyncio.to_thread(
                        db.save_message,
                        conversation_scope,
                        "assistant",
                        content,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", None),
                    channel_kind=str(message.channel.type),
                    discord_user_id=message.author.id,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    workflow=workflow,
                    event_type=result.get("event_type"),
                )

                await _reply_text(message, content)
            except Exception as e:
                log.error("%s channel error: %s", workflow, e)
                _log_prompt_failure(
                    question=raw_question,
                    workflow=workflow,
                    failure_type="exception",
                    failure_stage="on_message_channel",
                    channel=message.channel,
                    author=message.author,
                    discord_message_id=message.id,
                    detail=str(e),
                )
                if mentioned:
                    await message.reply("Hit an error. Try again in a moment.")
        return

    await bot.process_commands(message)


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
