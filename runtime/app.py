"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import discord
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands
from dotenv import load_dotenv

import cr_api  # re-exported; accessed by runtime submodules
import db
import elixir_agent
import prompts
from runtime import onboarding, prompt_feedback
from runtime import process as _process_service
from runtime import status as runtime_status
from runtime.activities import (
    AWARENESS_LOOP_HOURS_DEFAULT,
    format_scheduler_startup_summary,
    register_scheduled_activities,
)
from runtime.admin import admin_command_requires_leader, dispatch_admin_command
from runtime.channel_router import route_message
from runtime.discord_commands import register_elixir_app_commands
from runtime.emoji import sync_emoji
from runtime.leader_action_policy import can_post_leader_action
from runtime.system_signals import seed_startup_state

load_dotenv()

# Console only at import; runtime.logging_setup.configure_logging() adds the
# rotating main/error files from main(), so importing this module in a test does
# not create log files.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
# Quiet noisy third-party loggers so operational signals stay readable.
# discord.py installs its own handler via utils.setup_logging() in client.run();
# we pass log_handler=None below to suppress it, and clear any handlers it may
# have attached at import time so messages don't double-print.
for _noisy in (
    "apscheduler",
    "apscheduler.scheduler",
    "apscheduler.executors.default",
    "httpx",
):
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
CHANNEL_CONVERSATION_LIMIT = 20

# v5.1 engine schedule knobs (runtime.md §3; ratified defaults 2026-07-03)
ENGINE_TICK_MINUTES = int(os.getenv("ENGINE_TICK_MINUTES", "10"))
WEEKLY_REVIEW_DAY = os.getenv("WEEKLY_REVIEW_DAY", "mon")
WEEKLY_REVIEW_HOUR = int(os.getenv("WEEKLY_REVIEW_HOUR", "7"))
WEEKLY_REVIEW_MINUTE = int(os.getenv("WEEKLY_REVIEW_MINUTE", "0"))
WAR_ATTENDANCE_HOUR = int(os.getenv("WAR_ATTENDANCE_HOUR", "4"))
WAR_ATTENDANCE_MINUTE = int(os.getenv("WAR_ATTENDANCE_MINUTE", "15"))
ACTION_OUTCOME_REFRESH_HOUR = int(os.getenv("ACTION_OUTCOME_REFRESH_HOUR", "9"))
ACTION_OUTCOME_REFRESH_MINUTE = int(os.getenv("ACTION_OUTCOME_REFRESH_MINUTE", "30"))
ASK_ELIXIR_DAILY_INSIGHT_HOUR = int(os.getenv("ASK_ELIXIR_DAILY_INSIGHT_HOUR", "12"))
# The awareness loop runs on a wall-clock cron (deterministic across restarts).
# AWARENESS_LOOP_HOURS is an APScheduler cron hour expression ("*/3" = every 3
# hours: 00:05, 03:05, ... CT); AWARENESS_LOOP_MINUTE is the minute past the hour.
# :05 lands just after the top-of-hour engine tick, so the brain reads freshly-
# refreshed state. Cadence widened hourly -> 3h on 2026-07-23 for cost: the loop
# is an always-on Sonnet agentic turn, and hourly was more attentiveness than the
# clan needs. Hard-posts now wait up to 3h (was <=1h). Revert via env if too slow.
AWARENESS_LOOP_MINUTE = int(os.getenv("AWARENESS_LOOP_MINUTE", "5"))
AWARENESS_LOOP_HOURS = os.getenv("AWARENESS_LOOP_HOURS", AWARENESS_LOOP_HOURS_DEFAULT)
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
        except TypeError, ValueError:
            text = repr(value)
    return text[:limit]


def _json_trace_text(value):
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except TypeError, ValueError:
        return json.dumps({"repr": repr(value)}, ensure_ascii=False)


def _normalize_prompt_failure_question(question):
    text = (question or "").strip()
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"<@&\d+>", " ", text)
    return " ".join(text.split())


def _log_prompt_failure(
    *,
    question,
    workflow,
    failure_type,
    failure_stage,
    channel,
    author,
    discord_message_id=None,
    detail=None,
    result_preview=None,
    raw_json=None,
):
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
        log.exception("prompt failure logging error: %s", exc)


# ── The `elixir` runtime surface ─────────────────────────────────────────────
# This module doubles as the top-level `elixir` module (see elixir.py), and
# scheduling (runtime.activities resolves job functions and config constants
# by name on this module), other runtime modules, and the test suite all
# address helpers and jobs through it. These imports ARE that surface — they
# replaced a dynamic __export_public copy loop, so keep them explicit.

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
    alert_discord_post_failure,
    clear_discord_post_failure_alert,
    schedule_llm_failure_alert,
)
from runtime.discord_posting import (  # noqa: E402,F401
    _chunk_discord_text,
    _entry_posts,
    _normalize_entry_posts,
    _post_to_elixir,
    _resolve_custom_emoji,
)
from runtime.helpers import (  # noqa: E402,F401  # noqa: E402,F401
    _DB_STATUS_MEMORY_TABLES,
    _WEEKLY_RECAP_HEADER_RE,
    DISCORD_CHUNK_SIZE,
    DISCORD_MAX_MESSAGE_LEN,
    _author_msg_kwargs,
    _bare_tag,
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
    _channel_config_by_key,
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
    _format_weekly_recap_post,
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
    _strip_weekly_recap_header,
    _with_leader_ping,
)
from runtime.jobs._battle_intel import (  # noqa: E402,F401
    _battle_intel_stage_a,
    _battle_intel_stage_b,
)
from runtime.jobs._core import (  # noqa: E402,F401
    WEEKLY_DISCORD_INVITE_RELAY_DAY,
    WEEKLY_DISCORD_INVITE_RELAY_HOUR,
    WEEKLY_MEMBER_REPORT_DAY,
    WEEKLY_MEMBER_REPORT_HOUR,
    WEEKLY_RECAP_DAY,
    WEEKLY_RECAP_HOUR,
    _ask_elixir_daily_insight,
    _build_ask_elixir_daily_insight_context,
    _query_or_default,
    _summarize_member_rows,
    _weekly_clan_recap,
    _weekly_discord_invite_relay,
    _weekly_elder_standing,
    _weekly_member_report_cycle,
)
from runtime.jobs._intel import (  # noqa: E402,F401
    _clan_wars_intel_report,
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
    MEMORY_SYNTHESIS_HOUR,
    MEMORY_SYNTHESIS_POSTS_PER_CHANNEL,
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
    _memory_synthesis_cycle,
)
from runtime.jobs._promotion import (  # noqa: E402,F401
    _promotion_channel_posts,
    _promotion_content_cycle,
    _promotion_discord_required_text,
    _promotion_reddit_required_token,
    _unwrap_outer_bold,
    _validate_promote_content_or_raise,
)
from runtime.jobs._tournament import (  # noqa: E402,F401
    _TOURNAMENT_JOB_ID,
    TOURNAMENT_BATTLE_LOG_SPACING_SECONDS,
    TOURNAMENT_POLL_MINUTES,
    _tournament_recap,
    _tournament_watch_tick,
    start_tournament_watch,
    stop_tournament_watch,
)
from runtime.leader_action_ui import (  # noqa: E402,F401
    CLASH_COPY_MAX_LENGTH,
    LEADER_ACTION_UI_VERSION,
    post_leader_action_card,
)
from runtime.startup import (  # noqa: E402,F401
    _member_role_grant_status,
    _post_startup_message,
    _resolve_runtime_channel,
    _startup_channel_audit_summary,
)

