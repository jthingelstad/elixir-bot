"""Shared utilities and remaining job executors."""

__all__ = [
    "WAR_POLL_MINUTE", "WAR_AWARENESS_MINUTE",
    "LEADERSHIP_ACTION_SCAN_MINUTES", "LEADERSHIP_ACTION_SCAN_MAX_ACTIONS",
    "WEEKLY_RECAP_DAY", "WEEKLY_RECAP_HOUR",
    "_build_weekly_clan_recap_context",
    "_query_or_default", "_summarize_member_rows",
    "_build_ask_elixir_daily_insight_context",
    "_ask_elixir_daily_insight", "_clan_awareness_tick",
    "_war_poll_tick", "_war_awareness_tick",
    "_weekly_clan_recap",
    "_leadership_action_scan",
    "_weekly_discord_invite_relay",
    "_clan_wars_intel_report",
]

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone

import discord
import db
import elixir_agent
import heartbeat
import pytz
import prompts
from runtime.clan_chat_copy import (
    clip_clan_chat_text,
    generate_clan_chat_copy,
    role_action_clan_chat_copy,
)
from modules.poap_kings import site as poap_kings_site
from storage.contextual_memory import upsert_weekly_summary_memory
from storage.api_sentinel import EVENT_SENTINEL_SIGNAL_TYPE, SCHEMA_SENTINEL_SIGNAL_TYPE
from runtime.signal_lanes import (
    build_lane_memory_context,
    OPTIONAL_PROGRESSION_SIGNAL_TYPES,
)
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id, _safe_create_task
from runtime.leader_action_observability import post_leader_action_skip
from runtime.leader_action_policy import can_post_leader_action
from runtime.leader_action_ui import CLASH_COPY_MAX_LENGTH, LEADER_ACTION_UI_VERSION, post_leader_action_card
from runtime import status as runtime_status
from runtime.system_signals import queue_startup_system_signals
from runtime.jobs._signals import (
    _channel_config_by_key,
    _deliver_arena_relay_sidecars,
    _deliver_signal_group_via_awareness,
    _format_weekly_recap_post,
    _load_live_clan_context,
    _mark_delivered_signals,
    _persist_signal_detector_cursors,
    _post_to_elixir,
    _post_system_signal_updates,
    _publish_pending_system_signal_updates,
    _strip_weekly_recap_header,
)
from runtime.jobs._intel import _clan_wars_intel_report
from runtime.jobs._site import (
    _normalize_poap_kings_publish_result,
    _notify_poapkings_publish,
    _publish_poap_kings_site_or_raise,
    _publish_weekly_recap_blog_post,
)

CHICAGO = pytz.timezone("America/Chicago")
log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


_FINISH_TIME_MINUTE_RE = re.compile(r"\d{8}T(\d{2})(\d{2})(\d{2})")


def _derive_war_anchor_minute() -> int | None:
    """Read the most recent non-sentinel finishTime from the race log and
    return its minute-of-hour.

    Supercell stages River Race matchmaking in batches at season roll; the
    resulting "race close" moment is stable within a season and shifts when
    seasons roll. The minute component of the latest finishTime is our best
    proxy for the current season's anchor. Returns None if the log has no
    completed wars yet (first boot or fresh clan).
    """
    try:
        history = db.get_war_history(n=5)
    except Exception:
        log.warning("war-anchor derive: get_war_history failed", exc_info=True)
        return None
    for row in history or []:
        finish = (row.get("finish_time") or "").strip()
        if not finish or finish.startswith("19691231"):
            continue
        match = _FINISH_TIME_MINUTE_RE.match(finish)
        if match:
            return int(match.group(2))
    return None


_WAR_ANCHOR_MINUTE = _derive_war_anchor_minute()
# War-poll fires a couple of minutes AFTER the anchor so the CR API has
# flushed the state flip before we poll. War-awareness runs 5 min after
# that, matching the historical :00/:05 cadence.
_DEFAULT_WAR_POLL_MINUTE = (_WAR_ANCHOR_MINUTE + 2) % 60 if _WAR_ANCHOR_MINUTE is not None else 0
_DEFAULT_WAR_AWARENESS_MINUTE = (_WAR_ANCHOR_MINUTE + 7) % 60 if _WAR_ANCHOR_MINUTE is not None else 5
WAR_POLL_MINUTE = int(os.getenv("WAR_POLL_MINUTE", str(_DEFAULT_WAR_POLL_MINUTE)))
WAR_AWARENESS_MINUTE = int(os.getenv("WAR_AWARENESS_MINUTE", str(_DEFAULT_WAR_AWARENESS_MINUTE)))
if _WAR_ANCHOR_MINUTE is not None:
    log.info(
        "war schedule anchor: last finishTime minute=%02d → war-poll=:%02d war-awareness=:%02d",
        _WAR_ANCHOR_MINUTE, WAR_POLL_MINUTE, WAR_AWARENESS_MINUTE,
    )
else:
    log.info(
        "war schedule anchor: no finishTime in race log yet → war-poll=:%02d war-awareness=:%02d (defaults)",
        WAR_POLL_MINUTE, WAR_AWARENESS_MINUTE,
    )
