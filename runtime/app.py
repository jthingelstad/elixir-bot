"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import hashlib
import json
import os
import re
import logging
import sys
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

import cr_api  # re-exported; accessed by runtime submodules
import db
import elixir_agent
import prompts
from runtime.activities import format_scheduler_startup_summary, register_scheduled_activities
from runtime.admin import admin_command_requires_leader, dispatch_admin_command
from runtime.channel_router import route_message
from runtime.discord_commands import register_elixir_app_commands
from runtime.leader_action_policy import can_post_leader_action
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


def _json_trace_text(value):
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr(value)}, ensure_ascii=False)


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
    _bare_tag,
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
    WEEKLY_DISCORD_INVITE_RELAY_DAY,
    WEEKLY_DISCORD_INVITE_RELAY_HOUR,
    WEEKLY_RECAP_DAY,
    WEEKLY_RECAP_HOUR,
    _ask_elixir_daily_insight,
    _build_ask_elixir_daily_insight_context,
    _query_or_default,
    _summarize_member_rows,
    _weekly_clan_recap,
    _weekly_discord_invite_relay,
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
    MEMORY_SYNTHESIS_DRY_RUN,
    MEMORY_SYNTHESIS_HOUR,
    MEMORY_SYNTHESIS_POSTS_PER_CHANNEL,
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
    _memory_synthesis_cycle,
)
from runtime.system_status_post import (  # noqa: E402,F401
    _post_system_signal_updates,
    _preauthored_system_signal_result,
    _publish_pending_system_signal_updates,
    _system_signal_updates,
)
from runtime.helpers import (  # noqa: E402,F401
    _WEEKLY_RECAP_HEADER_RE,
    _channel_config_by_key,
    _format_weekly_recap_post,
    _strip_weekly_recap_header,
)
from runtime.jobs._site import (  # noqa: E402,F401
    _promotion_channel_posts,
    _promotion_content_cycle,
    _promotion_discord_required_text,
    _promotion_reddit_required_token,
    _unwrap_outer_bold,
    _validate_promote_content_or_raise,
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
from runtime.clan_chat_copy import (  # noqa: E402,F401
    generate_clan_chat_copy,
    sign_clan_chat_text,
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

def _v5_intent_key(metadata: dict | None) -> str:
    metadata = metadata or {}
    dedup_key = (metadata.get("event_core_dedup_key") or "").strip()
    if dedup_key:
        return f"v5:{dedup_key}"
    event_core_intent_id = (metadata.get("event_core_intent_id") or "").strip()
    if event_core_intent_id:
        return f"v5:{event_core_intent_id}"
    basis = json.dumps(metadata or {}, sort_keys=True, default=str).encode("utf-8")
    return f"v5:unknown:{hashlib.sha256(basis).hexdigest()[:16]}"


def _v5_message_payloads(sent_messages, fallback_text: str) -> list[dict]:
    payloads = []
    for idx, sent in enumerate(sent_messages or [], start=1):
        discord_message_id = getattr(sent, "id", None)
        content = getattr(sent, "content", None)
        if content is None:
            content = fallback_text if len(sent_messages or []) == 1 else ""
        discord_created_at = getattr(sent, "created_at", None)
        if hasattr(discord_created_at, "isoformat"):
            discord_created_at = discord_created_at.isoformat()
        payloads.append({
            "index": idx,
            "discord_message_id": str(discord_message_id) if discord_message_id is not None else None,
            "content": str(content or ""),
            "discord_created_at": discord_created_at,
        })
    return payloads


# Which recognized moments ALSO reach in-game clan chat (as a redraft of the
# posted Discord copy — the single-pipeline rule). Volume principle (Jamie,
# 2026-07-04, after the R108–R114 spam): Discord is high-volume by design;
# in-game clan chat is scarce, leader-voiced space. Only marquee moments
# relay — celebrate-volume types (arena_up, level_up) stay Discord-only, and
# _CLAN_CHAT_RELAY_DAILY_CAP bounds the worst day. Per-type guards
# (promoted-only, champion-only) live in the relay loop.
_V5_EVENT_LEADER_ACTION_TYPES = {
    "member_joined",
    "member_birthday",
    "join_anniversary",
    "clan_birthday",
    "career_wins_milestone",
    "collection_level_milestone",
    "card_unlocked",          # champion rarity only (guard in relay)
    "role_changed",           # promoted only (guard in relay)
    "weekly_donation_leader",
    "week_finished",
    "season_closed",
}
_CLAN_CHAT_RELAY_DAILY_CAP = 3


def _v5_event_detection_type(metadata: dict | None) -> str | None:
    metadata = metadata or {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    detection_type = summary.get("detection_type") or metadata.get("source_signal_type")
    if detection_type:
        return str(detection_type)
    intent_type = metadata.get("intent_type") or ""
    if ":" in intent_type:
        return intent_type.split(":", 1)[1]
    return None


def _v5_event_source_key(metadata: dict | None) -> str:
    metadata = metadata or {}
    return (
        str(metadata.get("event_core_dedup_key") or "").strip()
        or str(metadata.get("source_signal_key") or "").strip()
        or str(metadata.get("event_core_intent_id") or "").strip()
        or _v5_intent_key(metadata)
    )


def _v5_member_name_for_event(subject_tag: str | None, summary: dict) -> str | None:
    name = (summary.get("name") or summary.get("player_name") or "").strip()
    if name:
        return name
    if not subject_tag:
        return None
    try:
        profile = db.get_member_profile(subject_tag) or {}
    except Exception:
        profile = {}
    return (
        profile.get("member_name")
        or profile.get("current_name")
        or profile.get("name")
        or subject_tag
    )


def _v5_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _v5_first_int(*values) -> int | None:
    for value in values:
        parsed = _v5_int(value)
        if parsed is not None:
            return parsed
    return None


# (Removed 2026-07-04, single-pipeline rule: the relay's independent
# fact-gathering helpers — raw-profile lookups, badge maps, join-facts
# sentences, profile-fact markers — are gone. The Discord composition is
# the one authoring step; the clan-chat line redrafts its posted copy.)


def _v5_event_action_copy(detection_type: str, subject_name: str | None, summary: dict) -> str:
    name = subject_name or summary.get("clan_name") or "POAP KINGS"
    if detection_type == "member_joined":
        trophies = summary.get("trophies")
        if trophies:
            return (
                f"Welcome to POAP KINGS, {name}! Rolling in with {int(trophies):,} "
                "trophies. Glad you're here."
            )
        return f"Welcome to POAP KINGS, {name}! Glad to have you in the clan."
    if detection_type == "member_birthday":
        return f"Happy birthday, {name}! POAP KINGS is glad you're here."
    if detection_type == "join_anniversary":
        months = int(summary.get("months") or 0)
        if months and months % 12 == 0:
            years = months // 12
            label = f"{years}-year"
        elif months:
            label = f"{months}-month"
        else:
            label = "POAP KINGS"
        return f"Happy {label} anniversary, {name}! Glad you're still battling with us."
    if detection_type == "clan_birthday":
        years = int(summary.get("years") or 0)
        label = f"{years}-year" if years else "birthday"
        return f"Happy {label} birthday, POAP KINGS. Thanks for building this clan together."
    if detection_type == "career_wins_milestone":
        milestone = summary.get("milestone")
        try:
            milestone_label = f"{int(milestone):,}"
        except (TypeError, ValueError):
            milestone_label = str(milestone or "another big")
        return f"{name} hit {milestone_label} career wins. Huge POAP KINGS milestone."
    if detection_type == "collection_level_milestone":
        milestone = summary.get("milestone")
        try:
            milestone_label = f"{int(milestone):,}"
        except (TypeError, ValueError):
            milestone_label = str(milestone or "another big")
        return f"{name} hit collection level {milestone_label}. Huge POAP KINGS milestone."
    return f"Share a quick POAP KINGS shoutout for {name}."


def _v5_event_leader_action_spec(metadata: dict | None) -> dict | None:
    metadata = metadata or {}
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    detection_type = _v5_event_detection_type(metadata)
    if detection_type not in _V5_EVENT_LEADER_ACTION_TYPES:
        return None
    subject_tag = metadata.get("subject_tag")
    subject_name = _v5_member_name_for_event(subject_tag, summary)
    # Single-pipeline rule (Jamie, 2026-07-04): the relay never gathers its
    # own facts — the Discord composition is the one authoring step, and the
    # clan-chat line is a redraft of it. The old independent profile lookup
    # here is why the in-game welcome and the #clan-events welcome told
    # different stories.
    copy = _v5_event_action_copy(detection_type, subject_name, summary)
    copy = copy[:CLASH_COPY_MAX_LENGTH]
    action_type = "welcome_relay" if detection_type == "member_joined" else "celebration_relay"
    source_key = _v5_event_source_key(metadata)
    return {
        "action_type": action_type,
        "action_key": f"v5_event_leader_action:{source_key}",
        "objective": detection_type,
        "prompt_text": f"Paste this clan-chat note for the {detection_type.replace('_', ' ')} event: {copy}",
        "rationale": (
            "A v5 public clan event was delivered to Discord; joins, cake days, "
            "career-win milestones, and collection-level milestones also require "
            "an in-game leader-action card."
        ),
        "target_player_tag": subject_tag if subject_tag else None,
        "target_player_name": subject_name,
        "source_signal_key": source_key,
        "source_signal_type": detection_type,
        "copy": copy,
        "baseline": {
            "event_core": {
                "intent_type": metadata.get("intent_type"),
                "detection_type": detection_type,
                "summary": summary,
                "event_core_dedup_key": metadata.get("event_core_dedup_key"),
            },
        },
    }


def _recent_win_facts(player_tag: str | None) -> dict | None:
    """The player's most recent competitive win (opponent tag, mode, crowns) from
    battle_events, to seed a celebration relay so it can reference the actual game.
    Best-effort → None on error."""
    if not player_tag:
        return None
    try:
        from engine import db as engine_db
        from engine.recognition.compose import _recent_win

        conn = engine_db.connect()
        try:
            return _recent_win(conn, player_tag)
        finally:
            conn.close()
    except Exception:
        return None


def _v5_event_clan_chat_context(spec: dict, metadata: dict | None, discord_copy: str | None = None) -> str:
    """The REDRAFT prompt (single-pipeline rule, Jamie 2026-07-04): the posted
    Discord copy + the intent's facts are the SOLE inputs. The clan-chat line
    re-voices what Elixir already said — it never pulls in different facts.
    (The old builder did its own profile lookups and recent-win seeding and
    even invited tool use — that's how the in-game and Discord copy diverged.)"""
    summary = (((metadata or {}).get("summary") or {}) if isinstance((metadata or {}).get("summary"), dict) else {})
    source_line = ""
    if discord_copy:
        source_line = (
            "\nElixir already announced this moment on Discord. That post is the "
            "SOURCE — redraft it for in-game clan chat: same facts, same warmth, "
            "shorter and plainer (no markdown, no emoji codes, no links).\n"
            f"Discord post (source): {discord_copy}"
        )
    return (
        "V5 public event leader-action relay:\n"
        "Write ONE plain-text Clash Royale in-game clan chat message for a leader to paste.\n"
        "Keep it warm, specific, and natural for POAP KINGS clan chat.\n"
        "Use ONLY facts present in the source post or the facts JSON below — "
        "introduce nothing new; do not invent.\n"
        f"Event type: {spec.get('objective')}\n"
        f"Target player name: {spec.get('target_player_name') or 'POAP KINGS'}\n"
        f"Target player tag: {spec.get('target_player_tag') or ''}\n"
        f"Event facts JSON: {json.dumps(summary, sort_keys=True, default=str)}\n"
        f"Fallback message: {spec.get('copy') or ''}"
        f"{source_line}"
    )


async def _v5_event_clan_chat_copy(
    spec: dict, metadata: dict | None, discord_copy: str | None = None
) -> tuple[str, dict]:
    fallback = spec.get("copy") or "POAP KINGS keeps building together."
    fallback_signed = sign_clan_chat_text(fallback, limit=CLASH_COPY_MAX_LENGTH)
    try:
        generated = await generate_clan_chat_copy(
            intent=f"v5_event_{spec.get('objective') or 'leader_action'}",
            context=_v5_event_clan_chat_context(spec, metadata, discord_copy),
            max_messages=1,
            max_chars=CLASH_COPY_MAX_LENGTH,
            forbidden_terms=("http://", "https://", "www.", "Discord"),
            fallback_messages=[fallback],
            metadata={
                "source": "v5_event_leader_action",
                "event_type": spec.get("objective"),
                "action_type": spec.get("action_type"),
                "target_player_tag": spec.get("target_player_tag"),
            },
        )
    except Exception:
        log.warning(
            "v5 event leader-action clan-chat copy generation failed action_key=%s",
            spec.get("action_key"),
            exc_info=True,
        )
        return fallback_signed, {"used_fallback": True, "reason": "generation_error"}
    if generated and generated.messages:
        return generated.messages[0], {
            "used_fallback": bool(generated.used_fallback),
            "summary": generated.summary,
            "violations": generated.violations,
        }
    return fallback_signed, {"used_fallback": True, "reason": "empty_generation"}


async def _post_v5_event_leader_action(
    metadata: dict | None, sent_messages=None, discord_copy: str | None = None
) -> bool:
    spec = _v5_event_leader_action_spec(metadata)
    if not spec:
        return False
    existing = await asyncio.to_thread(db.get_leader_action_by_key, spec["action_key"])
    if existing and existing.get("source_message_id"):
        return False
    try:
        channel_config = _channel_config_by_key("arena-relay")
    except Exception:
        log.warning("v5 event leader-action skipped: arena-relay unavailable")
        return False
    relay_channel = bot.get_channel(int(channel_config["id"]))
    if relay_channel is None:
        log.warning("v5 event leader-action skipped: arena-relay channel not found")
        return False

    baseline = dict(spec["baseline"])
    baseline["public_delivery"] = {
        "message_ids": [
            str(getattr(message, "id", ""))
            for message in (sent_messages or [])
            if getattr(message, "id", None) is not None
        ],
    }
    copy, copy_metadata = await _v5_event_clan_chat_copy(spec, metadata, discord_copy)
    prompt_text = f"Paste this clan-chat note for the {spec['objective'].replace('_', ' ')} event: {copy}"
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type=spec["action_type"],
        objective=spec["objective"],
        prompt_text=prompt_text,
        rationale=spec["rationale"],
        target_channel_key="arena-relay",
        target_channel_id=channel_config["id"],
        target_player_tag=spec["target_player_tag"],
        target_player_name=spec["target_player_name"],
        source_signal_key=spec["source_signal_key"],
        source_signal_type=spec["source_signal_type"],
        copy_original_text=copy,
        copy_current_text=copy,
        baseline=baseline,
        action_key=spec["action_key"],
        ui_version=LEADER_ACTION_UI_VERSION,
    )
    if not action or action.get("source_message_id"):
        return False

    card_messages = await post_leader_action_card(relay_channel, action, copy_messages=[copy])
    first_message_id = getattr(card_messages[0], "id", None) if card_messages else None
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(relay_channel),
        "assistant",
        copy,
        summary=f"Leader action R{action.get('action_id')}: {spec['objective']}",
        **_channel_msg_kwargs(relay_channel),
        workflow="arena-relay",
        event_type=spec["action_type"],
        discord_message_id=first_message_id,
        raw_json={
            "leader_action": action,
            "clan_chat_copy": copy,
            "clan_chat_copy_metadata": copy_metadata,
            "event_core": metadata or {},
        },
    )
    return True






def _upsert_v5_operational_intent(
    *,
    channel_id,
    text: str,
    metadata: dict | None,
    status: str,
    error_detail: str | None = None,
) -> dict:
    metadata = dict(metadata or {})
    target_channel_id = metadata.get("target_channel_id") or channel_id
    target_channel_key = metadata.get("target_channel_key")
    summary = metadata.get("summary") if isinstance(metadata.get("summary"), dict) else {}
    payload = {
        **metadata,
        "original_copy": text,
        "audit_source": "runtime.app._v5_post",
    }
    return db.upsert_communication_intent(
        intent_key=_v5_intent_key(metadata),
        workflow="v5-reactive",
        intent_type=metadata.get("intent_type") or "event_core",
        status=status,
        target_channel_key=target_channel_key,
        target_channel_id=target_channel_id,
        source_signal_key=metadata.get("source_signal_key"),
        source_signal_type=metadata.get("source_signal_type"),
        covers_signal_keys=metadata.get("caused_by") or [],
        summary=_json_trace_text(summary),
        content_preview=_preview_text(text, limit=500),
        error_detail=error_detail,
        payload=payload,
    )


def _record_v5_delivery_failure(channel_id, text: str, metadata: dict | None, error_detail: str) -> None:
    _upsert_v5_operational_intent(
        channel_id=channel_id,
        text=text,
        metadata=metadata,
        status="failed",
        error_detail=error_detail,
    )


def _record_v5_delivery_success(channel, text: str, sent_messages, metadata: dict | None) -> None:
    operational_intent = _upsert_v5_operational_intent(
        channel_id=getattr(channel, "id", None),
        text=text,
        metadata=metadata,
        status="planned",
    )
    intent_id = operational_intent.get("intent_id")
    message_payloads = _v5_message_payloads(sent_messages, text)
    for payload in message_payloads:
        db.save_message(
            _channel_scope(channel),
            "assistant",
            payload["content"],
            **_channel_msg_kwargs(channel),
            workflow="v5-reactive",
            event_type=(metadata or {}).get("intent_type") or "event_core",
            discord_message_id=payload.get("discord_message_id"),
            raw_json={
                **(metadata or {}),
                "posted_message": payload,
                "original_copy": text,
            },
            intent_id=int(intent_id) if intent_id is not None else None,
        )
    db.mark_communication_intent_delivered(
        int(intent_id),
        target_channel_id=getattr(channel, "id", None),
        message_ids=[item["discord_message_id"] for item in message_payloads if item.get("discord_message_id")],
        payload={
            **(metadata or {}),
            "posted_messages": message_payloads,
            "original_copy": text,
        },
    )


async def _v5_post(channel_id, text, *, metadata: dict | None = None) -> bool:
    """Post v5 agent-composed copy to a channel by id (the Discord send bridge)."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        log.warning("v5: channel %s not found; skipping post", channel_id)
        await asyncio.to_thread(
            _record_v5_delivery_failure,
            channel_id,
            text,
            metadata,
            "channel_not_found",
        )
        return False
    try:
        sent_messages = await _post_to_elixir(channel, {"content": text})
    except Exception as exc:
        await asyncio.to_thread(
            _record_v5_delivery_failure,
            channel_id,
            text,
            metadata,
            f"{type(exc).__name__}: {exc}",
        )
        raise
    if not sent_messages:
        await asyncio.to_thread(
            _record_v5_delivery_failure,
            channel_id,
            text,
            metadata,
            "no_discord_messages_returned",
        )
        return False
    try:
        await asyncio.to_thread(_record_v5_delivery_success, channel, text, sent_messages, metadata)
    except Exception:
        log.exception("v5 delivery audit failed for channel %s", channel_id)
    try:
        await _post_v5_event_leader_action(metadata, sent_messages=sent_messages)
    except Exception:
        log.exception("v5 event leader-action failed for channel %s", channel_id)
    return True


def _engine_compose_copy(intent) -> str | None:
    """Compose voice for an engine intent (recognition.md §7): subject history
    context + the same agent path as before. Meta-guard + deterministic
    fallback live in engine.delivery.consume."""
    from engine import db as engine_db
    from engine.recognition import compose as engine_compose

    lanes = engine_compose.channels()
    ch = lanes.get(intent["lane"]) or {}
    conn = engine_db.connect()
    try:
        context = engine_compose.intent_context(conn, intent)
    finally:
        conn.close()
    result = elixir_agent.generate_channel_update(
        ch.get("channel_name") or intent["lane"],
        intent["lane"],
        context,
        leadership=bool(ch.get("leadership")),
    )
    if isinstance(result, str):
        return result.strip() or None
    if isinstance(result, dict):
        posts = result.get("posts")
        if posts:
            p = posts[0]
            if isinstance(p, dict):
                return (p.get("content") or p.get("summary") or "").strip() or None
            return str(p)
        return (result.get("content") or result.get("summary") or "").strip() or None
    return None


async def _engine_send(channel_id: int, text: str):
    """Async Discord send for the engine; returns the first message id or None."""
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        log.warning("engine: channel %s not found; send failed", channel_id)
        return None
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
        channel_config = _channel_config_by_key("arena-relay")
    except Exception:
        log.warning("engine leader-action cards skipped: arena-relay unavailable")
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
                    a["action_id"], source_message_id="posting"
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
        except Exception:
            log.exception("engine leader-action card post failed for %s", action.get("action_id"))
            if not post_attempted:
                try:  # sentinel write itself failed pre-post — safe to retry
                    await asyncio.to_thread(
                        lambda a=action: db.update_leader_action_message(
                            a["action_id"], source_message_id=None
                        )
                    )
                except Exception:
                    log.debug("sentinel clear failed for %s", action.get("action_id"),
                              exc_info=True)
    return posted


async def _ensure_role_action_clan_chat_copy(action: dict) -> dict:
    """Every promotion/demotion/kick card carries an in-game clan-chat message
    explaining the WHY (Jamie 2026-07-05: the clan must know why these actions
    happen). Generated at post time so it covers ALL sources — the weekly
    review, the reactive kick path, and manual leadership cards alike (none of
    which set copy at creation). LLM-composed from the rationale with a
    deterministic role-action fallback; never blocks the card."""
    from runtime.clan_chat_copy import (
        CLASH_COPY_MAX_LENGTH,
        ROLE_ACTION_TYPES,
        generate_clan_chat_copy,
        role_action_clan_chat_copy,
    )

    atype = action.get("action_type")
    if atype not in ROLE_ACTION_TYPES:
        return action
    if action.get("copy_current_text") or action.get("copy_original_text"):
        return action  # already has copy (e.g. a re-post)

    name = action.get("target_player_name") or action.get("target_player_tag") or "this member"
    rationale = action.get("rationale") or ""
    fallback = role_action_clan_chat_copy(
        action_type=atype, target_player_name=name, rationale=rationale,
        max_chars=CLASH_COPY_MAX_LENGTH,
    )
    copy_text = fallback
    try:
        context = (
            f"Tell the POAP KINGS clan, in in-game clan chat, WHY this is happening — "
            f"so the reason is clear to everyone. Action: {atype.replace('_', ' ')} for "
            f"{name}. Summarize this reasoning warmly and briefly; introduce nothing not "
            f"present here: {rationale}"
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
        log.warning("role-action clan-chat copy generation failed for %s",
                    action.get("action_id"), exc_info=True)

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
        action = {**action, "copy_original_text": copy_text, "copy_current_text": copy_text}
    return action


async def _relay_engine_clan_chat_actions(fulfilled_intents: list) -> int:
    """Carry the clan-chat relay tradition: certain public moments (joins, cake
    days, career/collection milestones) also get an in-game copy card for
    leaders (at most half the players use Discord — Q7 context).

    Single-pipeline rule (Jamie, 2026-07-04): the Discord composition is the
    one authoring step. The relay fetches the POSTED Discord message and the
    clan-chat line is a redraft of it — same facts, in-game voice."""
    from engine.recognition import compose as engine_compose

    # Daily cap — the in-game channel is scarce space (volume principle).
    def _relays_today() -> int:
        # Chicago-day bound (cold review #7): a UTC-midnight window resets at
        # 18:00/19:00 local, allowing up to 2× the cap in one Chicago day.
        day_start_utc, _ = db.chicago_day_bounds_utc(db.chicago_today())
        conn = db.get_connection()
        try:
            return conn.execute(
                """SELECT COUNT(*) FROM leader_action_recommendations
                   WHERE action_key LIKE 'v5_event_leader_action:%'
                     AND status != 'rejected'
                     AND created_at >= ?""",
                (day_start_utc,),
            ).fetchone()[0]
        finally:
            conn.close()

    posted = 0
    already_today = await asyncio.to_thread(_relays_today)
    for intent in fulfilled_intents:
        if already_today + posted >= _CLAN_CHAT_RELAY_DAILY_CAP:
            log.info("clan-chat relay daily cap reached (%s) — skipping remainder",
                     _CLAN_CHAT_RELAY_DAILY_CAP)
            break
        try:
            payload = json.loads(intent["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        event_type = payload.get("event_type") or (intent["intent_type"] or "").split(":", 1)[-1]
        if event_type not in _V5_EVENT_LEADER_ACTION_TYPES:
            continue
        # Per-type guards: only celebration-grade variants reach in-game chat.
        if event_type == "role_changed" and payload.get("direction") != "promoted":
            continue
        if event_type == "card_unlocked" and str(payload.get("rarity", "")).lower() != "champion":
            continue
        discord_copy = None
        try:
            lanes = engine_compose.channels()
            lane = lanes.get(intent["lane"]) or {}
            msg_id = intent["discord_message_id"]
            if lane.get("channel_id") and msg_id:
                channel = bot.get_channel(int(lane["channel_id"]))
                if channel is not None:
                    message = await channel.fetch_message(int(msg_id))
                    discord_copy = getattr(message, "content", None)
        except Exception:
            log.debug("relay: posted-copy fetch failed for intent %s",
                      intent["intent_id"], exc_info=True)
        summary = dict(payload)
        summary.setdefault("detection_type", event_type)
        metadata = {
            "intent_type": intent["intent_type"],
            "subject_tag": payload.get("subject_tag") or payload.get("tag"),
            "summary": summary,
            "event_core_dedup_key": intent["recognition_key"],
            "source_signal_key": intent["recognition_key"],
            "source_signal_type": event_type,
        }
        try:
            if await _post_v5_event_leader_action(metadata, discord_copy=discord_copy):
                posted += 1
        except Exception:
            log.exception("clan-chat relay failed for intent %s", intent["intent_id"])
    return posted


async def _engine_tick():
    """The v5.1 engine tick (runtime.md §2): poll → mirror → emit → project →
    manage → recognize → deliver, then leader-action cards + telemetry."""
    import cr_api as _cr_api

    from engine import db as engine_db
    from engine import tick as engine_tick_mod
    from engine.recognition import compose as engine_compose

    runtime_status.mark_job_start("engine_tick")
    loop = asyncio.get_running_loop()

    def send_fn(lane: str, copy: str, thread_id: int | None = None):
        lanes = engine_compose.channels()
        ch = lanes.get(lane) or lanes.get("arena-relay")
        if not ch:
            raise RuntimeError(f"engine send: lane {lane!r} unresolved and no fail-closed lane")
        # Thread routing (channels.md §2): the thread is a delivery ADDRESS —
        # try it first, fall back to the lane channel (never block on a room).
        if thread_id:
            future = asyncio.run_coroutine_threadsafe(
                _engine_send(int(thread_id), copy), loop
            )
            message_id = future.result(timeout=120)
            if message_id is not None:
                return message_id
            log.warning("engine send: thread %s failed; falling back to lane %s", thread_id, lane)
        future = asyncio.run_coroutine_threadsafe(
            _engine_send(ch["channel_id"], copy), loop
        )
        message_id = future.result(timeout=120)
        if message_id is None:
            raise RuntimeError(f"engine send: post to lane {lane!r} failed")
        return message_id

    def _run():
        from engine import editor as engine_editor

        conn = engine_db.connect()
        try:
            counters = engine_tick_mod.run_tick(
                conn, api=_cr_api, send_fn=send_fn, compose_fn=_engine_compose_copy,
                editor_gate=engine_editor.gate,  # live only — offline/tests never inject it
            )
            fulfilled = conn.execute(
                """SELECT * FROM communication_intents
                   WHERE status = 'fulfilled' AND fulfilled_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-15 minutes')"""
            ).fetchall()
            return counters, fulfilled
        finally:
            conn.close()

    try:
        counters, fulfilled = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("engine_tick", str(exc))
        log.exception("engine tick failed")
        return
    try:
        cards = await _post_pending_leader_action_cards()
        if cards:
            counters["leader_action_cards"] = cards
        relayed = await _relay_engine_clan_chat_actions(fulfilled)
        if relayed:
            counters["clan_chat_relays"] = relayed
        # Bounded-event thread lifecycle (channels.md §2) — best-effort by
        # contract; a room failure never touches the ceremony.
        from engine import db as _engine_db
        from runtime import threads as _threads

        lanes = engine_compose.channels()
        rr = lanes.get("river-race") or {}
        if rr.get("channel_id"):
            born = await _threads.ensure_war_week_thread(
                bot, _engine_db.connect, rr["channel_id"]
            )
            if born:
                counters["war_week_thread_born"] = born
        closed = await _threads.close_war_week_threads(bot, _engine_db.connect, fulfilled)
        if closed:
            counters["war_week_threads_closed"] = closed
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
    try:
        await _post_pending_leader_action_cards(limit=6)
        channel_id = _get_singleton_channel_id("arena-relay")
        channel = bot.get_channel(channel_id) if channel_id else None
        if channel is not None:
            promote_n = len(result.get("promote_eligible") or [])
            demote_n = len(result.get("demote_eligible") or [])
            withdrawn_n = len(result.get("withdrawn") or [])
            context = (
                f"Weekly leadership review for week starting {result.get('week_anchor')}: "
                f"{promote_n} promotion candidacies, {demote_n} demotion candidacies, "
                f"{withdrawn_n} auto-withdrawn. Member states: "
                f"{json.dumps((result.get('rows') or [])[:20], default=str)[:1500]}. "
                "Summarize the week for leadership: who is eligible and why (evidence, "
                "not judgment), who is at risk, and note that candidacy cards follow separately."
            )
            await compose_and_post(channel, lane="arena-relay", context=context, leadership=True)
    except Exception:
        log.exception("weekly review summary post failed")
    runtime_status.mark_job_success(
        "weekly_leadership_review",
        f"promote={len(result.get('promote_eligible') or [])} "
        f"demote={len(result.get('demote_eligible') or [])}",
    )
    return result