register_elixir_app_commands(bot)


async def _engine_send(channel_id: int, text: str, image_url: str | None = None):
    """Async Discord send for the engine; returns the first message id or None.
    When image_url is set (game-level card/badge announcements), post the copy
    with the art as an embed image in one message."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        log.warning("engine: channel %s not found; send failed", channel_id)
        return None
    if image_url:
        import discord

        from runtime.discord_posting import _resolve_custom_emoji

        guild = getattr(channel, "guild", None)
        content = _resolve_custom_emoji(text, guild)
        embed = discord.Embed()
        embed.set_image(url=image_url)
        try:
            msg = await channel.send(content=content, embed=embed)
            return getattr(msg, "id", None)
        except Exception:
            log.exception(
                "engine: embed send failed for channel %s; text-only fallback",
                channel_id,
            )
            # fall through to a plain text post so the announcement still lands
    sent_messages = await _post_to_elixir(channel, {"content": text})
    if not sent_messages:
        return None
    return getattr(sent_messages[0], "id", None)


def _engine_startup_cursors() -> int:
    """runtime.md §6: a missing consumer cursor initializes at the current
    stream head — replay is safe (durable ledger) but wasteful, so skip it."""
    from engine import db as engine_db

    consumers = {
        "recognize:battle": "battle_events",
        "recognize:player": "player_events",
        "recognize:clan": "clan_events",
        "recognize:war": "war_events",
        "recognize:game": "game_events",
    }
    conn = engine_db.connect()
    initialized = 0
    try:
        for consumer_key, table in consumers.items():
            row = conn.execute(
                "SELECT 1 FROM stream_cursors WHERE consumer_key = ? AND scope_key = ''",
                (consumer_key,),
            ).fetchone()
            if row is None:
                engine_db.cursor_set(conn, consumer_key, engine_db.stream_head(conn, table))
                initialized += 1
        conn.commit()
    finally:
        conn.close()
    return initialized


_LEADER_CARD_SURFACE = "#actions (leader-action cards)"


async def _post_pending_leader_action_cards(limit: int = 4) -> int:
    """Post interactive cards for engine-created leader actions that have no
    Discord message yet (kick recs from the tick, promote/demote from the
    weekly review). Gated by the existing post policy."""
    rows = await asyncio.to_thread(db.list_leader_actions, status="proposed", limit=25)
    pending = [a for a in rows if not a.get("source_message_id")]
    if not pending:
        return 0
    allowed, reason = await asyncio.to_thread(can_post_leader_action, critical=False)
    if not allowed:
        log.info("engine leader-action cards deferred by policy: %s", reason)
        return 0
    try:
        channel_config = _channel_config_by_key("actions")
    except Exception:
        # ERROR, not warning: this returns 0 having posted nothing, so a card a
        # leader is owed never reaches #actions. That is the 2026-07-18 outage
        # class — the bot looked healthy while leadership silently got nothing.
        log.error("engine leader-action cards skipped: actions unavailable", exc_info=True)
        return 0
    relay_channel = bot.get_channel(int(channel_config["id"]))
    if relay_channel is None:
        return 0
    posted = 0
    for action in pending[:limit]:
        # Mark BEFORE posting (cold review #7): a failure after the Discord
        # post but before the id write must not double-post next tick. The
        # sentinel is cleared ONLY when we know the post never happened;
        # otherwise it sticks — at-most-once is the right direction for cards
        # (the action row itself stays visible in DB + Observatory).
        post_attempted = False
        try:
            await asyncio.to_thread(
                lambda a=action: db.update_leader_action_message(
                    a["action_id"], source_message_id=db.POSTING_SENTINEL
                )
            )
            post_attempted = True
            action = await _ensure_role_action_clan_chat_copy(action)
            card_messages = await post_leader_action_card(relay_channel, action)
            first_id = getattr(card_messages[0], "id", None) if card_messages else None
            if first_id is not None:
                await asyncio.to_thread(
                    lambda a=action, m=first_id: db.update_leader_action_message(
                        a["action_id"], source_message_id=str(m)
                    )
                )
            posted += 1
            target = action.get("target_player_name") or action.get("target_player_tag")
            await _ops_log(
                f"📋 Surfaced R{action.get('action_id')} `{action.get('action_type')}` in #actions"
                + (f" — **{target}**." if target else ".")
            )
        except discord.Forbidden:
            # A 403 is UNAMBIGUOUS: Discord rejected the post, so it definitively
            # did not land — clear the sentinel so the next tick retries once the
            # missing permission is restored, rather than stranding the card at
            # 'posting' forever (the 2026-07-18 #actions outage failure mode).
            log.exception(
                "engine leader-action card post forbidden for %s", action.get("action_id")
            )
            await _clear_posting_sentinel(action)
            await alert_discord_post_failure(
                _LEADER_CARD_SURFACE,
                f"403 Forbidden posting R{action.get('action_id')} `{action.get('action_type')}`.",
            )
        except Exception:
            log.exception("engine leader-action card post failed for %s", action.get("action_id"))
            if not post_attempted:  # sentinel write itself failed pre-post — safe to retry
                await _clear_posting_sentinel(action)
    if posted:
        # A successful post proves the permission is back — reset the dedup so a
        # future revocation alerts again instead of being silently suppressed.
        clear_discord_post_failure_alert(_LEADER_CARD_SURFACE)
    return posted


async def _clear_posting_sentinel(action: dict) -> None:
    try:
        await asyncio.to_thread(
            lambda a=action: db.clear_leader_action_source_message(a["action_id"])
        )
    except Exception:
        log.debug("sentinel clear failed for %s", action.get("action_id"), exc_info=True)


def _member_participation_facts(tag: str | None) -> str:
    """Member-safe contribution facts for copy, or "" if unavailable."""
    if not tag:
        return ""
    conn = None
    try:
        from engine.management import member_participation_facts

        conn = db.get_connection()
        return member_participation_facts(conn, tag)
    except Exception:
        log.warning("participation facts lookup failed for %s", tag, exc_info=True)
        return ""
    finally:
        if conn is not None:
            conn.close()


async def _ensure_role_action_clan_chat_copy(action: dict) -> dict:
    """Every promotion/demotion/kick card carries an in-game clan-chat message
    explaining the WHY (Jamie 2026-07-05: the clan must know why these actions
    happen). Generated at post time so it covers ALL sources — the weekly
    review, the reactive kick path, and manual leadership cards alike (none of
    which set copy at creation). LLM-composed from the rationale with a
    deterministic role-action fallback; never blocks the card."""
    from runtime.clan_chat_copy import (
        ROLE_ACTION_TYPES,
        generate_clan_chat_copy,
        member_facing_role_reason,
        role_action_clan_chat_copy,
    )
    from runtime.leader_action_ui import CLASH_COPY_MAX_LENGTH

    atype = action.get("action_type")
    if atype not in ROLE_ACTION_TYPES:
        return action
    if action.get("copy_current_text") or action.get("copy_original_text"):
        return action  # already has copy (e.g. a re-post)

    name = action.get("target_player_name")
    if not name:
        # Never leak a raw player tag into member-facing copy: resolve the
        # display name from the roster when the card didn't carry one (legacy
        # rows / sources that skip target_player_name). Falls back to a generic
        # noun, never the #TAG.
        tag = action.get("target_player_tag")
        if tag:
            conn = None
            try:
                conn = db.get_connection()
                row = conn.execute(
                    "SELECT display_name, current_name FROM players WHERE player_tag = ?",
                    (tag,),
                ).fetchone()
                if row:
                    name = row["display_name"] or row["current_name"]
            except Exception:
                log.warning("copy name lookup failed for %s", tag, exc_info=True)
            finally:
                if conn is not None:
                    conn.close()
    name = name or "this member"
    rationale = action.get("rationale") or ""
    fallback = role_action_clan_chat_copy(
        action_type=atype,
        target_player_name=name,
        rationale=rationale,
        max_chars=CLASH_COPY_MAX_LENGTH,
    )
    copy_text = fallback
    try:
        # The rationale is written FOR LEADERS (score breakdown, standings rank,
        # elder band). Asking the model to "summarize this reasoning" pulled all
        # of that into member-facing copy — R213-R217 cited board rank, and R216
        # publicly told a demoted member they had "slipped to 12th of 39". Give
        # the model the contribution, not the scoring, and say what to recognize.
        member_reason = member_facing_role_reason(rationale, atype)
        # Real participation facts, not just the scoring summary. Without these
        # the composer had nothing concrete about the member and fell back to
        # restating the leadership maths. Same vocabulary as the public weekly
        # Elder Standing, so the two surfaces can't describe someone differently.
        facts = await asyncio.to_thread(
            _member_participation_facts, action.get("target_player_tag")
        )
        context = (
            f"Tell the POAP KINGS clan, in in-game clan chat, that this is happening and "
            f"recognize what this member has contributed. Action: "
            f"{atype.replace('_', ' ')} for {name}."
            + (f" What stood out: {member_reason}." if member_reason else "")
            + (f" Their actual contribution: {facts}." if facts else "")
            + " Speak to the impact they have had on the clan — the war days they showed up "
            "for, the battles, the donations. You may quote those concrete numbers. Never "
            "mention their position in any ranking or standings, never a score or number out "
            "of the roster, and never the elder slot count or band. Warm, brief, specific."
        )
        result = await generate_clan_chat_copy(
            intent=f"role_action_{atype}",
            context=context,
            max_messages=1,
            max_chars=CLASH_COPY_MAX_LENGTH,
            forbidden_terms=("http://", "https://", "www.", "Discord"),
            fallback_messages=[fallback] if fallback else None,
        )
        if result and getattr(result, "messages", None):
            copy_text = result.messages[0]
    except Exception:
        log.warning(
            "role-action clan-chat copy generation failed for %s",
            action.get("action_id"),
            exc_info=True,
        )

    if copy_text:

        def _persist(a_id=action["action_id"], text=copy_text):
            conn = db.get_connection()
            try:
                conn.execute(
                    "UPDATE leader_action_recommendations "
                    "SET copy_original_text = ?, copy_current_text = ? WHERE action_id = ?",
                    (text, text, a_id),
                )
                conn.commit()
            finally:
                conn.close()

        await asyncio.to_thread(_persist)
        action = {
            **action,
            "copy_original_text": copy_text,
            "copy_current_text": copy_text,
        }
    return action


async def _engine_tick():
    """The v5.1 data tick: poll → mirror → emit → project → manage."""
    import cr_api as _cr_api
    from engine import db as engine_db
    from engine import tick as engine_tick_mod
    from runtime import lanes as engine_compose

    runtime_status.mark_job_start("engine_tick")

    def _run():
        conn = engine_db.connect()
        try:
            return engine_tick_mod.run_tick(conn, api=_cr_api)
        finally:
            conn.close()

    try:
        counters = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("engine_tick", str(exc))
        log.exception("engine tick failed")
        return
    try:
        cards = await _post_pending_leader_action_cards()
        if cards:
            counters["leader_action_cards"] = cards
        # A tournament the clan is already playing should not need a human to
        # type `/tournament watch`. Elixir ingests these battles anyway --
        # `type: "tournament"` with a `tournamentTag` -- so it can start the
        # watch itself. On 2026-07-24 five members played #2JVLYQR9 for 100
        # minutes, Elixir saw the first battle within ~20, and nothing happened.
        from runtime.jobs import maybe_autowatch_tournament

        auto = await maybe_autowatch_tournament()
        if auto:
            counters["tournament_autowatch"] = auto
    except Exception:
        log.exception("engine tick post-steps failed")
    log.info("engine tick: %s", counters)
    try:  # Observatory tick history (in-memory ring; never fails the tick)
        from runtime.webapp import ticks as webapp_ticks

        webapp_ticks.record_tick(dict(counters))
    except Exception:
        log.debug("webapp tick recording failed", exc_info=True)
    runtime_status.mark_job_success("engine_tick", json.dumps(counters, default=str)[:900])
    return counters


async def _weekly_leadership_review():
    """Q1's weekly batch half (Monday 7:00 AM America/Chicago, ratified):
    roll the management week, raise promote/demote leader actions, post one
    review summary to the leadership lane."""
    from engine import db as engine_db
    from engine import management as engine_management

    runtime_status.mark_job_start("weekly_leadership_review")

    def _run():
        import pytz as _pytz

        conn = engine_db.connect()
        try:
            chicago_now = datetime.now(_pytz.timezone("America/Chicago"))
            monday = chicago_now.date() - timedelta(days=chicago_now.weekday())
            result = engine_management.run_weekly_review(conn, monday.isoformat())
            from storage.leader_actions import (
                auto_withdraw_leader_actions,
                create_leader_action_recommendation,
            )

            for kind, action_type in (
                ("promote_eligible", "promotion_recommendation"),
                ("demote_eligible", "demotion_recommendation"),
            ):
                for row in result.get(kind) or []:
                    if isinstance(row, str):
                        row = {"player_tag": row}
                    tag = row.get("player_tag")
                    if not tag:
                        continue
                    create_leader_action_recommendation(
                        action_type=action_type,
                        objective=f"{action_type.replace('_', ' ')} for {row.get('player_name') or tag}",
                        prompt_text=(
                            f"Weekly review: {tag} is {kind.replace('_', ' ')} "
                            f"per the management evaluators. Evidence: {json.dumps(row, default=str)[:600]}"
                        ),
                        rationale=row.get("rationale") or "Weekly management review candidacy.",
                        target_player_tag=tag,
                        target_player_name=row.get("player_name"),
                        source_signal_key=f"engine:weekly-review:{monday.isoformat()}:{tag}:{action_type}",
                        source_signal_type="engine_weekly_review",
                        conn=conn,
                    )
            withdrawn_actions = 0
            for item in result.get("withdrawn") or []:
                if not isinstance(item, dict):
                    continue
                action_type = {
                    "promote": "promotion_recommendation",
                    "demote": "demotion_recommendation",
                    "kick": "kick_recommendation",
                }.get(item.get("kind"))
                if not action_type or not item.get("player_tag"):
                    continue
                withdrawn_actions += auto_withdraw_leader_actions(
                    action_type=action_type,
                    target_player_tag=item.get("player_tag"),
                    reason=(
                        f"Auto-withdrawn by weekly leadership review: "
                        f"{item.get('kind')} candidacy no longer meets its gate."
                    ),
                    conn=conn,
                )
            result["withdrawn_actions"] = withdrawn_actions
            conn.commit()
            return result
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_leadership_review", str(exc))
        log.exception("weekly leadership review failed")
        return
    # The weekly review now speaks only through action cards — the promote/
    # demote candidacy cards raised above and posted here. The old narrative
    # "Weekly Leadership Review" digest was retired (Jamie, 2026-07-06):
    # #actions is cards-only; Elixir sends suggestions, not data dumps.
    try:
        await _post_pending_leader_action_cards(limit=6)
    except Exception:
        log.exception("weekly review card posting failed")
    runtime_status.mark_job_success(
        "weekly_leadership_review",
        f"promote={len(result.get('promote_eligible') or [])} "
        f"demote={len(result.get('demote_eligible') or [])}",
    )
    return result


# The leader-only #thinking channel — Elixir's train of thought lands here as a
# bot-native embed per loop, with the full read/tool-trace/decision in a thread.
# Replaced the old #elixir-log webhook (2026-07-09).
# The ID lives in prompts/DISCORD.md `## Config` with every other channel ID —
# AGENTS.md puts channel config there, and a hardcoded snowflake in code meant
# the channel map did not mention a channel that exists. Env still overrides.
THINKING_CHANNEL_ID = int(
    os.getenv("ELIXIR_THINKING_CHANNEL_ID") or _dc.get("thinking_channel", 0) or 0
)


# Live #thinking session for the in-flight tick. Awareness runs one-at-a-time
# (scheduler max_instances=1), so a single holder is safe; a fresh `start`
# finalizes any stale session first.
_thinking_session: dict = {}


async def _awareness_event(event: dict) -> None:
    """Stream one awareness-tick event to #thinking as it happens: ``start``
    opens the per-loop thread with the read, ``tool`` / ``truncation`` / ``retry``
    append live so the runtime train of thought is visible, and ``end`` finalizes
    the header with the outcome + verbatim decision. Never raises — a diagnostic
    hiccup must not fail the tick; the thought is persisted regardless."""
    import discord

    from runtime.awareness import diagnostic as diag_mod

    channel = bot.get_channel(THINKING_CHANNEL_ID)
    if channel is None:
        log.warning(
            "awareness: #thinking channel %s not found; event dropped",
            THINKING_CHANNEL_ID,
        )
        return
    none = discord.AllowedMentions.none()
    etype = event.get("type")
    try:
        if etype == "start":
            embed = discord.Embed(title="🧠 Awareness — deliberating…", color=0x95A5A6)
            embed.add_field(
                name="Read",
                value=(event.get("read_summary") or "—")[:1024],
                inline=False,
            )
            msg = await channel.send(embed=embed, allowed_mentions=none)
            thread = await msg.create_thread(
                name="Awareness — deliberating…", auto_archive_duration=1440
            )
            _thinking_session.clear()
            _thinking_session.update(message=msg, thread=thread)
            # A successful start proves #thinking perms are back — reset the alert
            # dedup so a future revocation alerts again.
            clear_discord_post_failure_alert("#thinking")
            await thread.send(
                "_Live train of thought — tool calls stream in below._",
                allowed_mentions=none,
            )

        elif etype in ("tool", "truncation", "retry"):
            thread = _thinking_session.get("thread")
            if thread is not None:
                await thread.send(diag_mod.format_live_event(event)[:1900], allowed_mentions=none)

        elif etype == "end":
            render = event.get("render") or {}
            n = event.get("loop_number")
            obs_url = render.get("observatory_url")
            thread = _thinking_session.get("thread")
            msg = _thinking_session.get("message")
            if thread is not None:
                for i, chunk in enumerate(render.get("thread_chunks") or []):
                    total = len(render["thread_chunks"])
                    body = chunk if total == 1 else f"{chunk}\n_({i + 1}/{total})_"
                    await thread.send(body, allowed_mentions=none)
                # Convenience link to the Observatory LLM view (this tick's rounds
                # at the top) — drill into any call for the full prompt + response.
                if obs_url:
                    await thread.send(
                        f"🔍 **Full prompts/responses in the Observatory:** {obs_url}",
                        allowed_mentions=none,
                        suppress_embeds=True,
                    )
                try:
                    await thread.edit(name=render.get("thread_name") or f"Loop #{n}")
                except Exception:
                    log.debug("awareness: #thinking thread rename failed", exc_info=True)
            if msg is not None:
                embed = discord.Embed(
                    title=render.get("header") or f"🧠 Loop #{n}",
                    color=render.get("color"),
                    url=obs_url or None,
                )
                for name, value in (render.get("fields") or {}).items():
                    embed.add_field(name=name, value=(value or "—")[:1024], inline=False)
                if obs_url:
                    embed.add_field(
                        name="Full details",
                        value=f"[Observatory · prompts & responses]({obs_url})",
                        inline=False,
                    )
                try:
                    await msg.edit(embed=embed)
                except Exception:
                    log.debug("awareness: #thinking header edit failed", exc_info=True)
            _thinking_session.clear()
    except discord.Forbidden:
        # Missing permission on #thinking — this 403'd hourly and silently for a
        # full day during the 2026-07-18 outage. Alert #elixir-log (deduped, so
        # one notice, not one per tick).
        log.exception("awareness: #thinking event %s forbidden", etype)
        await alert_discord_post_failure(
            "#thinking", f"403 Forbidden on the #thinking `{etype}` event."
        )
    except Exception:
        log.exception("awareness: #thinking event %s failed", etype)


async def _awareness_loop():
    """The awareness loop (runtime/awareness). Builds the read, runs the brain,
    persists the train of thought, and STREAMS a bot-native #thinking diagnostic.

    The brain is the SOLE proactive poster: it posts its plan to #announcements /
    #elixir and escalates clan-chat-worthy posts as #actions relay cards.
    The engine's proactive delivery is off (see _engine_tick), so there's never a
    gap or a double-post."""
    from runtime.awareness import deliver as deliver_mod
    from runtime.awareness import store as awareness_store
    from runtime.awareness.loop import run_awareness_loop

    runtime_status.mark_job_start("awareness_loop")
    loop = asyncio.get_running_loop()

    def _progress_fn(event):
        # Runs in the worker thread; marshal each live event back to the loop.
        try:
            asyncio.run_coroutine_threadsafe(_awareness_event(event), loop).result(timeout=90)
        except Exception:
            log.exception("awareness: #thinking event bridge failed")

    def _post_fn(channel_id, copy):
        # Worker thread → bot loop, mirroring the engine send_fn.
        fut = asyncio.run_coroutine_threadsafe(_engine_send(int(channel_id), copy), loop)
        return fut.result(timeout=120)

    def _relay_fn(post, channel_name):
        fut = asyncio.run_coroutine_threadsafe(
            _awareness_relay_to_clan_chat(post, channel_name), loop
        )
        return fut.result(timeout=120)

    def _deliver_fn(read, plan):
        return deliver_mod.deliver_posts(
            read,
            plan,
            post_fn=_post_fn,
            record_fn=awareness_store.record_awareness_post,
            relay_fn=_relay_fn,
            repair_fn=elixir_agent.repair_awareness_plan,
            intent_store=awareness_store,
        )

    try:
        counters = await asyncio.to_thread(
            run_awareness_loop,
            progress_fn=_progress_fn,
            deliver_fn=_deliver_fn,
        )
    except Exception as exc:
        runtime_status.mark_job_failure("awareness_loop", str(exc))
        log.exception("awareness loop failed")
        return
    runtime_status.mark_job_success("awareness_loop", json.dumps(counters, default=str))
    log.info("awareness loop (live): %s", counters)
    return counters


async def _awareness_relay_to_clan_chat(post: dict, channel_name: str) -> bool:
    """Deliver a post's in-game clan-chat voicing (``clan_chat``) — the sibling
    copy the brain authored for the in-game surface in the same grounded pass — as
    a card in #actions a leader can paste (clan chat has no post API, so a human is
    the send button). The Discord post has ALREADY landed; this delivers the moment
    to the clan's only everyone-reaches-it surface. Guarded by the leader-action
    post policy (backlog cap + same-objective cooldown) so the brain can't flood the
    board — but NOT the earned-frequency decline throttle, an old-engine artifact
    that shouldn't gate curated brain voicings (throttle_on_decline=False). Never
    raises into delivery — returns False on any guard/failure; the Discord post
    stands regardless."""
    content = post.get("content")
    if isinstance(content, list):
        content = "\n\n".join(str(c) for c in content if c is not None)
    content = (content or "").strip()
    if not content:
        return False

    key_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    objective = f"awareness_relay:{key_hash}"
    action_key = f"awareness-relay:{key_hash}"

    allowed, reason = await asyncio.to_thread(
        can_post_leader_action,
        action_type="in_game_relay",
        objective=objective,
        throttle_on_decline=False,
    )
    if not allowed:
        log.info("awareness relay skipped by policy: %s", reason)
        return False
    try:
        channel_config = _channel_config_by_key("actions")
    except Exception:
        # ERROR: returns False having delivered nothing. A relay the brain
        # decided to send is dropped, and only the log can say so.
        log.error("awareness relay skipped: actions unavailable", exc_info=True)
        return False
    relay_channel = bot.get_channel(int(channel_config["id"]))
    if relay_channel is None:
        log.warning("awareness relay skipped: actions channel not found")
        return False

    from runtime.clan_chat_copy import signed_valid_messages

    # The brain authors the in-game voicing in the SAME grounded pass that wrote
    # the Discord post — a sibling of the post, drawn from the full read, NOT a
    # redraft of it. Accept it only if it clears the deterministic guardrails.
    # There is NO second-LLM redraft: if the voicing is absent or fails the
    # guardrails, this moment simply isn't voiced in-game. (A join can't reach
    # here without a valid voicing — the copy policy fails the tick otherwise.)
    copies = signed_valid_messages(post.get("clan_chat"), max_chars=CLASH_COPY_MAX_LENGTH)
    if not copies:
        if post.get("clan_chat"):
            log.info("awareness clan-chat: voicing missed guardrails; not voicing in-game")
        return False
    # Cap the sequence at 2 so a voicing never becomes a wall of pastes; persist as
    # newline-joined text so it round-trips through _split_copy_messages on edit.
    copies = [c.strip() for c in copies if c and c.strip()][:2]
    if not copies:
        return False
    copy_text = "\n".join(copies)
    log.info("awareness clan-chat voicing (%d msg)", len(copies))

    baseline = await asyncio.to_thread(
        db.build_leader_action_baseline,
        action_type="in_game_relay",
        target_player_tag=None,
    )
    seq_note = f" ({len(copies)} messages, paste in order)" if len(copies) > 1 else ""
    prompt_text = f"Paste this clan-chat note (from #{channel_name}){seq_note}: {copy_text}"
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type="in_game_relay",
        objective=objective,
        prompt_text=prompt_text,
        rationale=(
            post.get("relay_reason") or post.get("summary") or "Brain-flagged for clan chat"
        ),
        target_channel_key="actions",
        target_channel_id=channel_config["id"],
        target_player_tag=None,
        target_player_name=None,
        source_signal_key=action_key,
        source_signal_type="awareness_relay",
        copy_original_text=copy_text,
        copy_current_text=copy_text,
        baseline=baseline,
        action_key=action_key,
        ui_version=LEADER_ACTION_UI_VERSION,
    )
    if not action or action.get("source_message_id"):
        return False
    card_messages = await post_leader_action_card(relay_channel, action, copy_messages=copies)
    return bool(card_messages)


# -- DM outreach (Phase 1) -------------------------------------------------
#
# A leader approves every card before any member is messaged. Flow logic lives
# in runtime/outreach.py; these are the thin Discord bridges.


def _outreach_tenure(joined_date) -> str | None:
    """A loose, warm tenure phrase from a join date — guarding the 2026-03-07
    'tracking start' artifact (pre-tracking joins are stamped that day, not real)."""
    if not joined_date:
        return None
    day = str(joined_date)[:10]
    if day <= "2026-03-07":
        return "a long-time member (here since before our records began)"
    try:
        jd = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except TypeError, ValueError:
        return None
    days = (datetime.now(timezone.utc) - jd).days
    if days < 21:
        return "a recent addition to the clan"
    if days < 60:
        return f"about {max(1, days // 7)} weeks in the clan"
    return f"about {max(1, round(days / 30))} months in the clan"


async def _send_member_dm(discord_user_id: str, content: str) -> tuple[bool, str]:
    """Deliver an outreach DM to a member. Returns ``(ok, detail)``."""
    try:
        uid = int(discord_user_id)
    except TypeError, ValueError:
        return False, "bad discord_user_id"
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
    except Exception as exc:
        log.warning("member outreach: could not fetch user %s: %s", discord_user_id, exc)
        return False, f"fetch failed: {exc}"
    if user is None:
        return False, "user not found"
    try:
        await user.send(content)
    except discord.Forbidden:  # hygiene: returns the reason to its caller
        return False, "member has DMs closed"
    except Exception as exc:
        log.warning("member outreach DM send failed: %s", exc, exc_info=True)
        return False, f"send error: {exc}"
    log.info("member outreach: DM sent to %s", discord_user_id)
    return True, "sent"


async def _raise_outreach_card(target: dict, copy: str):
    """Create and post one leader-gated 'Profile Outreach' card to #actions.
    Returns the action dict (or None). Never raises into the proposal loop."""
    try:
        channel_config = _channel_config_by_key("actions")
    except Exception:
        log.warning("member outreach: actions unavailable")
        return None
    relay_channel = bot.get_channel(int(channel_config["id"]))
    if relay_channel is None:
        log.warning("member outreach: actions channel not found")
        return None
    tag = target.get("player_tag")
    name = target.get("member_name") or tag
    try:
        baseline = await asyncio.to_thread(
            db.build_leader_action_baseline,
            action_type="member_outreach",
            target_player_tag=tag,
        )
        action = await asyncio.to_thread(
            db.create_leader_action_recommendation,
            action_type="member_outreach",
            objective=f"member_outreach:email:{tag}",
            prompt_text=f"DM {name} to ask for their email — approve to send, or Skip:",
            rationale=(
                f"{name} is a current member with no email on file; a DM builds a "
                "fuller profile. Leader-gated — nothing sends until you approve."
            ),
            target_channel_key="actions",
            target_channel_id=channel_config["id"],
            target_player_tag=tag,
            target_player_name=name,
            source_signal_key=f"member_outreach:email:{tag}",
            source_signal_type="member_outreach",
            copy_original_text=copy,
            copy_current_text=copy,
            action_key=f"member-outreach:email:{tag}",
            ui_version=LEADER_ACTION_UI_VERSION,
            baseline=baseline,
        )
    except Exception:
        log.exception("member outreach: failed to create card for %s", tag)
        return None
    if not action or action.get("source_message_id"):
        return None
    card_messages = await post_leader_action_card(relay_channel, action, copy_messages=[copy])
    return action if card_messages else None


async def _ops_log(message: str) -> None:
    """One-line #elixir-log ping for an operational event (outreach lifecycle,
    card posted, …). Never raises into the flow — a webhook hiccup must not break
    the path it observes."""
    from runtime import elixir_log

    try:
        await elixir_log.post_event_async(message)
    except Exception:
        log.debug("ops elixir-log post failed", exc_info=True)


async def _member_outreach_propose():
    """Scheduled: offer a few leader-gated outreach cards.

    Runs the synchronous, tested ``propose_cards`` flow with an async card-raise
    bridge.
    """
    from runtime import outreach

    loop = asyncio.get_running_loop()

    def _raise_sync(target, copy):
        fut = asyncio.run_coroutine_threadsafe(_raise_outreach_card(target, copy), loop)
        return fut.result(timeout=60)

    def _compose(target):
        # Elixir writes the ask in its own voice from a grounded facts brief (so it
        # nods to who they actually are); the flow falls back to the deterministic
        # template if this returns "" / raises.
        from agent.workflows import generate_outreach_ask

        tag = target.get("player_tag")
        name = target.get("member_name") or tag or "there"
        lines = [f"Member name: {name}"]
        try:
            from storage.roster import get_member_profile

            profile = get_member_profile(tag) or {}
            tenure = _outreach_tenure(profile.get("joined_date"))
            if tenure:
                lines.append(f"Time in the clan: {tenure}")
            trophies = profile.get("trophies")
            if trophies:
                arena = profile.get("arena_name")
                lines.append(f"Current trophies: {trophies}" + (f" (in {arena})" if arena else ""))
            fav = profile.get("current_favourite_card_name")
            if fav:
                lines.append(f"Favorite card: {fav}")
        except Exception:
            log.warning("outreach compose: profile lookup failed for %s", tag)
        lines.append("Context: a current POAP KINGS member with no email on file yet.")
        return generate_outreach_ask("\n".join(lines))

    try:
        proposed = await asyncio.to_thread(
            outreach.propose_cards, raise_card=_raise_sync, compose=_compose
        )
    except Exception:
        log.exception("member outreach propose failed")
        return
    runtime_status.mark_job_success("member_outreach_propose", f"proposed {len(proposed)}")
    if proposed:
        await _ops_log(
            f"📧 Proposed {len(proposed)} profile-outreach card(s) in #actions for leader review."
        )
    return proposed


async def _member_outreach_decision(action: dict, status: str) -> None:
    """Bridge a leader's decision on a member_outreach card into the flow: send
    the DM on approve, mark skipped on decline. Called from prompt_feedback."""
    from runtime import outreach

    loop = asyncio.get_running_loop()

    def _send_sync(discord_user_id, content):
        fut = asyncio.run_coroutine_threadsafe(_send_member_dm(discord_user_id, content), loop)
        return fut.result(timeout=60)

    row = await asyncio.to_thread(outreach.on_decision, action, status, send_dm=_send_sync)
    if not row:
        return
    name = action.get("target_player_name") or action.get("target_player_tag") or "a member"
    new_status = row.get("status")
    if new_status == "awaiting_reply":
        await _ops_log(f"📧 Sent profile-outreach DM to **{name}** — awaiting reply.")
    elif new_status == "failed":
        await _ops_log(
            f"⚠️ Profile-outreach DM to **{name}** failed: {row.get('last_error') or 'unknown error'}."
        )
    elif new_status == "skipped":
        await _ops_log(f"📧 Leader skipped profile outreach for **{name}**.")


async def _handle_outreach_dm(message) -> None:
    """Phase 2: a member's DM reply drives the email-collection state machine
    (reply with email -> emailed code -> reply with code -> verified). Only acts
    for a linked member who is mid-outreach; otherwise stays silent so Elixir
    isn't a general DM bot."""
    from runtime import email_verification, outreach

    member = await asyncio.to_thread(db.get_linked_member_for_discord_user, message.author.id)
    if not member:
        return
    from storage import member_outreach as mo

    tag = member["player_tag"]
    before = await asyncio.to_thread(mo.get_outreach, tag)
    reply = await asyncio.to_thread(
        outreach.handle_dm_reply,
        tag,
        message.content or "",
        start_verification=email_verification.start_verification,
        check_code=email_verification.check_code,
    )
    if reply:
        try:
            await message.channel.send(reply)
        except Exception:
            log.warning("outreach DM reply send failed", exc_info=True)
    # Log only real milestone transitions (a plain nudge doesn't move status, so
    # comparing before/after keeps #elixir-log to the moments that matter).
    after = await asyncio.to_thread(mo.get_outreach, tag)
    before_status = (before or {}).get("status")
    after_status = (after or {}).get("status")
    if after_status != before_status:
        name = member.get("member_name") or tag
        if after_status == "verifying":
            await _ops_log(f"📧 **{name}** shared an email — verification code sent.")
        elif after_status == "fulfilled":
            email = (after or {}).get("pending_email") or "their email"
            await _ops_log(f"✅ **{name}** verified {email} — profile updated.")
        elif after_status == "opted_out":
            await _ops_log(f"🚫 **{name}** opted out of profile outreach.")


