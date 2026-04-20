"""Signal delivery pipeline and helpers."""

__all__ = [
    "_WEEKLY_RECAP_HEADER_RE", "_post_to_elixir", "_load_live_clan_context",
    "_channel_config_by_key", "_signal_group_needs_recap_memory",
    "_build_outcome_context", "_mark_signal_group_completed", "_post_signal_memory",
    "_deliver_signal_outcome", "_deliver_signal_group",
    "_deliver_awareness_post", "_deliver_awareness_post_plan",
    "_deliver_signal_group_via_awareness",
    "_strip_weekly_recap_header", "_format_weekly_recap_post",
    "_progression_signal_batches",
    "_system_signal_updates", "_store_recap_memories_for_signal_batch",
    "_build_system_signal_context", "_preauthored_system_signal_result",
    "_post_system_signal_updates", "_publish_pending_system_signal_updates",
    "_mark_delivered_signals", "_persist_signal_detector_cursors",
]

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("elixir")

import db
import elixir_agent
import prompts
from storage.contextual_memory import upsert_race_streak_memory, upsert_war_recap_memory
from runtime import app as _app
from runtime.channel_subagents import (
    build_subagent_memory_context,
    is_leadership_only_signal,
    maybe_upsert_signal_memory,
    CLAN_RECORD_SIGNAL_TYPES,
    OPTIONAL_PROGRESSION_SIGNAL_TYPES,
    SEASON_AWARDS_SIGNAL_TYPES,
    plan_signal_outcomes,
    signal_source_key,
)
from runtime.app import (
    CHICAGO,
    bot,
    log,
)
from runtime.helpers import _channel_scope, _get_singleton_channel_id
from runtime import status as runtime_status
from runtime.system_signals import queue_startup_system_signals


_WEEKLY_RECAP_HEADER_RE = re.compile(r"^\s*[*#_`\s]*weekly recap\b", re.IGNORECASE)


async def _post_to_elixir(*args, **kwargs):
    return await _app._post_to_elixir(*args, **kwargs)


async def _load_live_clan_context(*args, **kwargs):
    return await _app._load_live_clan_context(*args, **kwargs)


def _channel_config_by_key(channel_key: str) -> dict:
    config = prompts.discord_channels_by_subagent().get(channel_key)
    if not config:
        raise RuntimeError(f"channel subagent not configured: {channel_key}")
    return config


def _signal_group_needs_recap_memory(signals):
    recap_types = {"war_battle_day_complete", "war_week_complete", "war_completed", "war_season_complete"}
    return any((signal.get("type") in recap_types) for signal in (signals or []))



def _extract_race_standings_summary(war):
    """Extract a compact race standings summary from the raw war API payload.

    The raw war JSON includes full participant arrays for all 5 clans, which can
    be thousands of lines. This extracts just the competitive picture: rank, name,
    fame, and fame gap vs. our clan.
    """
    clans = (war or {}).get("clans") or []
    if not clans:
        return []
    our_tag = None
    clan_obj = (war or {}).get("clan") or {}
    if clan_obj.get("tag"):
        our_tag = clan_obj["tag"].strip("#").upper()
    ranked = sorted(
        clans,
        key=lambda c: (c.get("fame") or 0, c.get("repairPoints") or 0),
        reverse=True,
    )
    our_fame = None
    for c in ranked:
        tag = (c.get("tag") or "").strip("#").upper()
        if our_tag and tag == our_tag:
            our_fame = c.get("fame") or 0
            break
    lines = []
    for rank, c in enumerate(ranked, start=1):
        tag = (c.get("tag") or "").strip("#").upper()
        is_us = our_tag and tag == our_tag
        fame = c.get("fame") or 0
        name = c.get("name") or "Unknown"
        gap = ""
        if our_fame is not None and not is_us:
            diff = fame - our_fame
            gap = f" ({diff:+,} vs us)" if diff != 0 else " (tied)"
        marker = " ← POAP KINGS" if is_us else ""
        lines.append(f"  #{rank} {name}: {fame:,} fame{gap}{marker}")
    return lines


def _build_compact_war_context(war):
    """Build a compact war context summary instead of dumping the full raw JSON.

    The raw currentriverrace API response includes full participant arrays for
    all 5 clans (50+ members each with fame, decks, repairs), which can easily
    exceed context limits. This extracts only what the LLM needs for reasoning.
    """
    war = war or {}
    lines = []
    # War state metadata
    state = war.get("state")
    if state:
        lines.append(f"war_state: {state}")
    clan = war.get("clan") or {}
    if clan:
        lines.append(
            f"our_clan: {clan.get('name')} | fame {clan.get('fame', 0):,} | "
            f"repair {clan.get('repairPoints', 0):,} | score {clan.get('clanScore', 0):,}"
        )
        finish_time = clan.get("finishTime")
        if finish_time:
            lines.append(f"finish_time: {finish_time}")
        participants = clan.get("participants") or []
        if participants:
            lines.append(f"our_participant_count: {len(participants)}")
    # Period logs count (useful context but not raw data)
    period_logs = war.get("periodLogs") or []
    if period_logs:
        lines.append(f"period_logs_available: {len(period_logs)} week(s)")
    if not lines:
        lines.append("(no war data available)")
    return lines


