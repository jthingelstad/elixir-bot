"""Shared utilities and remaining job executors."""

__all__ = [
    "_player_intel_refresh_minutes",
    "PLAYER_INTEL_REFRESH_MINUTES", "PLAYER_INTEL_REFRESH_HOURS",
    "WAR_POLL_MINUTE", "WAR_AWARENESS_MINUTE",
    "PLAYER_INTEL_BATCH_SIZE", "PLAYER_INTEL_STALE_HOURS",
    "PLAYER_INTEL_REQUEST_SPACING_SECONDS", "PLAYER_INTEL_REFRESH_JITTER_SECONDS",
    "CLANOPS_WEEKLY_REVIEW_DAY", "CLANOPS_WEEKLY_REVIEW_HOUR",
    "WEEKLY_RECAP_DAY", "WEEKLY_RECAP_HOUR",
    "_build_weekly_clanops_review", "_build_weekly_clan_recap_context",
    "_query_or_default", "_summarize_member_rows",
    "_build_ask_elixir_daily_insight_context",
    "_ask_elixir_daily_insight", "_clan_awareness_tick",
    "_war_poll_tick", "_war_awareness_tick", "_player_intel_refresh",
    "_clanops_weekly_review", "_weekly_clan_recap",
    "_clan_wars_intel_report",
]

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import discord
import cr_api
import db
import elixir_agent
import heartbeat
import prompts
from modules.poap_kings import site as poap_kings_site
from storage.contextual_memory import upsert_war_recap_memory, upsert_weekly_summary_memory
from runtime import app as _app
from runtime.channel_subagents import (
    build_subagent_memory_context,
    OPTIONAL_PROGRESSION_SIGNAL_TYPES,
)
from runtime.app import (
    CHICAGO,
    bot,
    log,
)
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id, _safe_create_task
from runtime import status as runtime_status
from runtime.system_signals import queue_startup_system_signals
from runtime.jobs._signals import (
    _channel_config_by_key,
    _deliver_signal_group,
    _format_weekly_recap_post,
    _load_live_clan_context,
    _mark_delivered_signals,
    _observation_signal_batches,
    _persist_signal_detector_cursors,
    _post_to_elixir,
    _progression_signal_batches,
    _publish_pending_system_signal_updates,
    _strip_weekly_recap_header,
)
from runtime.jobs._intel import _clan_wars_intel_report
from runtime.jobs._site import (
    _normalize_poap_kings_publish_result,
    _notify_poapkings_publish,
    _publish_poap_kings_site_or_raise,
)


def _player_intel_refresh_minutes() -> int:
    minutes = os.getenv("PLAYER_INTEL_REFRESH_MINUTES")
    if minutes:
        return max(1, int(minutes))
    legacy_hours = os.getenv("PLAYER_INTEL_REFRESH_HOURS")
    if legacy_hours:
        return max(1, int(float(legacy_hours) * 60))
    return 30


PLAYER_INTEL_REFRESH_MINUTES = _player_intel_refresh_minutes()
PLAYER_INTEL_REFRESH_HOURS = PLAYER_INTEL_REFRESH_MINUTES / 60
WAR_POLL_MINUTE = int(os.getenv("WAR_POLL_MINUTE", "0"))
WAR_AWARENESS_MINUTE = int(os.getenv("WAR_AWARENESS_MINUTE", "5"))
PLAYER_INTEL_BATCH_SIZE = int(os.getenv("PLAYER_INTEL_BATCH_SIZE", "5"))
PLAYER_INTEL_STALE_HOURS = int(os.getenv("PLAYER_INTEL_STALE_HOURS", "1"))
PLAYER_INTEL_REQUEST_SPACING_SECONDS = float(os.getenv("PLAYER_INTEL_REQUEST_SPACING_SECONDS", "2.0"))
PLAYER_INTEL_REFRESH_JITTER_SECONDS = int(os.getenv("PLAYER_INTEL_REFRESH_JITTER_SECONDS", "900"))
CLANOPS_WEEKLY_REVIEW_DAY = os.getenv("CLANOPS_WEEKLY_REVIEW_DAY", "fri")
CLANOPS_WEEKLY_REVIEW_HOUR = int(os.getenv("CLANOPS_WEEKLY_REVIEW_HOUR", "19"))
WEEKLY_RECAP_DAY = os.getenv("WEEKLY_RECAP_DAY", "mon")
WEEKLY_RECAP_HOUR = int(os.getenv("WEEKLY_RECAP_HOUR", "9"))


