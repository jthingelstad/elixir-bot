"""System-signal publication (api-sentinel CR-API drift, startup notices).

The v5-native home for publishing pre-authored system signals. It posts directly
to the target lane via runtime.discord_posting/_post_to_elixir — the same direct
post path the other scheduled jobs were rewired to (award-detection,
tournament-watch, weekly-relay) — instead of the retired v4 awareness-delivery
pipeline. System signals are expected to carry pre-authored Discord copy (e.g.
api-sentinel discord_content); anything without copy is logged and skipped.
"""

from __future__ import annotations

import asyncio
import logging

import db
from runtime.helpers import _channel_config_by_key, _channel_msg_kwargs, _channel_scope
from runtime.helpers._common import _bot, _post_to_elixir
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
    """Post pre-authored system signals directly to their target lane.

    ``clan``/``war`` are accepted for call-site compatibility but unused — the
    copy is pre-authored, so no live context is composed.
    """
    system_signals = _system_signal_updates(signals)
    if not system_signals:
        return

    for signal in system_signals:
        result = _preauthored_system_signal_result(signal)
        if not result:
            log.warning(
                "system signal %s has no pre-authored content; skipping",
                signal.get("signal_key") or signal.get("signal_log_type"),
            )
            continue
        channel_key, _leads_with = _preauthored_system_signal_target(signal)
        try:
            channel_config = _channel_config_by_key(channel_key)
        except RuntimeError:
            log.warning("system signal: channel lane %r not configured", channel_key)
            continue
        channel = _bot().get_channel(channel_config["id"])
        if channel is None:
            log.warning("system signal: channel %r (%s) not found", channel_key, channel_config["id"])
            continue

        content = result["content"]
        try:
            await _post_to_elixir(channel, {"content": content})
        except Exception:
            log.exception("system signal post failed channel=%r", channel_key)
            continue

        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel),
            "assistant",
            content,
            summary=result.get("summary"),
            **_channel_msg_kwargs(channel),
            workflow="system_signals",
            event_type=result.get("event_type") or "channel_update",
        )
        if signal.get("signal_key"):
            await asyncio.to_thread(db.mark_system_signal_announced, signal["signal_key"])


async def _publish_pending_system_signal_updates(*, seed_startup_signals: bool = False) -> int:
    if seed_startup_signals:
        await asyncio.to_thread(queue_startup_system_signals)
    pending = await asyncio.to_thread(db.list_pending_system_signals)
    if not pending:
        return 0
    await _post_system_signal_updates(pending, {}, {})
    return len(pending)