# _editorial_sweep + _editorial_review retired 2026-07-10 with the Editor (their
# ActivityDefinitions are gone, so nothing schedules them). The brain composes
# with depth natively — no template gate to feed or self-review.


async def _db_backup():
    """Daily compressed snapshots of the single operational DB to
    ELIXIR_BACKUP_DIR (iCloud Drive—offsite via sync). Uses the online backup
    API with no downtime."""
    from scripts.backup_db import create_backup, prune_backups

    runtime_status.mark_job_start("db_backup")

    def _run():
        # v5.1 memory pass (memory.md D1/M6): memories live in the engine DB,
        # so ONE database covers everything — the separate memory-DB snapshot
        # is retired (its history stays prunable under its old prefix).
        results = {}
        op = create_backup()  # defaults: ELIXIR_DB_PATH → ELIXIR_BACKUP_DIR
        results["operational"] = {k: op.get(k) for k in ("path", "ok", "error")}
        prune_backups()
        prune_backups(prefix="elixir-v5-memory")
        return results

    try:
        results = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("db_backup", str(exc))
        log.exception("db backup failed")
        return
    ok = all(v.get("ok") for v in results.values())
    (runtime_status.mark_job_success if ok else runtime_status.mark_job_failure)(
        "db_backup", json.dumps(results, default=str)[:400]
    )
    log.info("db backup: %s", results)
    return results