def _build_weekly_clanops_review(*args, **kwargs):
    return _app._build_weekly_clanops_review(*args, **kwargs)


def _build_weekly_clan_recap_context(*args, **kwargs):
    return _app._build_weekly_clan_recap_context(*args, **kwargs)


def _query_or_default(label: str, fn, default):
    try:
        return fn()
    except Exception as exc:
        log.warning("ask-elixir insight data unavailable for %s: %s", label, exc)
        return default


def _summarize_member_rows(rows, *, name_key="name", value_builder=None, limit=5):
    summary = []
    for row in (rows or [])[:limit]:
        name = row.get(name_key) or row.get("current_name") or row.get("member_ref") or row.get("tag")
        if not name:
            continue
        value = value_builder(row) if value_builder else None
        summary.append(f"{name} ({value})" if value else str(name))
    return summary


def _build_ask_elixir_daily_insight_context(clan, war):
    hot_streaks = _query_or_default(
        "hot_streaks",
        lambda: db.get_members_on_hot_streak(min_streak=4) or [],
        [],
    )
    favourite_cards = _query_or_default(
        "favourite_cards",
        lambda: db.get_clan_favourite_card_counts(limit=10) or [],
        [],
    )
    overlooked = _query_or_default(
        "overlooked_cards",
        lambda: db.get_clan_overlooked_cards(min_owners=3, min_level=14, battle_days=14, limit=10) or [],
        [],
    )
    played_cards = _query_or_default(
        "played_cards",
        lambda: db.get_clan_recently_played_cards(days=14, limit=20) or [],
        [],
    )

    lines = [
        "Write one short daily fun fact for #ask-elixir that teaches members something about a Clash Royale card.",
        "Pick a card from the lists below and teach something useful: a matchup, an elixir trade, a counter, a synergy, a mechanic, or a hidden interaction.",
        "The card lists are just hooks to pick from — do not mention levels, collections, or who owns what.",
        "Focus on gameplay: what the card does well, what beats it, what combos with it, or a non-obvious trick.",
        "Vary your picks — sometimes from popular clan cards, sometimes from overlooked ones, sometimes from cards the clan plays a lot.",
        "Use a playful opener like 'Did you know?', 'Fun fact', or 'Elixir noticed something...'.",
        "Do NOT write about clan wars, River Race, fame, or war participation.",
        "Do NOT mention card levels, who has a card maxed, or collection stats.",
        "Keep it to 1-3 short sentences.",
        "Do not turn it into a recap, reminder, call to action, leadership note, or war order.",
        "If today's data does not support a genuinely interesting insight, return null.",
    ]
    if played_cards:
        lines.extend([
            "",
            "=== CARDS THE CLAN IS PLAYING RIGHT NOW ===",
            ", ".join(row["card_name"] for row in played_cards),
        ])
    if favourite_cards:
        lines.extend([
            "",
            "=== CARDS CLAN MEMBERS LOVE (FAVOURITES) ===",
            ", ".join(row["card_name"] for row in favourite_cards),
        ])
    if overlooked:
        lines.extend([
            "",
            "=== CARDS NOBODY IN THE CLAN IS PLAYING ===",
            ", ".join(row["card_name"] for row in overlooked),
        ])
    if hot_streaks:
        lines.extend([
            "",
            "=== MEMBERS ON HOT STREAKS ===",
            "\n".join(
                f"- {item}"
                for item in _summarize_member_rows(
                    hot_streaks,
                    value_builder=lambda row: f"{row.get('current_streak') or 0} straight wins",
                )
            ),
        ])
    return "\n".join(lines)