LEADERSHIP_ACTION_SCAN_MINUTES = int(os.getenv("LEADERSHIP_ACTION_SCAN_MINUTES", "240"))
LEADERSHIP_ACTION_SCAN_MAX_ACTIONS = int(os.getenv("LEADERSHIP_ACTION_SCAN_MAX_ACTIONS", "2"))
KICK_RECOMMENDATION_FRESH_JOIN_GRACE_DAYS = int(os.getenv("KICK_RECOMMENDATION_FRESH_JOIN_GRACE_DAYS", "7"))
# How long a leader's decline suppresses re-proposing the same role action
# for the same member. Role situations change on roster timescales, so the
# default is 30 days — much longer than the 7-day unanswered-card dedup.
WEEKLY_DISCORD_INVITE_RELAY_DAY = os.getenv("WEEKLY_DISCORD_INVITE_RELAY_DAY", "sat")
WEEKLY_DISCORD_INVITE_RELAY_HOUR = int(os.getenv("WEEKLY_DISCORD_INVITE_RELAY_HOUR", "11"))
WEEKLY_RECAP_DAY = os.getenv("WEEKLY_RECAP_DAY", "mon")
WEEKLY_RECAP_HOUR = int(os.getenv("WEEKLY_RECAP_HOUR", "9"))
KICK_RECOMMENDATION_POLICY_CONTEXT = {
    "primary_signal": "inactivity_or_absence",
    "supporting_signals": ["donations", "war_participation"],
    "faq_alignment": [
        "Wars are encouraged, not required.",
        "Real life comes first when members communicate.",
        "Removal recommendations are for ghosting or inactivity without a heads-up.",
    ],
}
_AVAILABILITY_MEMORY_TERMS = (
    "away",
    "camping",
    "limited signal",
    "vacation",
    "travel",
    "travelling",
    "traveling",
    "unavailable",
    "offline",
    "break",
    "heads-up",
    "headsup",
    "real life",
)
_RETURN_MEMORY_TERMS = (
    "returned",
    "return from",
    "came back",
    "is back",
    "ready to participate",
    "active participation",
)