def _build_river_race_insight_layer(signals):
    """Extract high-value derived fields from river-race signals into a readable insight block."""
    lines = []
    for sig in (signals or []):
        # Lead pressure and narrative (from _battle_lead_payload)
        if sig.get("lead_pressure"):
            lines.append(f"lead_pressure: {sig['lead_pressure']}")
        if sig.get("lead_story"):
            lines.append(f"lead_story: {sig['lead_story']}")
        if sig.get("lead_call_to_action"):
            lines.append(f"lead_call_to_action: {sig['lead_call_to_action']}")

        # Rank movement — the change matters more than the current state
        if sig.get("gained_ground"):
            lines.append(f"rank_movement: gained ground (was #{sig.get('previous_rank')} -> now #{sig.get('race_rank')})")
        elif sig.get("lost_ground"):
            lines.append(f"rank_movement: lost ground (was #{sig.get('previous_rank')} -> now #{sig.get('race_rank')})")
        elif sig.get("race_rank") is not None and "gained_ground" not in sig:
            lines.append(f"rank_movement: holding at #{sig['race_rank']}")

        # Engagement rates — percentages are more insightful than raw counts
        if sig.get("engagement_pct") is not None:
            lines.append(
                f"engagement: {sig['completion_pct']}% finished all decks, "
                f"{sig['engagement_pct']}% have battled, "
                f"{100 - sig['engagement_pct']}% untouched"
            )
        elif sig.get("total_participants"):
            total = sig["total_participants"]
            finished = sig.get("finished_count") or 0
            engaged = sig.get("engaged_count") or 0
            lines.append(
                f"engagement: {round(100 * finished / max(1, total))}% finished all decks, "
                f"{round(100 * engaged / max(1, total))}% have battled, "
                f"{round(100 * (total - engaged) / max(1, total))}% untouched"
            )

        # Pace projection
        if sig.get("pace_status"):
            fame_target = sig.get("fame_target") or "10,000"
            lines.append(f"pace_status: {sig['pace_status']} (finish line: {fame_target:,} fame)" if isinstance(fame_target, int) else f"pace_status: {sig['pace_status']}")

        # Time pressure
        hours_remaining = sig.get("hours_remaining") or sig.get("checkpoint_hours_remaining")
        if hours_remaining is not None:
            lines.append(f"hours_remaining: {hours_remaining}")

        # Trophy stakes if known
        if sig.get("trophy_stakes_text"):
            lines.append(f"trophy_stakes: {sig['trophy_stakes_text']}")

    # Deduplicate in case multiple signals contributed the same fields
    seen = set()
    unique = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique


def _build_player_insight_context(tag):
    """Load recent form and trend data for a player to enrich progress signals."""
    lines = []
    try:
        form = db.get_member_recent_form(tag)
        if form:
            parts = [f"recent_form: {form.get('form_label', 'unknown')}"]
            if form.get("summary"):
                parts.append(f"({form['summary']})")
            lines.append(" ".join(parts))
            if form.get("current_streak") and form.get("current_streak_type"):
                lines.append(f"current_streak: {form['current_streak']}{form['current_streak_type']}")
    except Exception:
        log.warning("compare_member_form failed for %s", tag, exc_info=True)
    try:
        trend = db.compare_member_trend_windows(tag, window_days=7)
        if trend:
            current = trend.get("current") or {}
            previous = trend.get("previous") or {}
            ct = current.get("trophies") or {}
            pt = previous.get("trophies") or {}
            if ct.get("delta") is not None:
                prev_label = f" (prior 7 days: {pt['delta']:+d})" if pt.get("delta") is not None else ""
                lines.append(f"trophy_trend_7d: {ct['delta']:+d}{prev_label}")
            ca = current.get("battle_activity") or {}
            pa = previous.get("battle_activity") or {}
            if ca.get("battles"):
                prev_label = f" (prior: {pa.get('battles', 0)})" if pa.get("battles") else ""
                lines.append(f"battles_this_week: {ca['battles']}{prev_label}")
    except Exception:
        log.warning("compare_member_trend_windows failed for %s", tag, exc_info=True)
    return lines