async def _ask_elixir_daily_insight():
    runtime_status.mark_job_start("daily_clan_insight")
    try:
        channel_id = _get_singleton_channel_id("ask-elixir")
    except Exception as exc:
        runtime_status.mark_job_failure("daily_clan_insight", f"ask-elixir channel config error: {exc}")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("daily_clan_insight", "ask-elixir channel not found")
        return

    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.error("Ask Elixir daily insight refresh failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("daily_clan_insight", f"refresh failed: {exc}")
        return

    if not clan.get("memberList"):
        runtime_status.mark_job_success("daily_clan_insight", "no member data")
        return

    recent_posts = await asyncio.to_thread(
        db.list_channel_messages,
        channel.id,
        10,
        "assistant",
    )
    channel_config = _channel_config_by_key("ask-elixir")
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=[],
    )
    context = await asyncio.to_thread(_build_ask_elixir_daily_insight_context, clan, war)

    try:
        result = await asyncio.to_thread(
            elixir_agent.generate_channel_update,
            channel_config["name"],
            channel_config["subagent_key"],
            context,
            recent_posts=recent_posts,
            memory_context=memory_context,
            leadership=False,
        )
    except Exception as exc:
        log.error("Ask Elixir daily insight generation failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("daily_clan_insight", f"generation failed: {exc}")
        return

    if result is None:
        runtime_status.mark_job_success("daily_clan_insight", "no fresh insight")
        return

    result = await _app._apply_member_refs_to_result(result)
    posts = _app._entry_posts(result)
    if not posts:
        runtime_status.mark_job_success("daily_clan_insight", "no fresh insight")
        return

    await _post_to_elixir(channel, result)
    ch = _channel_msg_kwargs(channel)
    for index, post in enumerate(posts):
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel), "assistant", post,
            summary=result.get("summary") if index == 0 else None,
            **ch, workflow="ask-elixir",
            event_type="daily_clan_insight" if index == 0 else "daily_clan_insight_part",
            raw_json={"result": result, "context_kind": "daily_clan_insight"},
        )
    runtime_status.mark_job_success("daily_clan_insight", "daily insight published")