def _build_weekly_clan_recap_context(*args, **kwargs):
    return _runtime_app()._build_weekly_clan_recap_context(*args, **kwargs)


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
    event_windows = _query_or_default(
        "public_event_windows",
        lambda: db.summarize_events_by_window(windows=(7,), scope="public") or {},
        {},
    )
    recent_events = _query_or_default(
        "public_recent_events",
        lambda: db.list_recent_events(days=7, scope="public", limit=8) or [],
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
    if event_windows or recent_events:
        lines.extend([
            "",
            "=== RECENT PUBLIC EVENT PULSE (variety guardrail, not recap material) ===",
            "Use this only to avoid repeating yesterday's clan topic. Do not mention these events directly.",
        ])
        seven_day = (event_windows.get("7d") or {}) if isinstance(event_windows, dict) else {}
        by_type = seven_day.get("by_type") or {}
        if by_type:
            top_types = sorted(by_type.items(), key=lambda item: (-item[1], item[0]))[:5]
            lines.append("7d event types: " + ", ".join(f"{event_type}={count}" for event_type, count in top_types))
        if recent_events:
            lines.append(
                "recent events: "
                + "; ".join(
                    f"{event.get('event_type')}:{event.get('subject_key') or event.get('source_signal_key')}"
                    for event in recent_events[:5]
                )
            )
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

    channel = _bot().get_channel(channel_id)
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
        build_lane_memory_context,
        channel_config,
        signals=[],
    )
    context = await asyncio.to_thread(_build_ask_elixir_daily_insight_context, clan, war)

    try:
        result = await asyncio.to_thread(
            elixir_agent.generate_channel_update,
            channel_config["name"],
            channel_config["lane_key"],
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
        await _runtime_app()._maybe_alert_llm_failure("daily clan insight")
        runtime_status.mark_job_success("daily_clan_insight", "no fresh insight")
        return

    _runtime_app()._clear_llm_failure_alert_if_recovered()
    posts = _runtime_app()._entry_posts(result)
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

async def _revoke_member_role_for_leavers(signals: list[dict]) -> None:
    from runtime import onboarding

    for signal in signals:
        if signal.get("type") != "member_leave":
            continue
        tag = signal.get("tag")
        if not tag:
            continue
        name = signal.get("name") or tag
        try:
            ok, detail = await onboarding.remove_member_role_for_tag(
                tag, reason=f"Left clan: {name} ({tag})",
            )
        except Exception:
            log.exception("member_role_revoke_failed tag=%s", tag)
            continue
        log.info("member_role_revoke tag=%s ok=%s detail=%s", tag, ok, detail)


async def _clan_awareness_tick():
    """Recurring clan-awareness activity for non-war signals and routed clan-event outcomes."""
    runtime_status.mark_job_start("clan_awareness")

    try:
        await asyncio.to_thread(queue_startup_system_signals)

        # Run the clan-awareness tick — fetches data, snapshots, detects signals
        tick_result = await asyncio.to_thread(heartbeat.tick, include_war=False)
        if tick_result.clan.get("memberList"):
            _runtime_app()._clear_cr_api_failure_alert_if_recovered()
        else:
            await _runtime_app()._maybe_alert_cr_api_failure("clan awareness")
        signals = tick_result.signals

        if not signals:
            log.info("Clan awareness: no signals, nothing to post")
            runtime_status.mark_job_success("clan_awareness", "no signals")
            return

        log.info("Clan awareness: %d signals detected, routing outcomes", len(signals))

        # Use clan + war data fetched during heartbeat.tick()
        clan = tick_result.clan
        war = tick_result.war

        api_sentinel_types = {EVENT_SENTINEL_SIGNAL_TYPE, SCHEMA_SENTINEL_SIGNAL_TYPE}
        api_sentinel_signals = [
            signal for signal in signals
            if (signal.get("signal_type") or signal.get("type")) in api_sentinel_types
        ]
        awareness_signals = [
            signal for signal in signals
            if (signal.get("signal_type") or signal.get("type")) not in api_sentinel_types
        ]

        if api_sentinel_signals:
            await _post_system_signal_updates(api_sentinel_signals, clan, war)

        if not awareness_signals:
            runtime_status.mark_job_success(
                "clan_awareness",
                f"{len(api_sentinel_signals)} API sentinel signal(s) processed",
            )
            return

        # One agent turn sees all remaining signals together and emits a post plan.
        # Hard-post-floor signals fall back to per-signal on omission so
        # coverage is still guaranteed.
        failed = 0
        ok = await _deliver_signal_group_via_awareness(awareness_signals, clan, war, workflow="clan_awareness")
        if not ok:
            failed = len(awareness_signals)

        await _revoke_member_role_for_leavers(awareness_signals)

        if failed:
            runtime_status.mark_job_failure(
                "clan_awareness",
                f"{failed} of {len(awareness_signals)} signal(s) failed to deliver",
            )
        else:
            runtime_status.mark_job_success(
                "clan_awareness",
                f"{len(signals)} signal(s) processed",
            )

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
            _runtime_app()._clear_cr_api_failure_alert_if_recovered()
        else:
            log.info("War poll: no live war data returned")
        detail = "war snapshot stored" if war else "no live war data"
        if ingest_result.get("race_log_refreshed"):
            detail = f"{detail}; river race log refreshed ({ingest_result.get('race_log_items', 0)} row(s) stored)"
        runtime_status.mark_job_success("war_poll", detail)
    except Exception as e:
        log.error("War poll error: %s", e, exc_info=True)
        await _runtime_app()._maybe_alert_cr_api_failure("war poll")
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

        delivered_ok = await _deliver_signal_group_via_awareness(signals, clan, war, workflow="war_awareness")

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


async def _award_detection_tick():
    """Daily pass that grants season-wide clan awards.

    Season awards only land when a war season closes (every ~4-5 weeks), and
    War Participant accumulates at most once per battle day — so a daily
    check is more than enough. Runs the same detectors the war-awareness
    pipeline used to fire hourly, now isolated to its own activity and
    routed through the normal signal delivery path so new grants still
    surface in #clan-events.
    """
    from heartbeat._awards import (
        detect_season_awards,
        detect_war_participant_awards,
    )

    runtime_status.mark_job_start("award_detection")
    try:
        def _detect_all():
            signals = []
            signals.extend(detect_season_awards())
            signals.extend(detect_war_participant_awards())
            return signals

        signals = await asyncio.to_thread(_detect_all)
        if not signals:
            runtime_status.mark_job_success("award_detection", "no new awards")
            return

        clan, war = await _runtime_app()._load_live_clan_context()
        delivered_ok = await _deliver_signal_group_via_awareness(
            signals, clan, war, workflow="award_detection",
        )
        if not delivered_ok:
            runtime_status.mark_job_failure("award_detection", "award signal delivery failed")
            return
        runtime_status.mark_job_success(
            "award_detection", f"{len(signals)} new award signal(s)",
        )
    except Exception as e:
        log.error("Award detection error: %s", e, exc_info=True)
        runtime_status.mark_job_failure("award_detection", str(e))


def _leader_action_member_label(member: dict) -> str:
    return (
        member.get("member_ref")
        or member.get("current_name")
        or member.get("member_name")
        or member.get("name")
        or member.get("player_name")
        or member.get("tag")
        or member.get("player_tag")
        or "member"
    )


def _leader_action_member_tag(member: dict) -> str | None:
    return member.get("player_tag") or member.get("tag") or member.get("member_tag")


def _leader_action_activity_reason(member: dict) -> str | None:
    context = member.get("activity_context") or {}
    if not context.get("stale_activity"):
        return None
    try:
        battle_days = int(context.get("battle_days_ago")) if context.get("battle_days_ago") is not None else None
    except (TypeError, ValueError):
        battle_days = None
    try:
        login_days = int(context.get("login_days_ago")) if context.get("login_days_ago") is not None else None
    except (TypeError, ValueError):
        login_days = None
    if battle_days is not None and login_days is not None and battle_days >= 7 and login_days >= 7:
        return f"no activity in {min(battle_days, login_days)} days"
    if login_days is not None and login_days >= 7:
        return f"last seen {login_days} days ago"
    if battle_days is not None and battle_days >= 90:
        return "no battles in 90+ days"
    if battle_days is not None and battle_days >= 7:
        return f"no battles in {battle_days} days"
    return None


def _leader_action_reason(member: dict, *, promotion: bool) -> str:
    if promotion:
        bits = []
        rank = member.get("elder_donation_rank")
        target = member.get("elder_target_rank")
        rolling = member.get("rolling_donations_avg")
        if rank is not None and target is not None and rolling is not None:
            bits.append(f"rank {rank}/{target} on recent donations (avg {rolling}/week)")
        if member.get("donations") is not None:
            bits.append(f"{member.get('donations')} current donations")
        if member.get("war_races_played") is not None:
            bits.append(f"{member.get('war_races_played')} recent war races")
        if member.get("days_since_battle") is not None:
            bits.append(f"battle activity {member.get('days_since_battle')}d ago")
        return ", ".join(bits) or "inside Elder donation leaderboard"
    if member.get("reason"):
        bits = [str(member.get("reason"))]
        if member.get("war_races_played") is not None:
            bits.append(f"{member.get('war_races_played')} recent war races")
        if member.get("days_since_battle") is not None:
            bits.append(f"battle activity {member.get('days_since_battle')}d ago")
        return "; ".join(bits[:3])
    reasons = member.get("reasons") or []
    has_inactive_reason = any(reason.get("type") == "inactive" for reason in reasons if isinstance(reason, dict))
    bits = []
    if not has_inactive_reason:
        activity_reason = _leader_action_activity_reason(member)
        if activity_reason:
            bits.append(activity_reason)
    if reasons:
        bits.extend(
            reason.get("detail") or reason.get("type") or "needs review"
            for reason in reasons
            if isinstance(reason, dict)
        )
    if bits:
        return "; ".join(bits[:3])
    return "risk data says this member needs roster review"


CLAN_CHAT_ACTION_COPY_LIMIT = CLASH_COPY_MAX_LENGTH


def _kick_candidate_inactive_reason(member: dict) -> dict | None:
    for reason in member.get("reasons") or []:
        if isinstance(reason, dict) and reason.get("type") == "inactive":
            return reason
    return None


def _date_from_iso_prefix(value: str | None):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _kick_candidate_fresh_membership_reason(member: dict, *, conn=None) -> str | None:
    tag = _leader_action_member_tag(member)
    if not tag:
        return None
    try:
        summary = db.get_member_membership_summary(tag, conn=conn) or {}
    except Exception:
        log.warning("kick candidate membership lookup failed for %s", tag, exc_info=True)
        return None
    joined_date = _date_from_iso_prefix(summary.get("current_joined_at"))
    if not joined_date:
        return None
    age_days = (datetime.now(timezone.utc).date() - joined_date).days
    grace_days = max(0, KICK_RECOMMENDATION_FRESH_JOIN_GRACE_DAYS)
    if age_days < grace_days:
        return f"fresh_membership:{age_days}d<{grace_days}d"
    return None


def _availability_memory_matches(memory: dict) -> bool:
    tags = {str(tag or "").strip().lower() for tag in (memory.get("tags") or [])}
    text = " ".join(
        str(memory.get(key) or "")
        for key in ("title", "summary", "body")
    ).lower()
    if any(term in text for term in _RETURN_MEMORY_TERMS):
        return False
    return "availability" in tags or any(term in text for term in _AVAILABILITY_MEMORY_TERMS)


def _kick_candidate_availability_memory(member: dict, *, conn=None) -> dict | None:
    """Return an active availability memory that should suppress a kick card."""
    candidate_keys = [
        _leader_action_member_tag(member),
        member.get("member_ref"),
        member.get("name"),
        member.get("current_name"),
    ]
    seen = set()
    from memory_store import list_memories

    for key in candidate_keys:
        if not key:
            continue
        canon = db._canon_tag(str(key))
        if not canon or canon in seen:
            continue
        seen.add(canon)
        try:
            memories = list_memories(
                viewer_scope="leadership",
                filters={"member_tag": canon},
                limit=5,
                conn=conn,
            )
        except Exception:
            log.warning("kick candidate availability memory lookup failed for %s", canon, exc_info=True)
            continue
        for memory in memories or []:
            if _availability_memory_matches(memory):
                return memory
    return None


def _kick_candidate_ineligibility_reason(member: dict, *, conn=None) -> str | None:
    if not _kick_candidate_inactive_reason(member):
        return "no_inactivity_signal"
    fresh_membership = _kick_candidate_fresh_membership_reason(member, conn=conn)
    if fresh_membership:
        return fresh_membership
    availability_memory = _kick_candidate_availability_memory(member, conn=conn)
    if availability_memory:
        return f"availability_memory:{availability_memory.get('memory_id')}"
    return None


def _clip_clan_chat_text(text: str, *, limit: int = CLAN_CHAT_ACTION_COPY_LIMIT) -> str:
    return clip_clan_chat_text(text, limit=limit)


def _leader_action_clan_chat_copy(
    *,
    action_type: str,
    target_player_name: str | None,
    rationale: str,
) -> str | None:
    return role_action_clan_chat_copy(
        action_type=action_type,
        target_player_name=target_player_name,
        rationale=rationale,
        max_chars=CLAN_CHAT_ACTION_COPY_LIMIT,
    )


def _kick_candidate_priority(member: dict) -> tuple:
    reasons = member.get("reasons") or []
    inactive = next(
        (reason for reason in reasons if reason.get("type") == "inactive"),
        None,
    )
    overdue = 0
    if inactive:
        try:
            overdue = float(inactive.get("value") or 0) - float(inactive.get("threshold_days") or 0)
        except (TypeError, ValueError):
            overdue = 0
    reason_types = {reason.get("type") for reason in reasons if isinstance(reason, dict)}
    risk_score = int(member.get("risk_score") or len(reasons) or 0)
    reason_weight = (
        (3 if "inactive" in reason_types else 0)
        + (2 if "low_donations" in reason_types else 0)
        + (1 if "low_war_participation" in reason_types else 0)
    )
    return (
        0 if inactive else 1,
        -overdue,
        -risk_score,
        -reason_weight,
        member.get("clan_rank") if member.get("clan_rank") is not None else 999,
        (member.get("name") or member.get("current_name") or "").lower(),
    )


def _format_leader_action_card(
    action: dict,
    *,
    title: str,
    prompt_text: str,
    rationale: str,
    clan_chat_copy: str | None = None,
) -> str:
    action_id = action.get("action_id")
    objective = action.get("objective") or "leader_action"
    action_type = action.get("action_type") or ""
    icon = {
        "in_game_relay": "📣",
        "promotion_recommendation": "⬆️",
        "demotion_recommendation": "⬇️",
        "kick_recommendation": "🚪",
    }.get(action_type, "⚡")
    copy_instruction = (
        "📋 If done, copy the next message into Clash Royale after taking the action.\n\n"
        if clan_chat_copy
        else ""
    )
    return (
        f"**R{action_id} {icon} {title}**\n"
        f"🎯 `{objective}`\n"
        "🛠️ Action\n"
        f"```text\n{prompt_text}\n```\n"
        f"🧠 {rationale}\n\n"
        f"{copy_instruction}"
        "✅ done  ❌ decline  ↩️ reply with note"
    )


def _leader_action_candidate_action_key(
    *,
    action_type: str,
    objective: str,
    prompt_text: str,
    target_player_tag: str | None = None,
    case_id: int | None = None,
) -> str:
    proposal_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    parts = [
        action_type or "",
        objective or "",
        db._canon_tag(target_player_tag) if target_player_tag else "",
        str(case_id or ""),
        " ".join((prompt_text or "").split()),
        proposal_stamp,
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{action_type}:{digest}"


async def _post_leader_action_recommendation(
    channel,
    *,
    action_type: str,
    objective: str,
    title: str,
    prompt_text: str,
    rationale: str,
    target_player_tag: str | None = None,
    target_player_name: str | None = None,
    case_id: int | None = None,
    dedupe_hours: int = 168,
) -> bool:
    if target_player_tag and await asyncio.to_thread(
        db.has_recent_leader_action,
        action_type=action_type,
        target_player_tag=target_player_tag,
        objective=objective,
        within_hours=dedupe_hours,
    ):
        await post_leader_action_skip(
            source="leader_action_candidate_scan",
            action_type=action_type,
            reason=f"recent_action_within:{dedupe_hours}h",
            target_player_name=target_player_name,
            target_player_tag=target_player_tag,
            objective=objective,
            rationale=rationale,
        )
        return False
    allowed, reason = await asyncio.to_thread(can_post_leader_action, action_type=action_type)
    if not allowed:
        log.info("leader action candidate skipped by policy: %s", reason)
        await post_leader_action_skip(
            source="leader_action_candidate_scan",
            action_type=action_type,
            reason=f"policy:{reason}",
            target_player_name=target_player_name,
            target_player_tag=target_player_tag,
            objective=objective,
            rationale=rationale,
        )
        return False
    baseline = await asyncio.to_thread(
        db.build_leader_action_baseline,
        action_type=action_type,
        target_player_tag=target_player_tag,
    )
    if action_type == "kick_recommendation":
        baseline = dict(baseline or {})
        baseline["policy_context"] = KICK_RECOMMENDATION_POLICY_CONTEXT
    if case_id is not None:
        baseline = dict(baseline or {})
        baseline["decision_case_id"] = case_id
    clan_chat_copy = _leader_action_clan_chat_copy(
        action_type=action_type,
        target_player_name=target_player_name,
        rationale=rationale,
    )
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type=action_type,
        objective=objective,
        prompt_text=prompt_text,
        rationale=rationale,
        target_channel_key="arena-relay",
        target_channel_id=getattr(channel, "id", None),
        target_player_tag=target_player_tag,
        target_player_name=target_player_name,
        copy_original_text=clan_chat_copy,
        copy_current_text=clan_chat_copy,
        action_key=_leader_action_candidate_action_key(
            action_type=action_type,
            objective=objective,
            prompt_text=prompt_text,
            target_player_tag=target_player_tag,
            case_id=case_id,
        ),
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline=baseline,
        case_id=case_id,
    )
    if not action or action.get("source_message_id"):
        return False
    content = _format_leader_action_card(
        action,
        title=title,
        prompt_text=prompt_text,
        rationale=rationale,
        clan_chat_copy=clan_chat_copy,
    )
    sent_messages = await post_leader_action_card(
        channel,
        action,
        copy_messages=[clan_chat_copy] if clan_chat_copy else [],
    )
    first_message_id = getattr(sent_messages[0], "id", None) if sent_messages else None
    stored_content = "\n\n".join([content, clan_chat_copy] if clan_chat_copy else [content])
    raw_json = {
        "leader_action": action,
        "clan_chat_copy": clan_chat_copy,
    }
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        stored_content,
        summary=f"Leader action R{action.get('action_id')}: {title}",
        **_channel_msg_kwargs(channel),
        workflow="arena-relay",
        event_type=action_type,
        discord_message_id=first_message_id,
        raw_json=raw_json,
    )
    return True


