"""Weekly memory synthesis job.

Assembles the week's memories, channel posts, and live clan state, hands
them to the memory-synthesis LLM workflow, persists the resulting arc
memories, expires stale entries, and posts the digest to #leader-lounge.
"""

__all__ = [
    "MEMORY_SYNTHESIS_DAY", "MEMORY_SYNTHESIS_HOUR",
    "MEMORY_SYNTHESIS_DRY_RUN", "MEMORY_SYNTHESIS_POSTS_PER_CHANNEL",
    "MEMORY_SYNTHESIS_MEMORY_LIMIT", "MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT",
    "MEMORY_SYNTHESIS_MEMORY_BODY_CHARS", "MEMORY_SYNTHESIS_POST_CHARS",
    "_memory_synthesis_cycle",
    "_build_memory_synthesis_context",
    "_apply_memory_synthesis_plan",
]

import asyncio
import os
from datetime import datetime, timedelta, timezone

import db
import elixir_agent
import prompts
from storage.contextual_memory import upsert_weekly_summary_memory
from runtime.app import bot, log
from runtime.helpers import _channel_msg_kwargs, _channel_scope
from runtime import status as runtime_status
from runtime.jobs._signals import _post_to_elixir

MEMORY_SYNTHESIS_DAY = os.getenv("MEMORY_SYNTHESIS_DAY", "sun")
MEMORY_SYNTHESIS_HOUR = int(os.getenv("MEMORY_SYNTHESIS_HOUR", "22"))
MEMORY_SYNTHESIS_DRY_RUN = os.getenv("MEMORY_SYNTHESIS_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
MEMORY_SYNTHESIS_MEMORY_LIMIT = int(os.getenv("MEMORY_SYNTHESIS_MEMORY_LIMIT", "80"))
MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT = int(os.getenv("MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT", "12"))
MEMORY_SYNTHESIS_POSTS_PER_CHANNEL = int(os.getenv("MEMORY_SYNTHESIS_POSTS_PER_CHANNEL", "12"))
MEMORY_SYNTHESIS_MEMORY_BODY_CHARS = int(os.getenv("MEMORY_SYNTHESIS_MEMORY_BODY_CHARS", "500"))
MEMORY_SYNTHESIS_POST_CHARS = int(os.getenv("MEMORY_SYNTHESIS_POST_CHARS", "700"))


def _current_war_week_id(conn=None) -> str | None:
    """Stable week key used by memory filtering. Mirrors the shape written by
    other per-week upserts — ``"<season>:<week>"``."""
    try:
        state = db.get_current_war_status(conn=conn) or {}
    except Exception:
        return None
    season = state.get("season_id")
    week = state.get("week")
    if season is None or week is None:
        return None
    return f"{season}:{week}"


def _clip_text(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _compact_memory_row(row: dict) -> dict:
    """Return just the fields the synthesis agent needs to reason from."""
    return {
        "memory_id": row.get("memory_id"),
        "source_type": row.get("source_type"),
        "scope": row.get("scope"),
        "title": _clip_text(row.get("title"), 160),
        "body": _clip_text(row.get("body"), MEMORY_SYNTHESIS_MEMORY_BODY_CHARS),
        "summary": _clip_text(row.get("summary"), 300),
        "member_tag": row.get("member_tag"),
        "war_week_id": row.get("war_week_id"),
        "war_season_id": row.get("war_season_id"),
        "event_type": row.get("event_type"),
        "created_at": row.get("created_at"),
        "confidence": row.get("confidence"),
        "tags": row.get("tags") or [],
    }


def _compact_post_row(row: dict) -> dict:
    return {
        "channel_id": row.get("channel_id"),
        "author_type": row.get("author_type"),
        "content": _clip_text(row.get("content"), MEMORY_SYNTHESIS_POST_CHARS),
        "summary": _clip_text(row.get("summary"), 300),
        "created_at": row.get("created_at"),
        "workflow": row.get("workflow"),
        "event_type": row.get("event_type"),
    }


def _build_memory_synthesis_context():
    """Assemble the week's memory/post/live-state payload for the synthesis agent."""
    from memory_store import list_memories

    week_id = _current_war_week_id()
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

    # Week's memories: everything created in the last 7 days, leadership-scoped
    # or public, excluding archived/deleted.
    try:
        week_memories = list_memories(
            viewer_scope="leadership",
            filters={"created_after": week_ago},
            limit=MEMORY_SYNTHESIS_MEMORY_LIMIT,
        )
    except Exception:
        log.warning("memory synthesis: week memories load failed", exc_info=True)
        week_memories = []

    # Prior synthesis arcs so the agent doesn't re-canonize the same events.
    try:
        prior_arcs = list_memories(
            viewer_scope="leadership",
            filters={"source_type": "elixir_synthesis"},
            limit=MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT,
        )
    except Exception:
        log.warning("memory synthesis: prior arcs load failed", exc_info=True)
        prior_arcs = []

    # Recent posts from the channels that carry the week's operational and
    # narrative story. Keyed on channel names that match prompts.py config.
    channel_keys = ("leader-lounge", "river-race", "clan-events", "announcements")
    posts_by_channel: dict[str, list[dict]] = {}
    for key in channel_keys:
        try:
            channel = prompts.discord_singleton_subagent(key)
            channel_id = channel.get("id") if isinstance(channel, dict) else None
        except Exception:
            channel_id = None
        if not channel_id:
            continue
        try:
            rows = db.list_channel_messages(
                channel_id,
                MEMORY_SYNTHESIS_POSTS_PER_CHANNEL,
                "assistant",
            )
        except Exception:
            log.warning("memory synthesis: posts load failed for %s", key, exc_info=True)
            rows = []
        posts_by_channel[key] = [_compact_post_row(r) for r in rows or []]

    # Live clan state for contradiction checking.
    clan_state = {}
    try:
        clan_state["roster"] = db.get_clan_roster_summary()
    except Exception:
        log.warning("memory synthesis: roster summary load failed", exc_info=True)
    try:
        clan_state["war"] = db.get_current_war_status()
    except Exception:
        log.warning("memory synthesis: war status load failed", exc_info=True)

    return {
        "week_window": {"start": week_ago, "end": now.strftime("%Y-%m-%dT%H:%M:%S"), "war_week_id": week_id},
        "week_memories": [_compact_memory_row(m) for m in week_memories],
        "prior_arcs": [_compact_memory_row(m) for m in prior_arcs],
        "week_posts": posts_by_channel,
        "live_clan_state": clan_state,
    }


def _apply_memory_synthesis_plan(plan: dict, *, week_id: str | None, dry_run: bool = False) -> dict:
    """Persist arc memories + expire stale ids. Returns a small stats dict.

    Writes happen synchronously in this helper so the caller can thread it
    through ``asyncio.to_thread``. In dry-run mode the function returns
    counts without persisting anything.
    """
    from memory_store import create_memory, update_memory

    arcs = list(plan.get("arc_memories") or [])
    stale_ids = list(plan.get("stale_memory_ids") or [])
    contradictions = list(plan.get("contradictions") or [])

    stats = {
        "arcs_written": 0,
        "stale_expired": 0,
        "contradictions_flagged": len(contradictions),
        "arcs_requested": len(arcs),
        "stale_requested": len(stale_ids),
        "dry_run": bool(dry_run),
    }

    if dry_run:
        return stats

    actor = "elixir:memory-synthesis"
    now_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for arc in arcs:
        title = (arc.get("title") or "").strip()
        body = (arc.get("body") or "").strip()
        if not title or not body:
            continue
        scope = arc.get("scope") or "leadership"
        tags = arc.get("tags") or []
        member_tag = arc.get("member_tag") or None
        try:
            created = create_memory(
                title=title,
                body=body,
                summary=body[:220],
                source_type="elixir_synthesis",
                is_inference=False,
                confidence=1.0,
                created_by=actor,
                scope=scope,
                member_tag=member_tag,
                war_season_id=arc.get("war_season_id"),
                war_week_id=arc.get("war_week_id") or week_id,
                metadata={"synthesized_at": now_stamp},
            )
        except Exception:
            log.warning("memory synthesis: arc create failed title=%r", title, exc_info=True)
            continue
        if tags:
            try:
                from memory_store import attach_tags
                attach_tags(created["memory_id"], tags, actor=actor)
            except Exception:
                log.warning("memory synthesis: attach_tags failed", exc_info=True)
        stats["arcs_written"] += 1

    # Stale entries get expires_at = today; list_memories' expires_at filter
    # ignores anything that has expired, so stale rows vanish from readers
    # without losing audit history.
    for memory_id in stale_ids:
        try:
            update_memory(int(memory_id), actor=actor, expires_at=now_stamp)
            stats["stale_expired"] += 1
        except Exception:
            log.warning("memory synthesis: expire %s failed", memory_id, exc_info=True)

    return stats


async def _memory_synthesis_cycle():
    """Weekly memory-synthesis job.

    Runs Sunday late by default. Assembles the week's memories + posts +
    live state, hands them to ``run_memory_synthesis``, persists the
    resulting arcs, expires stale entries, and posts the digest (plus any
    contradictions) to #leader-lounge.
    """
    runtime_status.mark_job_start("memory_synthesis")

    leader_channels = prompts.discord_channels_by_workflow("clanops")
    if not leader_channels:
        runtime_status.mark_job_failure("memory_synthesis", "no leadership channel configured")
        return
    channel_config = leader_channels[0]
    channel = bot.get_channel(channel_config["id"])
    if not channel:
        runtime_status.mark_job_failure("memory_synthesis", "leadership channel not found")
        return

    try:
        context = await asyncio.to_thread(_build_memory_synthesis_context)
    except Exception as exc:
        log.error("memory_synthesis: context build failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("memory_synthesis", f"context build failed: {exc}")
        return

    try:
        plan = await asyncio.to_thread(elixir_agent.run_memory_synthesis, context)
    except Exception as exc:
        log.error("memory_synthesis: agent call failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("memory_synthesis", f"agent call failed: {exc}")
        return
    if plan is None:
        runtime_status.mark_job_failure("memory_synthesis", "agent returned no plan")
        return
    if isinstance(plan, dict) and isinstance(plan.get("_error"), dict):
        error = plan["_error"]
        kind = error.get("kind") or "agent_error"
        phase = error.get("phase") or "unknown"
        detail = error.get("detail") or error.get("result_preview") or "no detail"
        log.warning("memory_synthesis: agent returned structured error: %s", error)
        runtime_status.mark_job_failure(
            "memory_synthesis",
            f"agent {kind} during {phase}: {detail}",
        )
        return
    if not isinstance(plan, dict):
        runtime_status.mark_job_failure(
            "memory_synthesis",
            f"agent returned invalid plan type: {type(plan).__name__}",
        )
        return

    week_id = (context.get("week_window") or {}).get("war_week_id")
    stats = await asyncio.to_thread(
        _apply_memory_synthesis_plan,
        plan,
        week_id=week_id,
        dry_run=MEMORY_SYNTHESIS_DRY_RUN,
    )

    digest = (plan.get("digest") or "").strip()
    contradictions = list(plan.get("contradictions") or [])
    if contradictions:
        lines = ["", "**Contradictions flagged against live state:**"]
        for item in contradictions:
            stored = (item.get("stored") or "").strip() or "—"
            live = (item.get("live") or "").strip() or "—"
            action = (item.get("suggested_action") or "").strip() or "review"
            lines.append(f"- memory #{item.get('memory_id')}: stored=`{stored}` · live=`{live}` · suggested: {action}")
        digest = f"{digest}\n" + "\n".join(lines) if digest else "\n".join(lines)

    if not digest:
        runtime_status.mark_job_success(
            "memory_synthesis",
            f"quiet week (arcs={stats['arcs_written']}, stale={stats['stale_expired']}, dry_run={MEMORY_SYNTHESIS_DRY_RUN})",
        )
        return

    if MEMORY_SYNTHESIS_DRY_RUN:
        log.info("memory_synthesis dry_run digest preview: %s", digest[:400])
        runtime_status.mark_job_success(
            "memory_synthesis",
            f"dry_run: arcs={stats['arcs_requested']} stale={stats['stale_requested']} contradictions={stats['contradictions_flagged']}",
        )
        return

    await _post_to_elixir(channel, {"content": digest})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel), "assistant", digest,
        **_channel_msg_kwargs(channel),
        workflow="memory_synthesis",
        event_type="weekly_memory_synthesis",
    )
    await asyncio.to_thread(
        upsert_weekly_summary_memory,
        event_type="weekly_memory_synthesis",
        title="Weekly Memory Synthesis",
        body=digest,
        scope="leadership",
        tags=["weekly", "memory", "synthesis"],
        metadata={
            "channel_id": channel.id,
            "workflow": "memory_synthesis",
            "arcs_written": stats["arcs_written"],
            "stale_expired": stats["stale_expired"],
            "contradictions_flagged": stats["contradictions_flagged"],
        },
    )
    runtime_status.mark_job_success(
        "memory_synthesis",
        f"digest posted (arcs={stats['arcs_written']}, stale={stats['stale_expired']}, contradictions={stats['contradictions_flagged']})",
    )