async def _clan_awareness_tick():
    """Recurring clan-awareness activity for non-war signals and routed clan-event outcomes."""
    runtime_status.mark_job_start("clan_awareness")

    try:
        await asyncio.to_thread(queue_startup_system_signals)

        # Run the clan-awareness tick — fetches data, snapshots, detects signals
        tick_result = await asyncio.to_thread(heartbeat.tick, include_war=False)
        if tick_result.clan.get("memberList"):
            _app._clear_cr_api_failure_alert_if_recovered()
        else:
            await _app._maybe_alert_cr_api_failure("clan awareness")
        signals = tick_result.signals

        if not signals:
            log.info("Clan awareness: no signals, nothing to post")
            runtime_status.mark_job_success("clan_awareness", "no signals")
            return

        log.info("Clan awareness: %d signals detected, routing outcomes", len(signals))

        # Use clan + war data fetched during heartbeat.tick()
        clan = tick_result.clan
        war = tick_result.war

        for signal in signals:
            await _deliver_signal_group([signal], clan, war)

        runtime_status.mark_job_success("clan_awareness", f"{len(signals)} signal(s) processed")

    except Exception as e:
        log.error("Clan awareness error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("clan_awareness", str(e))


async def _war_poll_tick():
    """Predictable hourly war ingest for live state and race-log storage."""
    runtime_status.mark_job_start("war_poll")
    try:
        ingest_result = await asyncio.to_thread(
            heartbeat.ingest_live_war_state,
            refresh_race_log=True,
        )
        war = (ingest_result or {}).get("war") or {}
        if war:
            _app._clear_cr_api_failure_alert_if_recovered()
        else:
            log.info("War poll: no live war data returned")
        detail = "war snapshot stored" if war else "no live war data"
        if ingest_result.get("race_log_refreshed"):
            detail = f"{detail}; river race log refreshed ({ingest_result.get('race_log_items', 0)} row(s) stored)"
        runtime_status.mark_job_success("war_poll", detail)
    except Exception as e:
        log.error("War poll error: %s", e, exc_info=True)
        await _app._maybe_alert_cr_api_failure("war poll")
        runtime_status.mark_job_failure("war_poll", str(e))


async def _war_awareness_tick():
    """Stored-war observer that routes River Race signals on a fixed cadence."""
    runtime_status.mark_job_start("war_awareness")
    try:
        detection_result = await asyncio.to_thread(
            heartbeat.detect_war_signals_from_storage,
        )
        signals = detection_result.signals

        if not signals:
            if detection_result.cursor_updates:
                await asyncio.to_thread(_persist_signal_detector_cursors, detection_result.cursor_updates)
            runtime_status.mark_job_success("war_awareness", "no war signals")
            return

        clan = detection_result.clan
        war = detection_result.war

        delivered_ok = True
        for signal_batch in _observation_signal_batches(signals):
            batch_ok = await _deliver_signal_group(signal_batch, clan, war)
            delivered_ok = delivered_ok and batch_ok

        if not delivered_ok:
            runtime_status.mark_job_failure("war_awareness", "one or more war signal batches failed")
            return

        if detection_result.cursor_updates:
            await asyncio.to_thread(_persist_signal_detector_cursors, detection_result.cursor_updates)

        if any(s.get("type") == "war_season_rollover" for s in signals):
            _safe_create_task(_clan_wars_intel_report(), name="clan_wars_intel_auto")

        runtime_status.mark_job_success("war_awareness", f"{len(signals)} war signal(s) processed")
    except Exception as e:
        log.error("War awareness error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("war_awareness", str(e))


async def _player_intel_refresh():
    """Refresh stored player profile and battle intelligence for a subset of active members."""
    runtime_status.mark_job_start("player_intel_refresh")
    try:
        clan = await asyncio.to_thread(cr_api.get_clan)
        _app._clear_cr_api_failure_alert_if_recovered()
    except Exception as e:
        log.error("Player intel refresh: clan fetch failed: %s", e)
        await _app._maybe_alert_cr_api_failure("player intel refresh")
        runtime_status.mark_job_failure("player_intel_refresh", f"clan fetch failed: {e}")
        return

    members = clan.get("memberList", [])
    if not members:
        log.info("Player intel refresh: no member data, skipping")
        runtime_status.mark_job_success("player_intel_refresh", "no member data")
        return

    await asyncio.to_thread(db.snapshot_members, members)
    war = await asyncio.to_thread(db.get_current_war_status) or {}

    targets = await asyncio.to_thread(
        db.get_player_intel_refresh_targets,
        PLAYER_INTEL_BATCH_SIZE,
        PLAYER_INTEL_STALE_HOURS,
    )
    if not targets:
        log.info("Player intel refresh: no stale targets")
        runtime_status.mark_job_success("player_intel_refresh", "no stale targets")
        return

    refreshed = 0
    progression_signals = []
    profile_failures = 0
    battle_log_failures = 0
    failed_targets = 0
    processing_failures = 0
    for target in targets:
        tag = target["tag"]
        try:
            profile_ok = False
            battle_log_ok = False
            profile = await asyncio.to_thread(cr_api.get_player, tag)
            if profile is not None:
                profile_ok = True
            else:
                profile_failures += 1
            if profile:
                profile_signals = await asyncio.to_thread(db.snapshot_player_profile, profile)
                if isinstance(profile_signals, list) and profile_signals:
                    progression_signals.extend(profile_signals)
            battle_log = await asyncio.to_thread(cr_api.get_player_battle_log, tag)
            if battle_log is not None:
                battle_log_ok = True
            else:
                battle_log_failures += 1
            if battle_log:
                battle_signals = await asyncio.to_thread(db.snapshot_player_battlelog, tag, battle_log)
                if isinstance(battle_signals, list) and battle_signals:
                    progression_signals.extend(battle_signals)
            if profile_ok or battle_log_ok:
                refreshed += 1
            else:
                failed_targets += 1
            await asyncio.sleep(PLAYER_INTEL_REQUEST_SPACING_SECONDS)
        except Exception as e:
            processing_failures += 1
            failed_targets += 1
            log.warning("Player intel refresh failed for %s: %s", tag, e)

    for signal_batch in _progression_signal_batches(progression_signals):
        await _deliver_signal_group(signal_batch, clan, war)

    if profile_failures or battle_log_failures:
        await _app._maybe_alert_cr_api_failure("player intel refresh")

    total_targets = len(targets)
    failure_summary = []
    if profile_failures:
        failure_summary.append(f"profile failures {profile_failures}")
    if battle_log_failures:
        failure_summary.append(f"battle log failures {battle_log_failures}")
    if failed_targets:
        failure_summary.append(f"full target failures {failed_targets}")
    if processing_failures:
        failure_summary.append(f"processing failures {processing_failures}")

    if refreshed == 0 and failure_summary:
        detail = f"refreshed 0 of {total_targets} member(s); " + "; ".join(failure_summary)
        log.error("Player intel refresh failed: %s", detail)
        runtime_status.mark_job_failure("player_intel_refresh", detail)
        return

    summary = f"refreshed {refreshed} of {total_targets} member(s)"
    if failure_summary:
        summary = f"{summary}; " + "; ".join(failure_summary)
        log.warning("Player intel refresh completed with partial failures: %s", summary)
    else:
        log.info("Player intel refresh complete: %s", summary)
    runtime_status.mark_job_success("player_intel_refresh", summary)


async def _clanops_weekly_review():
    runtime_status.mark_job_start("clanops_weekly_review")
    clanops_channels = prompts.discord_channels_by_workflow("clanops")
    if not clanops_channels:
        runtime_status.mark_job_failure("clanops_weekly_review", "no leadership channel configured")
        return

    target_config = clanops_channels[0]
    channel = bot.get_channel(target_config["id"])
    if not channel:
        runtime_status.mark_job_failure("clanops_weekly_review", "leadership channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("ClanOps weekly review refresh failed: %s", exc)

    review_content = await asyncio.to_thread(_build_weekly_clanops_review, clan, war)
    if not review_content:
        runtime_status.mark_job_success("clanops_weekly_review", "no review content")
        return

    await _post_to_elixir(channel, {"content": review_content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel), "assistant", review_content,
        **_channel_msg_kwargs(channel), workflow="clanops",
        event_type="weekly_clanops_review",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clanops_review",
        title="Weekly ClanOps Review",
        body=review_content,
        scope="leadership",
        tags=["weekly", "clanops", "review"],
        metadata={"channel_id": channel.id, "workflow": "clanops"},
    )
    runtime_status.mark_job_success("clanops_weekly_review", "weekly review posted")


async def _weekly_clan_recap():
    runtime_status.mark_job_start("weekly_clan_recap")
    try:
        recap_channel_id = _get_singleton_channel_id("weekly_digest")
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_clan_recap", f"weekly digest channel config error: {exc}")
        return

    channel = bot.get_channel(recap_channel_id)
    if not channel:
        runtime_status.mark_job_failure("weekly_clan_recap", "weekly digest channel not found")
        return

    clan = {}
    war = {}
    try:
        clan, war = await _load_live_clan_context()
    except Exception as exc:
        log.warning("Weekly clan recap refresh failed: %s", exc)

    recap_context = await asyncio.to_thread(_build_weekly_clan_recap_context, clan, war)
    recent_posts = await asyncio.to_thread(db.list_channel_messages, recap_channel_id, 5, "assistant")
    previous_message = _strip_weekly_recap_header(recent_posts[-1]["content"] if recent_posts else "")
    recap_text = await asyncio.to_thread(
        elixir_agent.generate_weekly_digest,
        recap_context,
        previous_message,
    )
    if not recap_text:
        runtime_status.mark_job_success("weekly_clan_recap", "no recap generated")
        return
    recap_post = _format_weekly_recap_post(recap_text)

    try:
        await _post_to_elixir(channel, {"content": recap_post})
    except discord.Forbidden as exc:
        detail = f"missing Discord permissions in #{getattr(channel, 'name', 'unknown')}"
        runtime_status.mark_job_failure("weekly_clan_recap", detail)
        raise RuntimeError(f"weekly recap post failed: {detail}") from exc
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel), "assistant", recap_post,
        **_channel_msg_kwargs(channel), workflow="announcements",
        event_type="weekly_clan_recap",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_clan_recap",
        title="Weekly Clan Recap",
        body=recap_post,
        scope="public",
        tags=["weekly", "recap", "clan-history"],
        metadata={"channel_id": channel.id, "workflow": "announcements"},
    )
    if poap_kings_site.site_enabled():
        members_payload = {
            "members": {
                "title": "Weekly Recap",
                "message": recap_text,
                "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "weekly_clan_recap",
            }
        }
        try:
            publish_result = await asyncio.to_thread(
                _publish_poap_kings_site_or_raise,
                members_payload,
                "Elixir POAP KINGS weekly recap sync",
            )
            publish_result = _normalize_poap_kings_publish_result(
                publish_result,
                members_payload,
            )
            await _notify_poapkings_publish("weekly-recap", publish_result=publish_result)
        except Exception as exc:
            log.error("Weekly recap site sync failed: %s", exc, exc_info=True)
            await _notify_poapkings_publish("weekly-recap", error_detail=str(exc))
            runtime_status.mark_job_failure("weekly_clan_recap", f"site sync failed: {exc}")
            return
    runtime_status.mark_job_success("weekly_clan_recap", "weekly recap posted")
