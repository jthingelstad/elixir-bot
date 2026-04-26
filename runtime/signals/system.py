"""System signal publication helpers."""

from __future__ import annotations

import asyncio

import db
from runtime.system_signals import queue_startup_system_signals


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


async def _post_system_signal_updates(signals, clan, war):
    from runtime.jobs._signals import _deliver_signal_group

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