def _build_outcome_context(outcome, signals, clan, war):
    channel_key = outcome["target_channel_key"]
    first = (signals or [{}])[0]
    lines = [
        f"Target channel subagent: {channel_key}",
        f"Intent: {outcome['intent']}",
        "Write the final post for that destination only.",
        "Do not mention other channels or other internal outcomes from the same signal.",
        "",
        "Signals:",
        json.dumps(signals or [], indent=2, default=str),
    ]
    # Ambient time/phase awareness — available to every channel, not just war
    # checkpoints. Lets non-checkpoint posts narrate "six hours left, 180 fame
    # back" without waiting for a tripwire to fire.
    try:
        from heartbeat import build_situation_time
        situation_time = build_situation_time()
    except Exception:
        log.warning("build_situation_time failed", exc_info=True)
        situation_time = None
    if situation_time:
        lines.extend([
            "",
            "=== TIME / PHASE (current ambient context, use narratively) ===",
            json.dumps(situation_time, indent=2, default=str),
        ])
    if channel_key == "river-race":
        lines.extend([
            "",
            "Focus on momentum, change, and what the clan cannot easily see in-game.",
        ])
        # Build insight layer from signal fields the LLM should lead with
        insight_lines = _build_river_race_insight_layer(signals)
        if insight_lines:
            lines.extend(["", "=== INSIGHT LAYER (lead with this) ==="] + insight_lines)
        # Race standings — extracted compactly so they never get truncated
        standings_lines = _extract_race_standings_summary(war)
        if standings_lines:
            lines.extend(["", "=== RACE STANDINGS ==="] + standings_lines)
        # Compact war context instead of raw JSON dump
        lines.extend([
            "",
            "=== BACKGROUND DATA (for reasoning, do not restate as-is) ===",
        ])
        lines.extend(_build_compact_war_context(war))
    elif channel_key == "player-progress":
        lines.extend([
            "",
            "Focus on the player's achievement and why it is worth celebrating.",
        ])
        # Enrich with form and trend data so the LLM can interpret, not just restate
        tag = first.get("tag")
        if tag:
            insight_lines = _build_player_insight_context(tag)
            if insight_lines:
                lines.extend(["", "=== PLAYER CONTEXT (use to interpret the achievement) ==="] + insight_lines)
    elif channel_key == "trophy-road":
        lines.extend([
            "",
            "Focus on the *push happening right now* — non-war battle activity. Investigate before you post: when a streak names a player, "
            "use cr_api(aspect='player_battles') to see who they were beating, then cr_api(aspect='player') on a notable opponent if it sharpens the post.",
        ])
        tag = first.get("tag")
        if tag:
            insight_lines = _build_player_insight_context(tag)
            if insight_lines:
                lines.extend(["", "=== PLAYER CONTEXT (current form / streak / trend) ==="] + insight_lines)
    elif channel_key == "clan-events":
        has_likely_kick = any(s.get("likely_kicked") for s in (signals or []))
        is_clan_record = any(
            (s.get("type") in CLAN_RECORD_SIGNAL_TYPES) for s in (signals or [])
        )
        if has_likely_kick:
            lines.extend([
                "",
                "This member was likely removed from the clan due to inactivity.",
                "Keep the message brief and neutral. Do not write a warm farewell or thank them for contributions.",
                "A simple factual note that the member is no longer with the clan is enough.",
            ])
        elif is_clan_record:
            lines.extend([
                "",
                "This is an all-time clan record — the highest the metric has ever been since records began, not a seasonal peak.",
                "Do NOT call it a 'season high', 'season record', 'weekly high', or any other time-windowed framing. It is a lifetime clan high.",
                "Do NOT frame this as a personal achievement — the metric belongs to the clan, not any player.",
                "Report what the metric is, the previous record, the new record, and the date. Keep it short and celebratory.",
            ])
        else:
            lines.extend([
                "",
                "Focus on the communal clan moment and keep the tone welcoming and proud.",
            ])
    elif channel_key == "leader-lounge":
        lines.extend([
            "",
            "This is a leadership-facing factual note. Include useful operational context, not public hype.",
        ])
        tag = first.get("tag")
        if tag:
            try:
                profile = db.get_member_profile(tag)
            except Exception:
                profile = None
            if profile:
                lines.extend([
                    "Member profile context:",
                    json.dumps(profile, indent=2, default=str),
                ])
    else:
        lines.extend([
            "",
            "Current clan data:",
            json.dumps(clan or {}, indent=2, default=str),
        ])
    return "\n".join(lines)


async def _mark_signal_group_completed(signals):
    await asyncio.to_thread(_mark_delivered_signals, signals)
    for signal in signals or []:
        if signal.get("signal_key"):
            await asyncio.to_thread(db.mark_system_signal_announced, signal["signal_key"])


async def _post_signal_memory(body, outcome, signals):
    """Fire-and-forget: extract inference facts from signal-driven posts."""
    try:
        from agent.memory_tasks import extract_inference_facts, save_inference_facts

        context_label = f"signal:{outcome.get('intent', 'unknown')} in #{outcome.get('target_channel_key', 'unknown')}"
        facts = await asyncio.to_thread(extract_inference_facts, body, context_label)
        if facts:
            channel_id = outcome.get("target_channel_id")
            await asyncio.to_thread(save_inference_facts, facts, channel_id)
    except Exception:
        log.warning("_post_signal_memory failed", exc_info=True)