async def _engine_health():
    """Daily engine health audit (runtime/health.py) — the institutionalized
    version of the 2026-07-04 live behavioral audit. Read-only checks; posts
    to #elixir-log ONLY when something is off, silent when healthy."""
    from runtime import elixir_log, health

    runtime_status.mark_job_start("engine_health")

    def _run():
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT status_json FROM runtime_job_status WHERE job_name = 'engine_health'"
            ).fetchone()
            previous_size = None
            if row:
                try:
                    last = json.loads(json.loads(row["status_json"]).get("last_summary") or "{}")
                    previous_size = last.get("db_size_bytes")
                except (TypeError, ValueError):
                    pass
            return health.run_all(conn, previous_size)
        finally:
            conn.close()

    try:
        problems, size = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("engine_health", str(exc))
        log.exception("engine health audit failed")
        return
    if problems:
        body = "\n".join(f"- {p}" for p in problems)
        await asyncio.to_thread(
            elixir_log.post_event, f"**Engine health: {len(problems)} issue(s)**\n{body}"
        )
        log.warning("engine health: %d issue(s): %s", len(problems), "; ".join(problems))
    else:
        log.info("engine health: all checks clean (db %.1fMB)", size / 1e6)
    runtime_status.mark_job_success(
        "engine_health",
        json.dumps({"problems": len(problems), "db_size_bytes": size}),
    )
    return {"problems": problems, "db_size_bytes": size}