LEADER_ACTION_CASE_ORDER = (
    "inactivity_review",
    "demotion_review",
    "promotion_review",
)


def _leader_action_case_identity(case: dict | None) -> str | None:
    case = case or {}
    case_key = case.get("case_key")
    if case_key:
        return str(case_key)
    case_type = case.get("case_type")
    tag = case.get("target_player_tag")
    if case_type and tag:
        return f"{case_type}:member:{db._canon_tag(tag)}"
    return None


def _leader_action_case_payload(
    *,
    case_type: str,
    member: dict,
    action_type: str,
    objective: str,
    title: str,
    prompt_text: str,
    rationale: str,
    case: dict | None,
) -> dict:
    tag = _leader_action_member_tag(member)
    label = _leader_action_member_label(member)
    merged = dict(case or {})
    merged.update({
        "case_type": case_type,
        "action_type": action_type,
        "objective": objective,
        "title": title,
        "prompt_text": prompt_text,
        "rationale": rationale,
        "target_player_tag": tag,
        "target_player_name": label,
        "member": dict(member or {}),
    })
    merged.setdefault("recommendation", prompt_text)
    merged.setdefault("case_key", _leader_action_case_identity(merged))
    return merged


async def _upsert_leader_action_candidate_case(
    *,
    case_type: str,
    member: dict,
    action_type: str,
    objective: str,
    title: str,
    prompt_text: str,
    rationale: str,
) -> dict:
    case = await asyncio.to_thread(
        db.upsert_member_review_case,
        case_type=case_type,
        member=member,
        title=title.replace("/", " ").title(),
        recommendation=prompt_text,
        rationale=rationale,
    )
    return _leader_action_case_payload(
        case_type=case_type,
        member=member,
        action_type=action_type,
        objective=objective,
        title=title,
        prompt_text=prompt_text,
        rationale=rationale,
        case=case,
    )