async def _deliver_signal_outcome(outcome, signals, clan, war):
    existing = await asyncio.to_thread(
        db.get_signal_outcome,
        outcome["source_signal_key"],
        outcome["target_channel_key"],
        outcome["intent"],
    )
    if existing and existing.get("delivery_status") == "delivered":
        return True

    await asyncio.to_thread(
        db.upsert_signal_outcome,
        outcome["source_signal_key"],
        outcome["source_signal_type"],
        outcome["target_channel_key"],
        outcome["target_channel_id"],
        outcome["intent"],
        required=outcome.get("required", True),
        delivery_status="planned",
        payload=outcome.get("payload"),
    )

    channel_config = _channel_config_by_key(outcome["target_channel_key"])
    channel = bot.get_channel(channel_config["id"])
    if not channel:
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="failed",
            payload=outcome.get("payload"),
            error_detail="channel not found",
        )
        return False

    channel_id = channel_config["id"]
    recent_posts = await asyncio.to_thread(
        db.list_channel_messages,
        channel_id,
        10,
        "assistant",
    )
    memory_context = await asyncio.to_thread(
        build_subagent_memory_context,
        channel_config,
        signals=signals,
    )
    # Tournament, war-recap, and season-awards signals get dedicated
    # generators + self-contained context (no war state, no river-race
    # context, no clan dump). Keeps the LLM from confabulating ground-
    # truth details from RAG memory or ambient context.
    from runtime.channel_subagents import TOURNAMENT_SIGNAL_TYPES, WAR_RECAP_SIGNAL_TYPES
    is_tournament_batch = bool(signals) and all(
        (s or {}).get("type") in TOURNAMENT_SIGNAL_TYPES for s in signals
    )
    is_war_recap_batch = bool(signals) and all(
        (s or {}).get("type") in WAR_RECAP_SIGNAL_TYPES for s in signals
    )
    is_season_awards_batch = bool(signals) and all(
        (s or {}).get("type") in SEASON_AWARDS_SIGNAL_TYPES for s in signals
    )
    if is_tournament_batch or is_war_recap_batch or is_season_awards_batch:
        context = None  # unused; dedicated path builds its own user message
    else:
        context = _build_outcome_context(outcome, signals, clan, war)

    preauthored_result = None
    if len(signals) == 1 and signals[0].get("signal_key"):
        preauthored_result = _preauthored_system_signal_result(signals[0])

    try:
        channel_name = getattr(channel, "name", None)
        if not isinstance(channel_name, str):
            channel_name = None
        channel_kind = getattr(channel, "type", None)
        if channel_kind is not None:
            channel_kind = str(channel_kind)
        if preauthored_result is not None:
            result = preauthored_result
        elif is_tournament_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_tournament_update,
                signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        elif is_war_recap_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_war_recap_update,
                signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        elif is_season_awards_batch:
            result = await asyncio.to_thread(
                elixir_agent.generate_season_awards_post,
                signals,
                recent_posts=recent_posts,
                memory_context=memory_context,
            )
        else:
            result = await asyncio.to_thread(
                elixir_agent.generate_channel_update,
                channel_config["name"],
                channel_config["subagent_key"],
                context,
                recent_posts=recent_posts,
                memory_context=memory_context,
                leadership=(channel_config["memory_scope"] == "leadership"),
            )
        if result is None:
            await _app._maybe_alert_llm_failure("channel update")
            status = "failed" if outcome.get("required", True) else "skipped"
            await asyncio.to_thread(
                db.upsert_signal_outcome,
                outcome["source_signal_key"],
                outcome["source_signal_type"],
                outcome["target_channel_key"],
                outcome["target_channel_id"],
                outcome["intent"],
                required=outcome.get("required", True),
                delivery_status=status,
                payload=outcome.get("payload"),
                error_detail="generator returned null",
                mark_attempt=True,
            )
            return status == "skipped"

        _app._clear_llm_failure_alert_if_recovered()
        posts = _app._entry_posts(result)
        await _post_to_elixir(channel, result)
        summary = result.get("summary")
        event_type = result.get("event_type") or outcome["intent"]
        for index, post in enumerate(posts):
            post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
            post_event_type = event_type if index == 0 else f"{event_type}_part"
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel),
                "assistant",
                post,
                summary=post_summary,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_kind=channel_kind,
                workflow=channel_config["subagent_key"],
                event_type=post_event_type,
                raw_json={
                    "source_signal_key": outcome["source_signal_key"],
                    "intent": outcome["intent"],
                    "target_channel_key": outcome["target_channel_key"],
                    "result": result,
                },
            )
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="delivered",
            payload={"result": result, "signals": signals},
            mark_attempt=True,
            delivered=True,
        )
        body = "\n\n".join(posts)
        await asyncio.to_thread(
            maybe_upsert_signal_memory,
            source_signal_key=outcome["source_signal_key"],
            signal_type=(signals[0].get("type") or outcome["source_signal_type"]),
            body=body,
            outcome=outcome,
            signals=signals,
        )
        # Store structured observation facts directly from signal data
        from agent.memory_tasks import store_observation_facts
        await asyncio.to_thread(store_observation_facts, signals, channel_id)
        if channel_config["subagent_key"] == "river-race" and _signal_group_needs_recap_memory(signals):
            await asyncio.to_thread(_store_recap_memories_for_signal_batch, signals, posts, channel_id)
        from runtime.helpers._common import _safe_create_task
        _safe_create_task(
            _post_signal_memory(body, outcome, signals),
            name="signal_memory",
        )
        return True
    except Exception as exc:
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            outcome["source_signal_key"],
            outcome["source_signal_type"],
            outcome["target_channel_key"],
            outcome["target_channel_id"],
            outcome["intent"],
            required=outcome.get("required", True),
            delivery_status="failed",
            payload=outcome.get("payload"),
            error_detail=str(exc),
            mark_attempt=True,
        )
        log.error("Signal outcome delivery failed for %s/%s: %s", outcome["source_signal_key"], outcome["target_channel_key"], exc, exc_info=True)
        return False