async def _editorial_sweep():
    """Daily Editor rubric feeder (editor.md §3): yesterday's 👎/👍 prompt
    feedback becomes anti-pattern/exemplar candidates. Kept separate from
    engine-health so that job stays strictly read-only."""
    from engine import db as engine_db
    from engine import editor as engine_editor

    runtime_status.mark_job_start("editorial_sweep")

    def _run():
        conn = engine_db.connect()
        try:
            return engine_editor.sweep_prompt_feedback(conn)
        finally:
            conn.close()

    try:
        counters = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("editorial_sweep", str(exc))
        log.exception("editorial sweep failed")
        return
    runtime_status.mark_job_success("editorial_sweep", json.dumps(counters))
    log.info("editorial sweep: %s", counters)
    return counters


async def _editorial_review():
    """The Editor's weekly self-review (editor.md §4, Sun 20:07 CT): score the
    week's gated output, write the synthesis memory, post ONE report with the
    drift line + proposed rubric additions (auto-added at confidence 0.6;
    Jamie's 👎 or a note retires them — silence is consent)."""
    from engine import db as engine_db
    from engine import editor as engine_editor
    from runtime import elixir_log

    runtime_status.mark_job_start("editorial_review")

    def _run():
        conn = engine_db.connect()
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return engine_editor.compose_weekly_review(conn, now)
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure("editorial_review", str(exc))
        log.exception("editorial review failed")
        return
    try:
        await asyncio.to_thread(elixir_log.post_event, result["report"])
    except Exception:
        log.exception("editorial review report post failed")
    runtime_status.mark_job_success(
        "editorial_review",
        json.dumps({"verdicts": result.get("verdict_counts"),
                    "proposals": len(result.get("proposals") or [])}),
    )
    return result