def _merge_due_case_with_refreshed_payload(due_case: dict, refreshed: dict | None) -> dict:
    merged = dict(refreshed or {})
    for key, value in (due_case or {}).items():
        if value is not None:
            merged[key] = value
    if not merged.get("prompt_text"):
        merged["prompt_text"] = merged.get("recommendation")
    if not merged.get("title") and merged.get("case_type"):
        merged["title"] = str(merged["case_type"]).replace("_", " ").title()
    # When there is no fresh detector payload, fall back to the member evidence
    # the case stored when it was opened, so a re-surfaced case can still run the
    # kick-eligibility check and render a card.
    if not isinstance(merged.get("member"), dict):
        state = merged.get("state") if isinstance(merged.get("state"), dict) else {}
        member = state.get("member") if isinstance(state.get("member"), dict) else None
        if member:
            merged["member"] = member
    return merged


async def _post_leader_action_case(channel, case: dict) -> bool:
    case_type = case.get("case_type")
    action_type = case.get("action_type")
    if not action_type:
        action_type = {
            "inactivity_review": "kick_recommendation",
            "demotion_review": "demotion_recommendation",
            "promotion_review": "promotion_recommendation",
        }.get(case_type)
    if not action_type:
        return False

    member = case.get("member") if isinstance(case.get("member"), dict) else {}
    target_player_tag = case.get("target_player_tag") or _leader_action_member_tag(member)
    target_player_name = case.get("target_player_name") or _leader_action_member_label(member)
    rationale = case.get("rationale") or ""
    prompt_text = case.get("prompt_text") or case.get("recommendation")
    if not prompt_text:
        if action_type == "kick_recommendation":
            prompt_text = f"Review {target_player_name or target_player_tag} for removal from the clan."
        elif action_type == "demotion_recommendation":
            prompt_text = f"Move {target_player_name or target_player_tag} from Elder back to Member."
        elif action_type == "promotion_recommendation":
            prompt_text = f"Promote {target_player_name or target_player_tag} to Elder."
    if not rationale:
        rationale = case.get("recommendation") or prompt_text or "Case is due for leader review."

    if action_type == "kick_recommendation":
        ineligible_reason = await asyncio.to_thread(_kick_candidate_ineligibility_reason, member)
        if ineligible_reason:
            await post_leader_action_skip(
                source="decision_case_scan",
                action_type=action_type,
                reason=f"ineligible:{ineligible_reason}",
                target_player_name=target_player_name,
                target_player_tag=target_player_tag,
                objective=case.get("objective") or "roster_health",
                rationale=rationale,
            )
            return False

    return await _post_leader_action_recommendation(
        channel,
        action_type=action_type,
        objective=case.get("objective") or "leader_action",
        title=case.get("title") or str(action_type).replace("_", " "),
        prompt_text=prompt_text,
        rationale=rationale,
        target_player_tag=target_player_tag,
        target_player_name=target_player_name,
        case_id=case.get("case_id"),
    )


