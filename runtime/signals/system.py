"""System signal publication helpers."""

from __future__ import annotations

import asyncio
import logging

import db
from runtime.system_signals import queue_startup_system_signals

log = logging.getLogger("elixir")


def _system_signal_updates(signals):
    return [signal for signal in (signals or []) if signal.get("signal_key")]


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


def _preauthored_system_signal_target(signal):
    signal_type = (signal or {}).get("signal_type") or (signal or {}).get("type")
    payload = (signal or {}).get("payload") or {}
    audience = (payload.get("audience") or (signal or {}).get("audience") or "").strip().lower()
    if audience == "leadership" or signal_type in {"api_event_sentinel", "api_schema_sentinel"}:
        return "leader-lounge", "leadership"
    return "announcements", "system"


async def _post_system_signal_updates(signals, clan, war):
    from runtime.jobs._signals import (
        _deliver_awareness_post,
        _mark_signal_group_completed,
    )

    system_signals = _system_signal_updates(signals)
    if not system_signals:
        return

    for signal in system_signals:
        result = _preauthored_system_signal_result(signal)
        if not result:
            # System signals are expected to carry pre-authored copy (e.g.
            # api-sentinel discord_content). The v4 awareness fallback for
            # non-pre-authored system signals has been retired.
            log.warning(
                "system signal %s has no pre-authored content; skipping "
                "(v4 awareness fallback retired)",
                signal.get("signal_key") or signal.get("signal_log_type"),
            )
            continue
        source_key = signal.get("signal_key") or signal.get("signal_log_type")
        channel_key, leads_with = _preauthored_system_signal_target(signal)
        post = {
            "channel": channel_key,
            "leads_with": leads_with,
            "event_type": result.get("event_type") or "channel_update",
            "summary": result.get("summary"),
            "content": result.get("content"),
            "covers_signal_keys": [source_key] if source_key else [],
        }
        intent = await asyncio.to_thread(
            db.create_awareness_post_intent,
            post,
            [signal],
            workflow="system_signals",
        )
        delivered = await _deliver_awareness_post(post, [signal], intent=intent)
        if delivered:
            await _mark_signal_group_completed([signal])


async def _publish_pending_system_signal_updates(*, seed_startup_signals: bool = False) -> int:
    if seed_startup_signals:
        await asyncio.to_thread(queue_startup_system_signals)
    pending = await asyncio.to_thread(db.list_pending_system_signals)
    if not pending:
        return 0
    await _post_system_signal_updates(pending, {}, {})
    return len(pending)