async def _player_pulse():
    """The player Pulse's 30-minute check (pulse.md §4): when an 8h window is
    complete, build its facts and raise ONE battle-feed intent; the engine
    tick composes/gates/delivers it. The anchor rides in this job's own
    persisted last_summary (`anchor=<iso>`) — restart-proof tiling."""
    from runtime.jobs import player_pulse

    runtime_status.mark_job_start(player_pulse.JOB_NAME)

    def _run():
        conn = db.get_connection()
        try:
            return player_pulse.run_check(conn)
        finally:
            conn.close()

    try:
        summary = await asyncio.to_thread(_run)
    except Exception as exc:
        runtime_status.mark_job_failure(player_pulse.JOB_NAME, str(exc))
        log.exception("player pulse check failed")
        return
    runtime_status.mark_job_success(player_pulse.JOB_NAME, summary)
    if not summary.startswith("waiting"):
        log.info("player pulse: %s", summary)
    # Game-event thread watcher (channels.md §2, v1 thread kind #2) — rides
    # the pulse's cadence; best-effort, never fails the job.
    try:
        from engine import db as _engine_db
        from engine.recognition import compose as engine_compose
        from runtime import threads as _threads

        bf = (engine_compose.channels().get("battle-feed") or {}).get("channel_id")
        if bf:
            await _watch_game_event_threads(bf, _engine_db.connect)
    except Exception:
        log.debug("game-event thread watcher failed", exc_info=True)
    return summary