async def _post_candidate_leader_action_recommendations(*, max_actions: int = 3) -> int:
    try:
        target_config = prompts.discord_singleton_lane("arena-relay")
    except Exception as exc:
        log.info("leader action candidates skipped: arena-relay unavailable: %s", exc)
        return 0
    channel = _bot().get_channel(target_config["id"])
    if not channel:
        log.warning("leader action candidates skipped: arena-relay channel not found")
        return 0

    try:
        promotions = await asyncio.to_thread(db.get_promotion_candidates)
        at_risk = await asyncio.to_thread(db.get_members_at_risk, require_war_participation=True)
    except Exception as exc:
        log.warning("weekly leader action data unavailable: %s", exc, exc_info=True)
        return 0

    posted = 0
    max_actions = max(1, int(max_actions or 1))
    refreshed_cases_by_type: dict[str, list[dict]] = {
        case_type: [] for case_type in LEADER_ACTION_CASE_ORDER
    }
    refreshed_cases_by_identity: dict[str, dict] = {}

    def remember_case(case: dict) -> None:
        identity = _leader_action_case_identity(case)
        if not identity:
            return
        refreshed_cases_by_identity[identity] = case
        refreshed_cases_by_type.setdefault(case.get("case_type"), []).append(case)

    at_risk_members = (at_risk or {}).get("members") or []
    kick_candidates = [
        member for member in at_risk_members
        if _kick_candidate_inactive_reason(member)
    ]
    ignored_count = len(at_risk_members) - len(kick_candidates)
    if ignored_count:
        log.info(
            "kick candidate scan ignored %s non-inactive at-risk member(s)",
            ignored_count,
        )
    kick_candidates = sorted(kick_candidates, key=_kick_candidate_priority)
    for member in kick_candidates:
        rationale = _leader_action_reason(member, promotion=False)
        label = _leader_action_member_label(member)
        prompt_text = f"Review {label} for removal from the clan."
        case_payload = await _upsert_leader_action_candidate_case(
            case_type="inactivity_review",
            member=member,
            action_type="kick_recommendation",
            objective="roster_health",
            title="kick/removal recommendation",
            prompt_text=prompt_text,
            rationale=rationale,
        )
        remember_case(case_payload)

    for member in (promotions.get("demotion_candidates") or []):
        label = _leader_action_member_label(member)
        rationale = _leader_action_reason(member, promotion=False)
        prompt_text = f"Move {label} from Elder back to Member."
        case_payload = await _upsert_leader_action_candidate_case(
            case_type="demotion_review",
            member=member,
            action_type="demotion_recommendation",
            objective="role_health",
            title="demotion recommendation",
            prompt_text=prompt_text,
            rationale=rationale,
        )
        remember_case(case_payload)

    for member in (promotions.get("recommended") or []):
        label = _leader_action_member_label(member)
        rationale = _leader_action_reason(member, promotion=True)
        prompt_text = f"Promote {label} to Elder."
        case_payload = await _upsert_leader_action_candidate_case(
            case_type="promotion_review",
            member=member,
            action_type="promotion_recommendation",
            objective="reward_and_retention",
            title="promotion recommendation",
            prompt_text=prompt_text,
            rationale=rationale,
        )
        remember_case(case_payload)

    for case_type in LEADER_ACTION_CASE_ORDER:
        if posted >= max_actions:
            return posted
        due_cases = await asyncio.to_thread(
            db.list_due_decision_cases,
            case_type=case_type,
            limit=max_actions,
        )
        # Case-first: due cases drive the cards. A deferred case re-surfaces when
        # its due_at passes even if the live detector no longer flags the member —
        # the leader chose to revisit it, so we honor that instead of silently
        # dismissing it (card dedupe, has_recent_leader_action/168h, prevents
        # re-post spam). An open case the detector no longer flags is left in
        # Situation for the awareness loop rather than carded with stale evidence.
        candidates = []
        for due_case in due_cases:
            identity = _leader_action_case_identity(due_case)
            refreshed = refreshed_cases_by_identity.get(identity)
            if refreshed is None and due_case.get("status") != db.CASE_DEFERRED:
                continue
            candidates.append(_merge_due_case_with_refreshed_payload(due_case, refreshed))
        if not candidates:
            candidates = refreshed_cases_by_type.get(case_type) or []
        for case in candidates:
            if posted >= max_actions:
                return posted
            if await _post_leader_action_case(channel, case):
                posted += 1
    return posted


