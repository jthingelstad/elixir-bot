"""Bounded-event thread rooms (docs/reference/v5.1/channels.md §2).

War weeks and seasonal game-event modes get a Discord thread as their room —
born at the event's observed birth, locked at its observed death. Everything
here is BEST-EFFORT by contract: any failure logs and returns None/False;
posts fall back to the parent channel; no ceremony ever blocks on a room.

Engine/runtime seam: the engine stays sync and Discord-free — it only reads
`war_weeks.thread_id` (the delivery address). The runtime owns the lifecycle:
`ensure_war_week_thread` runs as an engine-tick post-step (like the clan-chat
relay) and `close_war_week_threads` keys off fulfilled week_finished intents.
"""

from __future__ import annotations

import json
import logging

import discord

log = logging.getLogger("elixir.threads")

THREAD_AUTO_ARCHIVE_MINUTES = 4320  # 3 days — war-day cadence (channels.md §2)
OPENER_MAX_NAMES = 15


async def create_thread(bot, channel_id: int, name: str):
    """Create a public thread under a text channel; returns thread or None."""
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        thread = await channel.create_thread(
            name=name[:100],
            auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
            type=discord.ChannelType.public_thread,
        )
        return thread
    except Exception as exc:
        log.warning("thread create failed for %r under %s", name, channel_id, exc_info=True)
        from storage.incidents import record_incident
        record_incident("threads.create", exc, context={"name": name, "channel_id": channel_id},
                        severity="warn")
        return None


async def lock_thread(bot, thread_id: int, closing_text: str | None = None) -> bool:
    """Post an optional closing line, then lock + archive. Idempotent: an
    already-locked thread is left alone (no double closing post)."""
    try:
        thread = bot.get_channel(int(thread_id))
        if thread is None:
            thread = await bot.fetch_channel(int(thread_id))
        if getattr(thread, "locked", False):
            return False
        if closing_text:
            await thread.send(closing_text)
        await thread.edit(locked=True, archived=True)
        return True
    except Exception as exc:
        log.warning("thread lock failed for %s", thread_id, exc_info=True)
        from storage.incidents import record_incident
        record_incident("threads.lock", exc, context={"thread_id": thread_id}, severity="warn")
        return False


def war_week_thread_name(season_id: int, section_index: int, period_type: str | None) -> str:
    if (period_type or "").lower() == "colosseum":
        return f"Colosseum — Season {season_id}"
    return f"War Week {int(section_index) + 1} — Season {season_id}"


def build_week_opener(conn, season_id: int, section_index: int, name: str) -> str:
    """Deterministic grounded opener (v1 choice: no LLM in the room's first
    breath — the facts ARE the welcome). Names last week's fighters."""
    prev = conn.execute(
        """SELECT p.current_name FROM war_participation wp
           JOIN players p USING (player_tag)
           WHERE (wp.season_id, wp.section_index) = (
               SELECT season_id, section_index FROM war_weeks
               WHERE finish_time IS NOT NULL
                 AND (season_id, section_index) < (?, ?)
               ORDER BY season_id DESC, section_index DESC LIMIT 1)
             AND wp.fame > 0
           ORDER BY wp.fame DESC LIMIT ?""",
        (season_id, section_index, OPENER_MAX_NAMES),
    ).fetchall()
    fighters = ", ".join(r["current_name"] for r in prev if r["current_name"])
    lines = [
        f"⚔️ **{name}** — this is the week's room. Day-by-day updates and "
        "battle chatter live here; the big results still land in the channel."
    ]
    if fighters:
        lines.append(f"Last week's fighters: {fighters}.")
    lines.append("The record of this week stays browsable here after it ends.")
    return "\n".join(lines)


async def ensure_war_week_thread(bot, conn_factory, river_race_channel_id: int) -> int | None:
    """Engine-tick post-step: the newest unfinished war week without a thread
    gets one (idempotent — thread_id set exactly once). Returns thread id."""
    import asyncio

    def _find():
        conn = conn_factory()
        try:
            row = conn.execute(
                """SELECT season_id, section_index, period_type FROM war_weeks
                   WHERE finish_time IS NULL AND thread_id IS NULL
                     AND season_id >= 133
                   ORDER BY season_id DESC, section_index DESC LIMIT 1"""
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    week = await asyncio.to_thread(_find)
    if not week:
        return None
    name = war_week_thread_name(week["season_id"], week["section_index"], week.get("period_type"))
    thread = await create_thread(bot, river_race_channel_id, name)
    if thread is None:
        return None

    def _store_and_opener():
        conn = conn_factory()
        try:
            cur = conn.execute(
                """UPDATE war_weeks SET thread_id = ?
                   WHERE season_id = ? AND section_index = ? AND thread_id IS NULL""",
                (thread.id, week["season_id"], week["section_index"]),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None  # lost a race with another writer; keep theirs
            return build_week_opener(conn, week["season_id"], week["section_index"], name)
        finally:
            conn.close()

    opener = await asyncio.to_thread(_store_and_opener)
    if opener:
        try:
            await thread.send(opener)
        except Exception:
            log.warning("week opener post failed in %s", thread.id, exc_info=True)
        log.info("war week thread born: %s (%s)", name, thread.id)
        return thread.id
    return None


async def close_war_week_threads(bot, conn_factory, fulfilled_intents: list) -> int:
    """Keyed off fulfilled week_finished intents: closing post + lock.
    lock_thread's already-locked check makes the 15-minute rescan harmless."""
    closed = 0
    for intent in fulfilled_intents:
        if (intent["intent_type"] or "") != "war:week_finished":
            continue
        try:
            payload = json.loads(intent["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        thread_id = payload.get("week_thread_id")
        if not thread_id:
            continue
        rank = payload.get("our_rank")
        fame = payload.get("our_fame")
        line = "🏁 The record stands."
        if rank and fame:
            line = f"🏁 The record stands — finished #{rank} at {fame:,} fame."
        if await lock_thread(bot, thread_id, closing_text=line):
            closed += 1
    return closed