async def _watch_game_event_threads(battle_feed_channel_id: int, conn_factory):
    """New special_event mode seen in the last 3 days → born (thread + row);
    a mode absent 3 days → locked. One open room per mode."""
    from runtime import threads as _threads

    def _scan():
        conn = conn_factory()
        try:
            cutoff = "strftime('%Y%m%dT%H%M%S', 'now', '-3 days')"
            # A room must be EARNED: >=8 battles by >=3 distinct players in the
            # window. The classifier leaks single-digit misclassified rows
            # (live 2026-07-05: 3 stray 'Ladder' rows carried special_event and
            # birthed a public thread); volume+breadth thresholds make stray
            # rows structurally unable to open rooms. Friendly-flavored
            # modes are excluded outright: they're evergreen casual play, not
            # bounded events - their rooms would never earn a lock (live
            # 2026-07-05: Showdown_Friendly, 47 battles of ordinary friendlies
            # classified special_event).
            fresh = conn.execute(
                f"""SELECT game_mode_name, MAX(battle_time) last_seen
                    FROM battle_events
                    WHERE mode_group = 'special_event'
                      AND battle_time >= {cutoff}
                      AND game_mode_name NOT LIKE '%Friendly%'
                    GROUP BY game_mode_name
                    HAVING COUNT(*) >= 8 AND COUNT(DISTINCT player_tag) >= 3"""
            ).fetchall()
            rows = conn.execute(
                "SELECT * FROM event_threads WHERE locked_at IS NULL"
            ).fetchall()
            return (
                {r["game_mode_name"]: r["last_seen"] for r in fresh},
                [dict(r) for r in rows],
            )
        finally:
            conn.close()

    fresh, open_rows = await asyncio.to_thread(_scan)
    open_modes = {r["mode_name"] for r in open_rows}
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for mode, last_seen in fresh.items():
        if mode in open_modes:
            continue
        thread = await _threads.create_thread(bot, battle_feed_channel_id, mode.replace("_", " "))
        if thread is None:
            continue

        def _record(m=mode, tid=thread.id, seen=last_seen):
            conn = conn_factory()
            try:
                conn.execute(
                    """INSERT INTO event_threads (mode_name, thread_id, born_at, last_seen_at)
                       VALUES (?, ?, ?, ?)""",
                    (m, tid, now_iso, seen),
                )
                conn.commit()
            finally:
                conn.close()

        await asyncio.to_thread(_record)
        try:
            await thread.send(
                f"🎪 **{mode.replace('_', ' ')}** is live in the clan — this room "
                "follows it while it runs. It locks itself when the event fades."
            )
        except Exception:
            log.debug("event thread opener failed", exc_info=True)
        log.info("game-event thread born: %s (%s)", mode, thread.id)

    for row in open_rows:
        if row["mode_name"] in fresh:
            def _touch(rid=row["event_thread_id"], seen=fresh[row["mode_name"]]):
                conn = conn_factory()
                try:
                    conn.execute(
                        "UPDATE event_threads SET last_seen_at = ? WHERE event_thread_id = ?",
                        (seen, rid))
                    conn.commit()
                finally:
                    conn.close()
            await asyncio.to_thread(_touch)
            continue
        if row["thread_id"] and await _threads.lock_thread(
            bot, row["thread_id"],
            closing_text="🎪 The event has moved on — the record stands."):
            def _lock(rid=row["event_thread_id"]):
                conn = conn_factory()
                try:
                    conn.execute(
                        "UPDATE event_threads SET locked_at = ? WHERE event_thread_id = ?",
                        (now_iso, rid))
                    conn.commit()
                finally:
                    conn.close()
            await asyncio.to_thread(_lock)
            log.info("game-event thread locked: %s", row["mode_name"])