async def _war_attendance_snapshot():
    """Finalize war_attendance_days for the just-closing war day (runtime.md §3).
    fame_delta stays NULL when no intra-day participation history exists — the
    evaluators read decks_used, which is always present."""
    from engine import db as engine_db
    from engine.emitters.war import finalize_attendance_day
    from engine.tick import _current_clock

    runtime_status.mark_job_start("war_attendance_snapshot")

    def _run():
        conn = engine_db.connect()
        try:
            # _current_clock adapts the stored snake_case race projection back
            # to the CR-shaped keys war_clock reads. Feeding the projection in
            # directly reads as permanent "training" (live incident 2026-07-04:
            # the 04:15 snapshot skipped on a Colosseum battle day).
            clock = _current_clock(conn, datetime.now(timezone.utc))
            if clock is None:
                return {"skipped": "no riverrace baseline"}
            if clock.phase == "training" or clock.season_id is None:
                return {"skipped": f"phase={clock.phase}"}
            finalized = finalize_attendance_day(
                conn, clock.season_id, clock.section_index, clock.war_day_index
            )
            conn.commit()
            return {
                "finalized": finalized,
                "season": clock.season_id,
                "section": clock.section_index,
                "day": clock.war_day_index,
            }
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("war_attendance_snapshot", str(exc))
        log.exception("war attendance snapshot failed")
        return
    runtime_status.mark_job_success("war_attendance_snapshot", json.dumps(result, default=str))
    return result