async def _leadership_action_scan():
    runtime_status.mark_job_start("leadership_action_scan")
    posted = 0
    try:
        refreshed = await asyncio.to_thread(db.refresh_due_leader_action_outcomes)
        if refreshed:
            log.info("leadership action scan refreshed %s due action outcome(s)", len(refreshed))
            # Measured outcomes (role changes after a promotion, etc.) land
            # hours after the leader's decision — re-run
            # feedback synthesis for the affected action types so the lessons
            # include what actually happened, not just what the leader clicked.
            from runtime.leader_action_feedback import queue_leader_action_feedback_refresh

            for action_type in sorted({a.get("action_type") for a in refreshed if a.get("action_type")}):
                queue_leader_action_feedback_refresh(action_type)
        critical = await asyncio.to_thread(_leadership_scan_has_critical_war_action)
        allowed, reason = await asyncio.to_thread(can_post_leader_action, critical=critical)
        if not allowed:
            await post_leader_action_skip(
                source="leadership_action_scan",
                reason=f"policy:{reason}",
            )
            runtime_status.mark_job_success("leadership_action_scan", f"skipped: {reason}")
            return
        remaining = max(0, LEADERSHIP_ACTION_SCAN_MAX_ACTIONS - posted)
        if remaining:
            posted += await _post_candidate_leader_action_recommendations(max_actions=remaining)
    except Exception as exc:
        runtime_status.mark_job_failure("leadership_action_scan", str(exc))
        log.warning("leadership action scan failed: %s", exc, exc_info=True)
        return
    runtime_status.mark_job_success("leadership_action_scan", f"posted {posted} action(s)")


async def _weekly_discord_invite_relay():
    runtime_status.mark_job_start("weekly_discord_invite_relay")
    try:
        now = datetime.now(CHICAGO)
        week_key = now.strftime("%G-W%V")
        signal_key = f"discord_invite_reminder:{week_key}"
        signal = {
            "type": "discord_invite_reminder",
            "signal_key": signal_key,
            "signal_log_type": signal_key,
            "week_key": week_key,
        }
        processed = await _deliver_arena_relay_sidecars([signal], {}, {})
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_discord_invite_relay", str(exc))
        log.warning("weekly Discord invite relay failed: %s", exc, exc_info=True)
        return
    runtime_status.mark_job_success(
        "weekly_discord_invite_relay",
        f"processed {processed} arena-relay invite signal(s)",
    )


