"""Signal delivery pipeline and helpers."""

__all__ = [
    "_WEEKLY_RECAP_HEADER_RE", "_post_to_elixir", "_load_live_clan_context",
    "_channel_config_by_key", "_signal_group_needs_recap_memory",
    "_build_outcome_context", "_mark_signal_group_completed", "_post_signal_memory",
    "_deliver_signal_outcome", "_deliver_signal_group",
    "_deliver_awareness_post", "_deliver_awareness_post_plan",
    "_deliver_signal_group_via_awareness", "_deliver_arena_relay_sidecars",
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


# These wrappers exist so test patches on `elixir._post_to_elixir` /
# `elixir._load_live_clan_context` (i.e. `runtime.app.<name>`) take effect
# everywhere downstream — the attribute access at call time picks up the patch.
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


# Re-export facade: the real implementations live in runtime/signals/*.py.
from runtime.signals.context import (  # noqa: E402,F401
    _build_compact_war_context,
    _build_outcome_context,
    _build_player_insight_context,
    _build_river_race_insight_layer,
    _build_system_signal_context,
    _extract_race_standings_summary,
)
from runtime.signals.memory import (  # noqa: E402,F401
    _post_signal_memory,
    _store_recap_memories_for_signal_batch,
)
from runtime.signals.state import (  # noqa: E402,F401
    _mark_delivered_signals,
    _mark_signal_group_completed,
    _persist_signal_detector_cursors,
)
from runtime.signals.system import (  # noqa: E402,F401
    _preauthored_system_signal_result,
    _post_system_signal_updates,
    _publish_pending_system_signal_updates,
    _system_signal_updates,
)
from runtime.signals.delivery import (  # noqa: E402,F401
    _deliver_awareness_post,
    _deliver_awareness_post_plan,
    _deliver_arena_relay_sidecars,
    _deliver_signal_group,
    _deliver_signal_group_via_awareness,
    _deliver_signal_outcome,
)