async def _deliver_signal_group(signals, clan, war):
    outcomes = plan_signal_outcomes(signals)
    if not outcomes:
        return False
    results = []
    for outcome in outcomes:
        delivered = await _deliver_signal_outcome(outcome, signals, clan, war)
        results.append(delivered)
    rows = await asyncio.to_thread(db.list_signal_outcomes, outcomes[0]["source_signal_key"])
    if rows and all(row.get("delivery_status") in {"delivered", "skipped"} for row in rows):
        await _mark_signal_group_completed(signals)
        return True
    return all(results)


# ---------------------------------------------------------------------------
# Awareness-loop delivery (Phase 4)
# ---------------------------------------------------------------------------

async def _deliver_awareness_post(post: dict, signals: list[dict]) -> bool:
    """Deliver one post from an awareness post-plan to its target channel.

    Reuses the existing Discord write path (`_post_to_elixir`) and message
    log so downstream consumers (memory extraction, recap storage) keep
    working unchanged.
    """
    from runtime.situation import CHANNEL_LANES
    channel_key = (post.get("channel") or "").strip()
    if channel_key not in CHANNEL_LANES:
        log.warning("awareness post rejected: unknown channel %r", channel_key)
        return False
    leads_with = (post.get("leads_with") or "").strip()
    if leads_with and leads_with not in CHANNEL_LANES[channel_key]:
        log.warning(
            "awareness post rejected: leads_with=%r not allowed on channel=%r (allowed=%s)",
            leads_with, channel_key, sorted(CHANNEL_LANES[channel_key]),
        )
        return False

    # Reject posts with empty covers when the tick had signals to consider.
    # Quiet-deadline ticks (no signals, near battle deadline) may legitimately
    # produce a post that covers nothing.
    covers = list(post.get("covers_signal_keys") or [])
    if signals and not covers:
        log.warning(
            "awareness post rejected: empty covers_signal_keys channel=%r despite %d input signal(s)",
            channel_key, len(signals),
        )
        return False

    # Audience integrity: a post that covers a leadership-only signal must
    # land in the leader-lounge channel, not a public channel.
    if covers:
        covers_set = set(covers)
        for sig in signals or []:
            if signal_source_key(sig) not in covers_set:
                continue
            if is_leadership_only_signal(sig) and channel_key != "leader-lounge":
                log.warning(
                    "awareness post rejected: leadership-only signal %s routed to public channel %s",
                    signal_source_key(sig), channel_key,
                )
                return False

    try:
        channel_config = _channel_config_by_key(channel_key)
    except RuntimeError:
        log.warning("awareness post rejected: channel %r not configured", channel_key)
        return False
    channel = bot.get_channel(channel_config["id"])
    if not channel:
        log.warning("awareness post rejected: channel %r not found in Discord", channel_key)
        return False

    content = post.get("content")
    if not content:
        log.warning("awareness post on %r had empty content", channel_key)
        return False

    result = {
        "event_type": post.get("event_type") or "awareness_update",
        "summary": post.get("summary"),
        "content": content,
    }
    try:
        await _post_to_elixir(channel, result)
    except Exception:
        log.error("awareness post send failed channel=%r", channel_key, exc_info=True)
        return False

    posts = _app._entry_posts(result)
    channel_id = channel_config["id"]
    channel_name = getattr(channel, "name", None)
    if not isinstance(channel_name, str):
        channel_name = None
    channel_kind = getattr(channel, "type", None)
    if channel_kind is not None:
        channel_kind = str(channel_kind)
    summary = result.get("summary")
    event_type = result.get("event_type")
    for index, body_part in enumerate(posts):
        post_summary = summary if index == 0 else f"{summary} ({index + 1}/{len(posts)})" if summary else None
        post_event_type = event_type if index == 0 else f"{event_type}_part"
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            body_part,
            summary=post_summary,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow=channel_config["subagent_key"],
            event_type=post_event_type,
            raw_json={
                "source": "awareness_loop",
                "leads_with": post.get("leads_with"),
                "covers_signal_keys": post.get("covers_signal_keys") or [],
                "result": result,
            },
        )

    # Persist signal outcomes so the existing dedupe/stats path stays intact.
    body = "\n\n".join(posts)
    for signal in signals or []:
        sig_key = signal_source_key(signal)
        if not sig_key or sig_key not in covers:
            continue
        await asyncio.to_thread(
            db.upsert_signal_outcome,
            sig_key,
            signal.get("type") or "awareness_signal",
            channel_key,
            channel_id,
            event_type,
            required=True,
            delivery_status="delivered",
            payload={"result": result, "signals": [signal]},
            mark_attempt=True,
            delivered=True,
        )

    # Memory extraction — same fire-and-forget pattern as per-signal delivery.
    from runtime.helpers._common import _safe_create_task
    fake_outcome = {
        "intent": event_type,
        "target_channel_key": channel_key,
        "target_channel_id": channel_id,
        "source_signal_key": (covers[0] if covers else "awareness_loop"),
    }
    _safe_create_task(
        _post_signal_memory(body, fake_outcome, signals or []),
        name="awareness_signal_memory",
    )
    return True


