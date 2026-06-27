"""runtime.app — Elixir Discord bot runtime."""

import asyncio
import hashlib
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
    _leadership_action_scan,
    _query_or_default,
    _summarize_member_rows,
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


_V5_EVENT_LEADER_ACTION_TYPES = {
    "member_joined",
    "member_birthday",
    "join_anniversary",
    "clan_birthday",
    "career_wins_milestone",
    "collection_level_milestone",
}


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


def _v5_latest_raw_player_payload(subject_tag: str | None) -> dict:
    tag_key = (subject_tag or "").strip().upper().removeprefix("#")
    if not tag_key:
        return {}
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT payload_json, fetched_at FROM raw_api_payloads "
            "WHERE endpoint = 'player' AND UPPER(REPLACE(entity_key, '#', '')) = ? "
            "ORDER BY fetched_at DESC, payload_id DESC LIMIT 1",
            (tag_key,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    payload["_fetched_at"] = row["fetched_at"]
    return payload


def _v5_badge_map(player_payload: dict) -> dict:
    badges = player_payload.get("badges") or []
    if not isinstance(badges, list):
        return {}
    return {
        str(badge.get("name") or ""): badge
        for badge in badges
        if isinstance(badge, dict) and badge.get("name")
    }


def _v5_badge_int(badges: dict, name: str, field: str = "progress") -> int | None:
    badge = badges.get(name) or {}
    if not isinstance(badge, dict):
        return None
    return _v5_int(badge.get(field))


def _v5_member_profile_facts(subject_tag: str | None) -> dict:
    """Collect compact player facts for v5 event relays.

    New joins can exist in v5/raw API data before the legacy member-profile
    projection has a full profile row. Read both sources so welcome copy can
    still be grounded in the player Elixir just observed.
    """
    if not subject_tag:
        return {}
    try:
        profile = db.get_member_profile(subject_tag) or {}
    except Exception:
        profile = {}
    raw = _v5_latest_raw_player_payload(subject_tag)
    badges = _v5_badge_map(raw)
    favourite = raw.get("currentFavouriteCard") if isinstance(raw.get("currentFavouriteCard"), dict) else {}
    arena = raw.get("arena") if isinstance(raw.get("arena"), dict) else {}

    facts = {
        "source": "member_profile/raw_player_payload",
        "raw_player_fetched_at": raw.get("_fetched_at"),
        "name": profile.get("member_name") or raw.get("name"),
        "account_age_years": _v5_first_int(
            profile.get("cr_account_age_years"),
            _v5_badge_int(badges, "YearsPlayed", "level"),
        ),
        "account_age_days": _v5_first_int(
            profile.get("cr_account_age_days"),
            _v5_badge_int(badges, "YearsPlayed", "progress"),
        ),
        "collection_level": _v5_first_int(
            profile.get("cr_collection_level"),
            _v5_badge_int(badges, "CollectionLevel", "progress"),
        ),
        "battle_wins": _v5_first_int(
            profile.get("cr_battle_wins"),
            raw.get("wins"),
            _v5_badge_int(badges, "BattleWins", "progress"),
        ),
        "clan_war_wins": _v5_first_int(
            profile.get("cr_clan_war_wins"),
            raw.get("warDayWins"),
            _v5_badge_int(badges, "ClanWarWins", "progress"),
            _v5_badge_int(badges, "ClanWarsVeteran", "progress"),
        ),
        "total_donations": _v5_first_int(
            profile.get("cr_clan_donations"),
            raw.get("totalDonations"),
            _v5_badge_int(badges, "ClanDonations", "progress"),
        ),
        "banner_count": _v5_first_int(
            profile.get("cr_banner_count"),
            _v5_badge_int(badges, "BannerCollection", "progress"),
        ),
        "emote_count": _v5_first_int(
            profile.get("cr_emote_count"),
            _v5_badge_int(badges, "EmoteCollection", "progress"),
        ),
        "trophies": _v5_first_int(profile.get("trophies"), raw.get("trophies")),
        "best_trophies": _v5_first_int(profile.get("best_trophies"), raw.get("bestTrophies")),
        "battle_count": _v5_first_int(profile.get("career_battle_count"), raw.get("battleCount")),
        "current_win_streak": _v5_int(raw.get("currentWinLoseStreak")),
        "favorite_card": profile.get("current_favourite_card_name") or favourite.get("name"),
        "arena_name": profile.get("arena_name") or arena.get("name"),
    }
    return {
        key: value
        for key, value in facts.items()
        if value not in (None, "", [], {})
    }


def _v5_number(value: int) -> str:
    return f"{int(value):,}"


def _v5_member_profile_fact_phrases(facts: dict) -> list[str]:
    phrases: list[str] = []
    if facts.get("account_age_years") is not None:
        years = int(facts["account_age_years"])
        phrases.append(f"{years} year{'s' if years != 1 else ''} played")
    if facts.get("battle_wins") is not None:
        phrases.append(f"{_v5_number(facts['battle_wins'])} wins")
    if facts.get("collection_level") is not None:
        phrases.append(f"collection level {_v5_number(facts['collection_level'])}")
    trophies = facts.get("trophies")
    best = facts.get("best_trophies")
    if trophies is not None:
        if best is not None and int(best) > int(trophies):
            phrases.append(f"{_v5_number(trophies)} trophies, best {_v5_number(best)}")
        else:
            phrases.append(f"{_v5_number(trophies)} trophies")
    if facts.get("current_win_streak") is not None and int(facts["current_win_streak"]) > 1:
        phrases.append(f"{int(facts['current_win_streak'])}-win streak")
    if facts.get("battle_count") is not None:
        phrases.append(f"{_v5_number(facts['battle_count'])} battles")
    if facts.get("total_donations") is not None:
        phrases.append(f"{_v5_number(facts['total_donations'])} donations")
    if facts.get("favorite_card"):
        phrases.append(f"{facts['favorite_card']} as a favorite card")
    return phrases


def _v5_join_facts_sentence(facts: dict, *, limit: int = 3) -> str:
    phrases = _v5_member_profile_fact_phrases(facts)[:limit]
    if not phrases:
        return ""
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"


def _v5_member_profile_fact_markers(facts: dict) -> list[str]:
    markers: list[str] = []
    for key in (
        "account_age_years",
        "battle_wins",
        "collection_level",
        "trophies",
        "best_trophies",
        "battle_count",
        "total_donations",
        "banner_count",
        "emote_count",
    ):
        value = facts.get(key)
        if value is None:
            continue
        number = int(value)
        if number >= 10:
            markers.extend([str(number), _v5_number(number)])
    streak = facts.get("current_win_streak")
    if streak is not None and int(streak) > 1:
        markers.append(f"{int(streak)}-win")
    for key in ("favorite_card", "arena_name"):
        if facts.get(key):
            markers.append(str(facts[key]))
    seen = set()
    unique = []
    for marker in markers:
        lowered = marker.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique.append(marker)
    return unique


def _v5_message_has_profile_fact(message: str, markers: list[str]) -> bool:
    if not markers:
        return True
    lowered = (message or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def _v5_event_action_copy(detection_type: str, subject_name: str | None, summary: dict) -> str:
    name = subject_name or summary.get("clan_name") or "POAP KINGS"
    if detection_type == "member_joined":
        facts_text = _v5_join_facts_sentence(summary.get("profile_facts") or {})
        if facts_text:
            return f"Welcome to POAP KINGS, {name}! {facts_text} already caught my eye. Glad you're here."
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
    profile_facts = _v5_member_profile_facts(subject_tag) if detection_type == "member_joined" else {}
    if profile_facts:
        summary = dict(summary)
        summary["profile_facts"] = profile_facts
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
        "profile_facts": profile_facts,
        "profile_fact_markers": _v5_member_profile_fact_markers(profile_facts),
        "baseline": {
            "event_core": {
                "intent_type": metadata.get("intent_type"),
                "detection_type": detection_type,
                "summary": summary,
                "event_core_dedup_key": metadata.get("event_core_dedup_key"),
            },
            "profile_facts": profile_facts,
        },
    }


def _v5_event_clan_chat_context(spec: dict, metadata: dict | None) -> str:
    summary = (((metadata or {}).get("summary") or {}) if isinstance((metadata or {}).get("summary"), dict) else {})
    profile_facts = spec.get("profile_facts") or {}
    profile_line = ""
    if spec.get("objective") == "member_joined" and profile_facts:
        profile_line = (
            "\nMember joined welcome rule: mention at least one player-profile fact so the welcome "
            "feels researched, warm, and specific. Do not write a generic welcome."
            f"\nPlayer profile facts JSON: {json.dumps(profile_facts, sort_keys=True, default=str)}"
        )
    return (
        "V5 public event leader-action relay:\n"
        "Write ONE plain-text Clash Royale in-game clan chat message for a leader to paste.\n"
        "Keep it warm, specific, and natural for POAP KINGS clan chat.\n"
        f"Event type: {spec.get('objective')}\n"
        f"Target player name: {spec.get('target_player_name') or 'POAP KINGS'}\n"
        f"Target player tag: {spec.get('target_player_tag') or ''}\n"
        f"Event facts JSON: {json.dumps(summary, sort_keys=True, default=str)}\n"
        f"Fallback message: {spec.get('copy') or ''}"
        f"{profile_line}"
    )


async def _v5_event_clan_chat_copy(spec: dict, metadata: dict | None) -> tuple[str, dict]:
    fallback = spec.get("copy") or "POAP KINGS keeps building together."
    fallback_signed = sign_clan_chat_text(fallback, limit=CLASH_COPY_MAX_LENGTH)
    try:
        generated = await generate_clan_chat_copy(
            intent=f"v5_event_{spec.get('objective') or 'leader_action'}",
            context=_v5_event_clan_chat_context(spec, metadata),
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
        if (
            spec.get("objective") == "member_joined"
            and spec.get("profile_fact_markers")
            and not _v5_message_has_profile_fact(generated.messages[0], spec["profile_fact_markers"])
        ):
            return fallback_signed, {
                "used_fallback": True,
                "reason": "missing_profile_fact",
                "violations": ["missing_profile_fact"],
            }
        return generated.messages[0], {
            "used_fallback": bool(generated.used_fallback),
            "summary": generated.summary,
            "violations": generated.violations,
        }
    return fallback_signed, {"used_fallback": True, "reason": "empty_generation"}


async def _post_v5_event_leader_action(metadata: dict | None, sent_messages=None) -> bool:
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
    copy, copy_metadata = await _v5_event_clan_chat_copy(spec, metadata)
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


def _recent_delivered_v5_event_metadata(limit: int = 50) -> list[dict]:
    rows: list[dict] = []
    conn = db.get_connection()
    try:
        for row in conn.execute(
            """
            SELECT intent_key, intent_type, source_signal_key, source_signal_type,
                   target_channel_key, target_channel_id, payload_json, delivered_at
            FROM communication_intents
            WHERE workflow = 'v5-reactive'
              AND status = 'delivered'
              AND delivered_at >= datetime('now', '-7 days')
            ORDER BY delivered_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            metadata = dict(payload)
            metadata.setdefault("intent_type", row["intent_type"])
            metadata.setdefault("source_signal_key", row["source_signal_key"])
            metadata.setdefault("source_signal_type", row["source_signal_type"])
            metadata.setdefault("target_channel_key", row["target_channel_key"])
            metadata.setdefault("target_channel_id", row["target_channel_id"])
            metadata.setdefault("operational_intent_key", row["intent_key"])
            metadata.setdefault("delivered_at", row["delivered_at"])
            if _v5_event_detection_type(metadata) in _V5_EVENT_LEADER_ACTION_TYPES:
                rows.append(metadata)
    finally:
        conn.close()
    return rows


async def _post_missing_v5_event_leader_actions(limit: int = 50) -> int:
    count = 0
    for metadata in await asyncio.to_thread(_recent_delivered_v5_event_metadata, limit):
        try:
            if await _post_v5_event_leader_action(metadata):
                count += 1
        except Exception:
            log.exception(
                "v5 event leader-action backfill failed source=%s",
                _v5_event_source_key(metadata),
            )
    return count


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


async def _v5_reactive_tick():
    """Event-driven tick: ingest CR data -> advance Followers -> reactively post new
    communication intents (agent-composed). Replaces the v4 scheduled-awareness loop."""
    from event_core.live import service

    loop = asyncio.get_running_loop()
    result = await service.reactive_tick(loop, _v5_post)
    try:
        leader_actions_posted = await _post_missing_v5_event_leader_actions()
        if leader_actions_posted:
            result = {**result, "leader_actions_posted": leader_actions_posted}
    except Exception:
        log.exception("v5 event leader-action backfill scan failed")
    log.info("v5 reactive tick: %s", result)
    return result


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
        # v5 go-live: ingest current state + drain ALL intents (backlog + downtime
        # catch-up) once, WITHOUT posting, so reactive posting starts clean from the
        # next tick and never floods Discord with historical events.
        try:
            from event_core.live import service as _v5_service

            caught_up = await asyncio.to_thread(_v5_service.catch_up)
            log.info("v5 go-live catch-up: %s", caught_up)
        except Exception:
            log.exception("v5 catch-up failed (reactive posting may flood or lag)")
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
            posted_event_actions = await _post_missing_v5_event_leader_actions()
            if posted_event_actions:
                log.info("Posted %s missing v5 event leader-action card(s)", posted_event_actions)
        except Exception:
            log.exception("Startup v5 event leader-action backfill failed")
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