def _leadership_scan_has_critical_war_action() -> bool:
    state = db.get_current_war_day_state() or {}
    if state.get("phase") != "battle":
        return False
    remaining = state.get("time_left_seconds")
    if remaining is not None and int(remaining) <= 2 * 60 * 60:
        return True
    if (
        state.get("day_number") is not None
        and state.get("day_total") is not None
        and state.get("day_number") == state.get("day_total")
        and remaining is not None
        and int(remaining) <= 6 * 60 * 60
    ):
        return True
    return False


async def _weekly_clan_recap():
    runtime_status.mark_job_start("weekly_clan_recap")
    try:
        recap_channel_id = _get_singleton_channel_id("weekly_digest")
    except Exception as exc:
        runtime_status.mark_job_failure("weekly_clan_recap", f"weekly digest channel config error: {exc}")
        return

    channel = _bot().get_channel(recap_channel_id)
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
    if poap_kings_site.site_enabled():
        try:
            blog_result = await asyncio.to_thread(
                _publish_weekly_recap_blog_post,
                recap_text,
            )
            await _notify_poapkings_publish("weekly-recap-blog", publish_result=blog_result)
        except Exception as exc:
            log.error("Weekly recap blog post publish failed: %s", exc, exc_info=True)
            await _notify_poapkings_publish("weekly-recap-blog", error_detail=str(exc))
    try:
        await _weekly_story_relay_card(recap_text)
    except Exception:
        log.warning("weekly story relay card failed", exc_info=True)
    runtime_status.mark_job_success("weekly_clan_recap", "weekly recap posted")


async def _weekly_story_relay_card(recap_text: str) -> bool:
    """Offer the recap's best beat as a clan-chat relay card in #leader-actions.

    Most of the clan never reads Discord — the recap's strongest member
    story reaches them only if a leader pastes it into game chat. One card
    per week, leader-decided; earned frequency learns if these are unwanted.
    """
    try:
        channel_config = _channel_config_by_key("arena-relay")
    except Exception:
        log.info("weekly story relay skipped: arena-relay unavailable")
        return False
    relay_channel = _bot().get_channel(channel_config["id"])
    if not relay_channel:
        log.info("weekly story relay skipped: arena-relay channel not found")
        return False
    allowed, reason = await asyncio.to_thread(can_post_leader_action, action_type="in_game_relay")
    if not allowed:
        log.info("weekly story relay skipped by policy: %s", reason)
        return False

    context = (
        "Weekly story relay task:\n"
        "Compress the weekly recap below into ONE Clash Royale clan-chat message.\n"
        f"- Plain text only: no markdown, no links, no Discord emoji shortcodes, under {CLAN_CHAT_ACTION_COPY_LIMIT} characters.\n"
        "- Pick the single strongest member story and name the member(s) — recognition is the point.\n"
        "- Write it as something a leader would naturally say in clan chat, not as a broadcast.\n"
        "=== THIS WEEK'S RECAP ===\n"
        f"{recap_text}"
    )
    generated = await generate_clan_chat_copy(
        intent="weekly_story_relay",
        context=context,
        max_messages=1,
        max_chars=CLAN_CHAT_ACTION_COPY_LIMIT,
        forbidden_terms=("http://", "https://", "www.", "Discord"),
        metadata={"channel": channel_config["name"], "lane": channel_config.get("lane_key") or "arena-relay"},
    )
    copy = generated.messages[0] if generated and generated.messages else ""
    if not copy:
        log.info("weekly story relay skipped: no usable clan-chat copy")
        return False

    week_key = datetime.now(CHICAGO).strftime("%G-W%V")
    baseline = await asyncio.to_thread(
        db.build_leader_action_baseline,
        action_type="in_game_relay",
        target_player_tag=None,
    )
    action = await asyncio.to_thread(
        db.create_leader_action_recommendation,
        action_type="in_game_relay",
        objective="clan_story",
        prompt_text=f"Relay this week's story into clan chat: {copy}",
        rationale="Most members never read Discord; the recap's best story reaches them through game chat.",
        target_channel_key="arena-relay",
        target_channel_id=channel_config["id"],
        source_signal_key=f"weekly_story_relay:{week_key}",
        source_signal_type="weekly_story_relay",
        copy_original_text=copy,
        copy_current_text=copy,
        ui_version=LEADER_ACTION_UI_VERSION,
        baseline=baseline,
    )
    if not action or action.get("source_message_id"):
        return False
    sent_messages = await post_leader_action_card(relay_channel, action, copy_messages=[copy])
    if not isinstance(sent_messages, list):
        sent_messages = []
    first_message = sent_messages[0] if sent_messages else None
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(relay_channel), "assistant", copy,
        summary=f"Leader action R{action.get('action_id')}: weekly story relay",
        channel_id=channel_config["id"],
        channel_name=getattr(relay_channel, "name", "arena-relay"),
        channel_kind=str(getattr(relay_channel, "type", "text")),
        workflow="arena-relay",
        event_type="weekly_story_relay",
        discord_message_id=getattr(first_message, "id", None),
        raw_json={"leader_action": action},
    )
    return True