async def _deliver_awareness_post_plan(plan: dict, signals: list[dict]) -> dict:
    """Deliver every valid post in an awareness post plan.

    Returns a report dict: ``{"delivered": int, "rejected": int,
    "covered_signal_keys": set[str]}``. The caller compares
    ``covered_signal_keys`` against ``hard_post_signals`` and falls back to
    the legacy per-signal delivery path for any uncovered hard-post-floor
    signals.
    """
    posts = (plan or {}).get("posts") or []
    delivered = 0
    rejected = 0
    covered: set[str] = set()
    for post in posts:
        ok = await _deliver_awareness_post(post, signals or [])
        if ok:
            delivered += 1
            for key in post.get("covers_signal_keys") or []:
                if key:
                    covered.add(str(key))
        else:
            rejected += 1

    # Mark covered signals immediately so a late partial failure (e.g., one of
    # several hard-floor fallbacks) does not leave delivered posts un-marked
    # and cause a duplicate on the next tick.
    if covered:
        covered_signals = [
            s for s in (signals or [])
            if signal_source_key(s) in covered
        ]
        if covered_signals:
            await _mark_signal_group_completed(covered_signals)

    return {
        "delivered": delivered,
        "rejected": rejected,
        "covered_signal_keys": covered,
    }


async def _deliver_signal_group_via_awareness(signals, clan, war, *, workflow: str | None = None) -> bool:
    """Awareness-loop replacement for ``_deliver_signal_group``.

    1. Build the situation from ``signals + clan + war``.
    2. Mark every input signal as sent on intake — the awareness tick owns
       these signals from this point on. If the agent skips them, the post is
       rejected, or the run crashes, they will not re-fire next tick. This is
       intentional: rejections are structural (empty covers, audience gates),
       not transient. Retries are reserved for hard-post-floor signals that
       take the explicit fallback path below.
    3. Fast-path: if quiet (no signals, no hard floors, not near deadline),
       return True without calling the LLM.
    4. Run the awareness agent.
    5. Validate + deliver the post plan.
    6. For any hard-post-floor signal not covered by the plan, fall back to
       the legacy per-signal ``_deliver_signal_group`` for *just that signal*
       so coverage is guaranteed.

    Returns True iff every required signal was delivered through some path.
    """
    from runtime.situation import build_situation, situation_is_quiet
    from heartbeat import HeartbeatTickResult

    bundle = HeartbeatTickResult(signals=signals or [], clan=clan or {}, war=war or {})
    situation = build_situation(bundle)

    if signals:
        await _mark_signal_group_completed(signals)

    if situation_is_quiet(situation):
        log.info("awareness loop: quiet tick, skipping agent call")
        return True

    tool_stats: dict = {}
    try:
        plan = await asyncio.to_thread(
            elixir_agent.run_awareness_tick, situation, tool_stats=tool_stats,
        )
    except Exception as exc:
        log.error("awareness loop run_awareness_tick failed: %s", exc, exc_info=True)
        plan = None

    if plan is None:
        log.warning("awareness loop returned no plan; falling back to per-signal delivery")
        return await _deliver_signal_group(signals, clan, war)

    report = await _deliver_awareness_post_plan(plan, signals)

    # Hard-post-floor fallback: any required signal the agent omitted must
    # still produce a post via the legacy per-signal path. Key everything on
    # signal_source_key so the comparison matches build_situation annotation.
    hard_required_keys = {hp.get("signal_key") for hp in (situation.get("hard_post_signals") or [])}
    covered_keys = report["covered_signal_keys"]
    uncovered = [
        signal for signal in (signals or [])
        if signal_source_key(signal) in hard_required_keys
        and signal_source_key(signal) not in covered_keys
    ]
    fallback_failed_keys: set[str] = set()
    all_ok = True
    if uncovered:
        log.warning(
            "awareness loop: %d hard-post-floor signal(s) uncovered; falling back per-signal",
            len(uncovered),
        )
        for signal in uncovered:
            ok = await _deliver_signal_group([signal], clan, war)
            if not ok:
                fallback_failed_keys.add(signal_source_key(signal))
            all_ok = all_ok and ok

    # Mark non-covered signals the agent consciously skipped so they don't
    # re-surface every tick. Exclude fallback-failed hard signals so they
    # retry. Covered signals + fallback-succeeded hard signals are already
    # marked by _deliver_awareness_post_plan and _deliver_signal_group.
    considered_skipped = [
        signal for signal in (signals or [])
        if signal_source_key(signal) not in covered_keys
        and signal_source_key(signal) not in fallback_failed_keys
        and signal_source_key(signal) not in hard_required_keys
    ]
    if considered_skipped:
        await _mark_signal_group_completed(considered_skipped)

    # Mark any revisits the agent covered as revisited so they don't re-fire
    # on the next tick. A revisit is "covered" when its signal_key appears in
    # any post's covers_signal_keys OR when it matches a signal the agent
    # consciously skipped / fell back on — either way, the agent saw it.
    revisit_keys_seen = set(covered_keys)
    revisit_keys_seen.update(signal_source_key(s) for s in (considered_skipped or []))
    revisit_keys_seen.update(signal_source_key(s) for s in (uncovered or []))
    if revisit_keys_seen:
        try:
            await asyncio.to_thread(db.mark_revisited, sorted(revisit_keys_seen))
        except Exception:
            log.warning("mark_revisited failed", exc_info=True)

    log.info(
        "awareness_tick_result workflow=%r delivered=%d rejected=%d covered=%d considered_skipped=%d "
        "hard_fallback=%d hard_fallback_failed=%d signals_in=%d skipped_reason=%r",
        workflow,
        report["delivered"],
        report["rejected"],
        len(covered_keys),
        len(considered_skipped),
        len(uncovered),
        len(fallback_failed_keys),
        len(signals or []),
        (plan or {}).get("skipped_reason"),
    )

    # Persistent tick record for admin observability. Includes per-signal
    # status so non-covered signals are no longer invisible in reports.
    uncovered_keys = {signal_source_key(s) for s in uncovered}
    signal_outcomes: list[dict] = []
    for signal in (signals or []):
        key = signal_source_key(signal)
        if key in covered_keys:
            status = "covered"
        elif key in fallback_failed_keys:
            status = "fallback_failed"
        elif key in uncovered_keys:
            status = "fallback"
        else:
            status = "skipped"
        signal_outcomes.append({
            "signal_key": key,
            "signal_type": signal.get("type") or "",
            "status": status,
        })
    try:
        await asyncio.to_thread(
            db.record_awareness_tick,
            workflow=workflow,
            signals_in=len(signals or []),
            posts_delivered=report["delivered"],
            posts_rejected=report["rejected"],
            covered_keys=len(covered_keys),
            considered_skipped=len(considered_skipped),
            hard_fallback=len(uncovered),
            hard_fallback_failed=len(fallback_failed_keys),
            all_ok=all_ok,
            skipped_reason=(plan or {}).get("skipped_reason"),
            signal_outcomes=signal_outcomes,
            write_calls_issued=int(tool_stats.get("write_calls_issued", 0)),
            write_calls_succeeded=int(tool_stats.get("write_calls_succeeded", 0)),
            write_calls_denied=int(tool_stats.get("write_calls_denied", 0)),
        )
    except Exception:
        log.warning("record_awareness_tick failed", exc_info=True)

    if not all_ok:
        return False

    return True


