"""Signal delivery pipeline and helpers."""

__all__ = [
    "_WEEKLY_RECAP_HEADER_RE", "_post_to_elixir", "_load_live_clan_context",
    "_channel_config_by_key", "_signal_group_needs_recap_memory",
    "_build_outcome_context", "_mark_signal_group_completed", "_post_signal_memory",
    "_deliver_signal_outcome", "_deliver_signal_group",
    "_strip_weekly_recap_header", "_format_weekly_recap_post",
    "_observation_signal_batches", "_progression_signal_batches",
    "_system_signal_updates", "_store_recap_memories_for_signal_batch",
    "_build_system_signal_context", "_preauthored_system_signal_result",
    "_post_system_signal_updates", "_publish_pending_system_signal_updates",
    "_mark_delivered_signals", "_persist_signal_detector_cursors",
]

import asyncio
import json
import re
from datetime import datetime, timezone

import discord
import db
import elixir_agent
import prompts
from storage.contextual_memory import upsert_war_recap_memory
from runtime import app as _app
from runtime.channel_subagents import (
    build_subagent_memory_context,
    maybe_upsert_signal_memory,
    OPTIONAL_PROGRESSION_SIGNAL_TYPES,
    plan_signal_outcomes,
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
    if channel_key == "river-race":
        lines.extend([
            "",
            "Focus on River Race state, momentum, and what the clan should do right now.",
            "Current war data:",
            json.dumps(war or {}, indent=2, default=str),
        ])
    elif channel_key == "player-progress":
        lines.extend([
            "",
            "Focus on the player's achievement and why it is worth celebrating.",
        ])
    elif channel_key == "clan-events":
        has_likely_kick = any(s.get("likely_kicked") for s in (signals or []))
        if has_likely_kick:
            lines.extend([
                "",
                "This member was likely removed from the clan due to inactivity.",
                "Keep the message brief and neutral. Do not write a warm farewell or thank them for contributions.",
                "A simple factual note that the member is no longer with the clan is enough.",
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

        result = await _app._apply_member_refs_to_result(result)
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
        asyncio.get_event_loop().create_task(
            _post_signal_memory(body, outcome, signals)
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


def _observation_signal_batches(signals):
    if not signals:
        return []
    grouped = []
    completion_batch = []
    batches = []
    completion_signal_types = {
        "war_completed",
        "war_week_complete",
        "war_champ_standings",
    }
    for signal in signals:
        signal_type = signal.get("type") or ""
        if signal_type.startswith("war_"):
            if signal_type in completion_signal_types:
                completion_batch.append(signal)
                continue
            batches.append([signal])
        else:
            grouped.append(signal)
    if grouped:
        batches.insert(0, grouped)
    if completion_batch:
        batches.append(completion_batch)
    return batches


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
    return upsert_war_recap_memory(
        signals=signal_batch,
        body=body,
        channel_id=channel_id,
        workflow="observation",
    )


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