async def _action_outcome_refresh():
    """Daily leader-action hygiene carried from leadership-action-scan
    (runtime.md §3): refresh due outcomes, re-queue feedback synthesis. The
    scan/creation role retired — the engine owns the Q1 reactive path."""
    runtime_status.mark_job_start("action_outcome_refresh")
    try:
        refreshed = await asyncio.to_thread(db.refresh_due_leader_action_outcomes)
        if refreshed:
            log.info("action outcome refresh: %s due outcome(s)", len(refreshed))
            from runtime.leader_action_feedback import (
                queue_leader_action_feedback_refresh,
            )

            for action_type in sorted(
                {a.get("action_type") for a in refreshed if a.get("action_type")}
            ):
                queue_leader_action_feedback_refresh(action_type)
    except Exception as exc:
        runtime_status.mark_job_failure("action_outcome_refresh", str(exc))
        log.warning("action outcome refresh failed: %s", exc, exc_info=True)
        return
    runtime_status.mark_job_success(
        "action_outcome_refresh", f"refreshed {len(refreshed) if refreshed else 0}"
    )


@bot.event
async def on_ready():
    global SLASH_COMMANDS_SYNCED
    log.info("Elixir online as %s", bot.user)
    prompts.ensure_valid_discord_channel_config()
    await asyncio.to_thread(seed_startup_state)
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
                log.info(
                    "Synced /elixir commands to guild %s and cleared stale global commands",
                    GUILD_ID,
                )
            else:
                await bot.tree.sync()
                log.info("Synced global /elixir commands")
            SLASH_COMMANDS_SYNCED = True
        except Exception as exc:
            log.exception("Slash command sync failed: %s", exc)
    # Sync custom emoji
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await sync_emoji(guild)
    # The Observatory (admin web UI) — in-process, loopback-only, tailnet-gated.
    # Outside the scheduler guard (a reconnect must not skip it); start_webapp
    # is idempotent, and a webapp failure must never block the bot.
    try:
        from runtime.webapp.server import start_webapp

        await start_webapp(deps={"bot": bot})
    except Exception:
        log.exception("observatory webapp startup failed")
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
        # v5.1 startup (runtime.md §6): missing consumer cursors initialize at
        # the current stream head — replay is safe (durable ledger) but wasteful.
        try:
            initialized = await asyncio.to_thread(_engine_startup_cursors)
            if initialized:
                log.info("engine startup: %s cursor(s) initialized at head", initialized)
        except Exception:
            log.exception("engine startup cursor init failed")
        startup_posted = await _post_startup_message()
        if not startup_posted:
            log.warning("Startup announcement was not posted to leadership")
        log.info(
            "Scheduler started — %s",
            format_scheduler_startup_summary(sys.modules[__name__]),
        )
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
        try:
            from runtime.leader_action_ui import restore_leader_action_views

            await restore_leader_action_views(bot)
        except Exception as exc:
            # ERROR: the cards stay on Discord but their buttons stop working,
            # so a leader clicks Done and nothing happens. Silent from outside.
            log.exception("Leader action view restore failed: %s", exc)
        try:
            cards = await _post_pending_leader_action_cards()
            if cards:
                log.info("Posted %s pending leader-action card(s) at startup", cards)
        except Exception:
            log.exception("Startup leader-action card backfill failed")
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
async def on_message_delete(message):
    """Editor deletion feeder (engine/editor.py): an admin deleting one of
    Elixir's OWN posts in one of its posting lanes is the strongest
    anti-pattern signal — capture the copy before it's gone."""
    try:
        if bot.user is None or message.author is None or message.author.id != bot.user.id:
            return
        from engine import editor as engine_editor
        from runtime import lanes as engine_compose

        lane_channel_ids = {
            ch["channel_id"]: ch["channel_name"] for ch in engine_compose.channels().values()
        }
        channel_name = lane_channel_ids.get(getattr(message.channel, "id", None))
        if channel_name is None:
            return

        def _record():
            from engine import db as engine_db

            conn = engine_db.connect()
            try:
                return engine_editor.record_deleted_post(
                    conn, str(message.id), channel_name, message.content or ""
                )
            finally:
                conn.close()

        mid = await asyncio.to_thread(_record)
        if mid:
            log.info(
                "editor: deletion of message %s in #%s recorded as anti-pattern memory %s",
                message.id,
                channel_name,
                mid,
            )
    except Exception:
        log.exception("editor deletion feeder failed for message %s", getattr(message, "id", "?"))


@bot.event
async def on_raw_reaction_add(payload):
    await prompt_feedback.handle_raw_reaction_add(payload)


@bot.event
async def on_raw_reaction_remove(payload):
    await prompt_feedback.handle_raw_reaction_remove(payload)


PID_FILE = _process_service.PID_FILE


def main():
    from runtime.logging_setup import configure_logging, error_log_path, main_log_path

    configure_logging()
    log.info("logging to %s (errors also to %s)", main_log_path(), error_log_path())

    # Fail fast and by name. A missing secret used to surface late and cryptically
    # (a None token inside discord.py, or an auth error on the first LLM call);
    # refusing to boot names every missing variable at once instead.
    missing = runtime_status.missing_required_secrets()
    if missing:
        raise SystemExit(
            "Elixir cannot start — missing required environment variable(s): "
            f"{', '.join(missing)}. Set them in .env and retry."
        )
    return _process_service.main(TOKEN, bot)