def _strip_weekly_recap_header(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    lines = body.splitlines()
    if lines and _WEEKLY_RECAP_HEADER_RE.match(lines[0] or ""):
        lines = lines[1:]
        while lines and not (lines[0] or "").strip():
            lines = lines[1:]
    return "\n".join(lines).strip()


def _format_weekly_recap_post(recap_text: str, *, now: datetime | None = None) -> str:
    body = _strip_weekly_recap_header(recap_text)
    current = (now or datetime.now(timezone.utc)).astimezone(CHICAGO)
    title = f"**Weekly Recap | {current.strftime('%B')} {current.day}, {current.year}**"
    if not body:
        return title
    return f"{title}\n\n{body}"


def _progression_signal_batches(signals):
    if not signals:
        return []

    required_signals = [
        signal for signal in signals
        if signal.get("type") not in OPTIONAL_PROGRESSION_SIGNAL_TYPES
    ]
    optional_signals = [
        signal for signal in signals
        if signal.get("type") in OPTIONAL_PROGRESSION_SIGNAL_TYPES
    ]

    batches = []
    if required_signals:
        batches.append(required_signals)
    if optional_signals:
        batches.append(optional_signals)
    return batches


def _system_signal_updates(signals):
    return [signal for signal in (signals or []) if signal.get("signal_key")]


def _store_recap_memories_for_signal_batch(signal_batch, posts, channel_id):
    body = "\n\n".join((post or "").strip() for post in (posts or []) if (post or "").strip())
    if not body:
        return None
    recap = upsert_war_recap_memory(
        signals=signal_batch,
        body=body,
        channel_id=channel_id,
        workflow="observation",
    )
    # Update the race win streak memory on race completion
    streak_signal_types = {"war_week_complete", "war_completed"}
    for signal in (signal_batch or []):
        if signal.get("type") in streak_signal_types:
            season_id = signal.get("season_id")
            week = signal.get("week")
            if week is None and signal.get("section_index") is not None:
                week = int(signal["section_index"]) + 1
            race_rank = signal.get("race_rank") or signal.get("rank")
            if season_id is not None and week is not None and race_rank is not None:
                try:
                    upsert_race_streak_memory(
                        season_id=season_id,
                        week=week,
                        race_rank=race_rank,
                    )
                except Exception:
                    log.warning("Failed to update race streak memory", exc_info=True)
            break
    return recap


def _build_system_signal_context(signal, channel_name):
    payload = signal.get("payload") or {}
    details = payload.get("details") or []
    lines = [
        "This is a standalone clan-wide system update about a new Elixir capability.",
        f"Post it for {channel_name}.",
        "Write exactly one Discord message. Do not split it into parts or a series.",
        "Write the full final Discord message yourself, including the subject line.",
        "For system updates, prefer starting with a bolded subject line as the first line.",
        "If you use a subject line, include an Elixir custom emoji in it using :emoji_name: shortcode syntax.",
        "If you use a subject line, do not restate that title again immediately after the first line.",
        "Do not mention hidden system mechanics or call it a system signal.",
        "Make it feel like a self-contained clan update from Elixir.",
        "",
        f"signal_type: {signal.get('type') or 'unknown'}",
        f"signal_key: {signal.get('signal_key') or 'unknown'}",
        f"title: {payload.get('title') or signal.get('title') or ''}",
        f"message: {payload.get('message') or signal.get('message') or ''}",
        f"audience: {payload.get('audience') or 'clan'}",
        f"capability_area: {payload.get('capability_area') or 'general'}",
    ]
    if details:
        lines.append("details:")
        lines.extend(f"- {detail}" for detail in details)
    return "\n".join(lines)


def _preauthored_system_signal_result(signal):
    payload = (signal or {}).get("payload") or {}
    content = (
        payload.get("discord_content")
        or payload.get("preauthored_discord_content")
        or signal.get("discord_content")
    )
    content = (content or "").strip()
    if not content:
        return None
    summary = (
        payload.get("title")
        or signal.get("title")
        or signal.get("signal_key")
        or "System update"
    )
    return {
        "event_type": "channel_update",
        "summary": summary,
        "content": content,
    }


async def _post_system_signal_updates(signals, clan, war):
    system_signals = _system_signal_updates(signals)
    if not system_signals:
        return
    for signal in system_signals:
        await _deliver_signal_group([signal], clan, war)


async def _publish_pending_system_signal_updates(*, seed_startup_signals: bool = False) -> int:
    if seed_startup_signals:
        await asyncio.to_thread(queue_startup_system_signals)
    pending = await asyncio.to_thread(db.list_pending_system_signals)
    if not pending:
        return 0
    await _post_system_signal_updates(pending, {}, {})
    return len(pending)


def _mark_delivered_signals(signals, *, today: str | None = None):
    for signal in signals or []:
        if signal.get("signal_key"):
            continue
        signal_date = signal.get("signal_date") or today or db.chicago_today()
        signal_type = signal.get("signal_log_type") or signal.get("type")
        if signal_type:
            db.mark_signal_sent(signal_type, signal_date)
        # Group signals (e.g. war_surprise_participant) carry per-member
        # signal_log_type values for finer-grained dedup. Detector code
        # checks those keys directly via was_signal_sent_any_date, so we
        # have to log each one — otherwise the detector re-fires the same
        # member every tick because the per-member key is never written.
        for member in signal.get("members") or []:
            member_log_type = (member or {}).get("signal_log_type")
            if member_log_type:
                db.mark_signal_sent(member_log_type, signal_date)
        if signal.get("type") == "clan_birthday":
            db.mark_announcement_sent(signal_date, "clan_birthday", None)
        elif signal.get("type") == "join_anniversary":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "join_anniversary", tag)
        elif signal.get("type") == "member_birthday":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "birthday", tag)


def _persist_signal_detector_cursors(cursor_updates):
    for update in cursor_updates or []:
        db.upsert_signal_detector_cursor(
            update.get("detector_key") or "",
            update.get("scope_key") or "",
            cursor_text=update.get("cursor_text"),
            cursor_int=update.get("cursor_int"),
            metadata=update.get("metadata"),
        )
