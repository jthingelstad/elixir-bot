import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import db
from runtime.activities import schedule_specs_from_registry
from runtime import status as runtime_status

DISCORD_MAX_MESSAGE_LEN = 2000
DISCORD_CHUNK_SIZE = 1990  # leave room for overhead

__all__ = [
    "BOT_ROLE_ID", "CHICAGO", "LEADER_ROLE_ID", "bot", "log", "scheduler",
    "DISCORD_MAX_MESSAGE_LEN", "DISCORD_CHUNK_SIZE",
    "_runtime_app", "_bot", "_scheduler", "_log", "_chicago",
    "_leader_role_id", "_bot_role_id", "_post_to_elixir",
    "_chunk_for_discord", "_safe_create_task",
    "_fmt_iso_short", "_fmt_relative", "_fmt_bytes", "_fmt_num", "_status_badge",
    "_member_label", "_join_member_bits", "_canon_tag",
    "_format_relative_join_age", "_recent_join_display_rows",
    "_leader_role_mention", "_with_leader_ping",
    "_job_next_runs", "_schedule_specs",
]

BOT_ROLE_ID = None
CHICAGO = None
LEADER_ROLE_ID = None
bot = None
log = None
scheduler = None


def _runtime_app():
    from runtime import app as app_module
    return app_module


def _bot():
    return bot if bot is not None else _runtime_app().bot


def _scheduler():
    return scheduler if scheduler is not None else _runtime_app().scheduler


def _log():
    return log if log is not None else _runtime_app().log


def _chicago():
    return CHICAGO if CHICAGO is not None else _runtime_app().CHICAGO


def _leader_role_id():
    return LEADER_ROLE_ID if LEADER_ROLE_ID is not None else _runtime_app().LEADER_ROLE_ID


def _bot_role_id():
    return BOT_ROLE_ID if BOT_ROLE_ID is not None else _runtime_app().BOT_ROLE_ID


async def _post_to_elixir(*args, **kwargs):
    return await _runtime_app()._post_to_elixir(*args, **kwargs)


def _chunk_for_discord(text, size=DISCORD_CHUNK_SIZE):
    """Split text into chunks that fit within Discord's message limit.

    Prefers paragraph breaks, then line breaks, then word boundaries, so
    dense replies don't split mid-word or mid-code-block. Falls back to a
    hard character split only when no whitespace is found inside the
    window — the size limit is an absolute ceiling.
    """
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        split_at = window.rfind("\n\n")
        if split_at > 0:
            split_at += 2
        else:
            newline = window.rfind("\n")
            if newline > 0:
                split_at = newline + 1
            else:
                space = window.rfind(" ")
                split_at = space + 1 if space > 0 else size
        piece = remaining[:split_at].rstrip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _safe_create_task(coro, *, name=None):
    """Schedule an asyncio task with automatic exception logging."""
    _bg_log = logging.getLogger("elixir")

    async def _wrapper():
        try:
            await coro
        except Exception:
            _bg_log.warning("background task %s failed", name or "unnamed", exc_info=True)

    return asyncio.get_event_loop().create_task(_wrapper(), name=name)


def _fmt_iso_short(value):
    if not value:
        return "n/a"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(_chicago()).strftime("%Y-%m-%d %I:%M %p CT")
    except ValueError:
        return str(value)


def _fmt_relative(value):
    if not value:
        return "n/a"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return str(value)
    delta = datetime.now(timezone.utc) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _fmt_bytes(size):
    if size is None:
        return "n/a"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _fmt_num(value, digits=0):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if digits:
            return f"{value:,.{digits}f}"
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return f"{value:,}"


def _status_badge(ok):
    if ok is None:
        return "⚪"
    return "🟢" if ok else "🔴"


def _member_label(member):
    return (
        member.get("member_ref")
        or member.get("member_reference")
        or member.get("name")
        or member.get("member_name")
        or member.get("tag")
        or "unknown"
    )


def _join_member_bits(members, formatter, limit=3):
    if not members:
        return "none"
    return ", ".join(formatter(member) for member in members[:limit])


def _canon_tag(tag):
    return (str(tag or "").strip().upper().lstrip("#"))


def _format_relative_join_age(joined_date):
    if not joined_date:
        return "join timing unknown"
    try:
        joined_day = datetime.strptime(joined_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return "join timing unknown"
    today = datetime.now(_chicago()).date()
    age_days = max(0, (today - joined_day).days)
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "1 day ago"
    return f"{age_days} days ago"


def _recent_join_display_rows(clan):
    clan = clan or {}
    live_recent_joins = clan.get("_elixir_recent_joins") or []
    stored_recent_joins = db.list_recent_joins(days=30)
    if live_recent_joins:
        return live_recent_joins, max(len(live_recent_joins), len(stored_recent_joins))
    return stored_recent_joins, len(stored_recent_joins)


def _leader_role_mention():
    leader_role_id = _leader_role_id()
    return f"<@&{leader_role_id}>" if leader_role_id else ""


def _with_leader_ping(content):
    mention = _leader_role_mention()
    if not mention or not content or mention in content:
        return content
    return f"{mention}\n{content}"


def _job_next_runs():
    items = []
    for job in _scheduler().get_jobs():
        next_run = job.next_run_time.astimezone(_chicago()).strftime("%Y-%m-%d %I:%M %p CT") if job.next_run_time else "n/a"
        items.append({"id": job.id, "next_run": next_run})
    return sorted(items, key=lambda item: item["id"])


def _schedule_specs():
    return schedule_specs_from_registry(_runtime_app())