async def _db_backup():
    """Daily compressed snapshots of the operational + memory DBs to
    ELIXIR_BACKUP_DIR (iCloud Drive — offsite via sync). Uses the online
    backup API; no downtime. Added 2026-07-04 (Jamie: offsite backup)."""
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
            from runtime.leader_action_feedback import queue_leader_action_feedback_refresh

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
    """Editor deletion feeder (editor.md §3): an admin deleting one of
    Elixir's OWN posts in one of its posting lanes is the strongest
    anti-pattern signal — capture the copy before it's gone."""
    try:
        if bot.user is None or message.author is None or message.author.id != bot.user.id:
            return
        from engine import editor as engine_editor
        from engine.recognition import compose as engine_compose

        lane_channel_ids = {
            ch["channel_id"]: ch["channel_name"]
            for ch in engine_compose.channels().values()
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
            log.info("editor: deletion of message %s in #%s recorded as anti-pattern memory %s",
                     message.id, channel_name, mid)
    except Exception:
        log.exception("editor deletion feeder failed for message %s",
                      getattr(message, "id", "?"))


@bot.event
async def on_raw_reaction_add(payload):
    await prompt_feedback.handle_raw_reaction_add(payload)


@bot.event
async def on_raw_reaction_remove(payload):
    await prompt_feedback.handle_raw_reaction_remove(payload)


PID_FILE = _process_service.PID_FILE


def main():
    return _process_service.main(TOKEN, bot)
