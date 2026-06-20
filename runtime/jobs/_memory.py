"""Weekly memory synthesis job.

Assembles the week's memories, channel posts, and live clan state, hands
them to the memory-synthesis LLM workflow, persists the resulting arc
memories, and expires stale entries. The digest is stored as durable
memory only. Derived-state memory contradictions are handled automatically;
only genuine leader-judgment contradictions are eligible for #leader-actions.
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
import logging
import os
from datetime import datetime, timedelta, timezone

import db
import elixir_agent
import prompts
from memory_store import update_memory
from storage.contextual_memory import upsert_weekly_summary_memory
from runtime import elixir_log
from runtime.helpers import _channel_scope
from runtime.leader_action_ui import LEADER_ACTION_UI_VERSION, post_leader_action_card
from runtime import status as runtime_status

log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


class _BotProxy:
    def get_channel(self, *args, **kwargs):
        return bot.get_channel(*args, **kwargs)


bot = _BotProxy()


MEMORY_SYNTHESIS_DAY = os.getenv("MEMORY_SYNTHESIS_DAY", "sun")
MEMORY_SYNTHESIS_HOUR = int(os.getenv("MEMORY_SYNTHESIS_HOUR", "22"))
MEMORY_SYNTHESIS_DRY_RUN = os.getenv("MEMORY_SYNTHESIS_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}
MEMORY_SYNTHESIS_MEMORY_LIMIT = int(os.getenv("MEMORY_SYNTHESIS_MEMORY_LIMIT", "80"))
MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT = int(os.getenv("MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT", "12"))
MEMORY_SYNTHESIS_POSTS_PER_CHANNEL = int(os.getenv("MEMORY_SYNTHESIS_POSTS_PER_CHANNEL", "12"))
MEMORY_SYNTHESIS_MEMORY_BODY_CHARS = int(os.getenv("MEMORY_SYNTHESIS_MEMORY_BODY_CHARS", "500"))
MEMORY_SYNTHESIS_POST_CHARS = int(os.getenv("MEMORY_SYNTHESIS_POST_CHARS", "700"))
# Cap contradiction cards per weekly run so a bad synthesis can't flood the
# action board.
MEMORY_CONTRADICTION_CARD_LIMIT = int(os.getenv("MEMORY_CONTRADICTION_CARD_LIMIT", "3"))

DERIVED_STATE_CONTRADICTION_TERMS = {
    "arena",
    "badge",
    "battle",
    "battles",
    "card",
    "collection",
    "deck",
    "donation",
    "donations",
    "elder",
    "fame",
    "level",
    "points",
    "rank",
    "role",
    "roster",
    "season",
    "trophies",
    "trophy",
    "war",
    "wins",
}
DERIVED_STATE_CONTRADICTION_CATEGORIES = {
    "metric_snapshot",
    "derived_state",
    "stale_state",
    "current_state",
    "calculation",
}
LEADER_REVIEW_CONTRADICTION_CATEGORIES = {
    "human_context",
    "policy_or_preference",
    "leader_preference",
    "clan_policy",
    "identity_ambiguity",
    "leader_judgment",
}


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


def _compact_event_row(row: dict) -> dict:
    return {
        "event_key": row.get("event_key"),
        "event_type": row.get("event_type"),
        "scope": row.get("scope"),
        "subject_type": row.get("subject_type"),
        "subject_key": row.get("subject_key"),
        "source_signal_key": row.get("source_signal_key"),
        "season_id": row.get("season_id"),
        "war_week": row.get("war_week"),
        "observed_at": row.get("observed_at"),
    }


def _compact_intent_row(row: dict) -> dict:
    return {
        "intent_id": row.get("intent_id"),
        "workflow": row.get("workflow"),
        "intent_type": row.get("intent_type"),
        "status": row.get("status"),
        "target_channel_key": row.get("target_channel_key"),
        "source_signal_key": row.get("source_signal_key"),
        "source_signal_type": row.get("source_signal_type"),
        "case_id": row.get("case_id"),
        "project_id": row.get("project_id"),
        "summary": _clip_text(row.get("summary"), 200),
        "skipped_reason": _clip_text(row.get("skipped_reason"), 160),
        "updated_at": row.get("updated_at"),
    }


def _contradiction_text(item: dict) -> str:
    fields = (
        "stored",
        "live",
        "suggested_action",
        "category",
        "reason",
        "rationale",
    )
    return " ".join(str(item.get(key) or "") for key in fields).lower()


def _contradiction_category(item: dict) -> str:
    return str(item.get("category") or item.get("contradiction_type") or "").strip().lower()


def _is_derived_state_contradiction(item: dict) -> bool:
    category = _contradiction_category(item)
    if category in DERIVED_STATE_CONTRADICTION_CATEGORIES:
        return True
    text = _contradiction_text(item)
    return any(term in text for term in DERIVED_STATE_CONTRADICTION_TERMS)


def _requires_leader_memory_review(item: dict) -> bool:
    category = _contradiction_category(item)
    if category in DERIVED_STATE_CONTRADICTION_CATEGORIES:
        return False
    if category in LEADER_REVIEW_CONTRADICTION_CATEGORIES:
        return True
    for key in ("needs_leader_review", "requires_leader_review", "requires_leader_judgment"):
        if item.get(key) is True:
            return True
    review_scope = str(item.get("review_scope") or "").strip().lower()
    if review_scope in {"leader", "leadership", "human"}:
        return True
    if _is_derived_state_contradiction(item):
        return False
    return True


def _auto_expire_contradiction_ids(contradictions: list[dict]) -> list[int]:
    ids = []
    for item in contradictions or []:
        if not isinstance(item, dict):
            continue
        if _requires_leader_memory_review(item):
            continue
        memory_id = item.get("memory_id")
        try:
            clean_id = int(memory_id)
        except (TypeError, ValueError):
            continue
        if clean_id not in ids:
            ids.append(clean_id)
    return ids


def _leader_review_contradictions(contradictions: list[dict]) -> list[dict]:
    return [
        item for item in contradictions or []
        if isinstance(item, dict) and _requires_leader_memory_review(item)
    ]


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
            channel = prompts.discord_singleton_lane(key)
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

    operations_context = {}
    try:
        operations_context["event_windows"] = db.summarize_events_by_window(
            windows=(7, 28, 56, 90),
            scope=None,
        )
        operations_context["recent_events"] = [
            _compact_event_row(row)
            for row in db.list_recent_events(days=7, limit=50)
        ]
    except Exception:
        log.warning("memory synthesis: event stream load failed", exc_info=True)
    try:
        operations_context["war_season"] = db.get_war_season_snapshot()
    except Exception:
        log.warning("memory synthesis: war season context load failed", exc_info=True)
    try:
        operations_context["decision_cases"] = db.decision_case_snapshot(
            open_limit=20,
            due_limit=20,
        )
    except Exception:
        log.warning("memory synthesis: decision case context load failed", exc_info=True)
    try:
        operations_context["recent_intents"] = [
            _compact_intent_row(row)
            for row in db.list_recent_communication_intents(limit=25)
        ]
    except Exception:
        log.warning("memory synthesis: communication intent context load failed", exc_info=True)

    return {
        "week_window": {"start": week_ago, "end": now.strftime("%Y-%m-%dT%H:%M:%S"), "war_week_id": week_id},
        "week_memories": [_compact_memory_row(m) for m in week_memories],
        "prior_arcs": [_compact_memory_row(m) for m in prior_arcs],
        "week_posts": posts_by_channel,
        "live_clan_state": clan_state,
        "operations_context": operations_context,
    }


def _apply_memory_synthesis_plan(plan: dict, *, week_id: str | None, dry_run: bool = False) -> dict:
    """Persist arc memories + expire stale ids. Returns a small stats dict.

    Writes happen synchronously in this helper so the caller can thread it
    through ``asyncio.to_thread``. In dry-run mode the function returns
    counts without persisting anything.
    """
    from memory_store import create_memory

    arcs = list(plan.get("arc_memories") or [])
    stale_ids = list(plan.get("stale_memory_ids") or [])
    contradictions = list(plan.get("contradictions") or [])
    auto_expire_ids = _auto_expire_contradiction_ids(contradictions)
    leader_review_items = _leader_review_contradictions(contradictions)

    stats = {
        "arcs_written": 0,
        "stale_expired": 0,
        "contradictions_flagged": len(contradictions),
        "contradictions_auto_expired": 0,
        "contradictions_leader_review": len(leader_review_items),
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
    stale_or_auto_ids = []
    for memory_id in [*stale_ids, *auto_expire_ids]:
        try:
            clean_id = int(memory_id)
        except (TypeError, ValueError):
            continue
        if clean_id not in stale_or_auto_ids:
            stale_or_auto_ids.append(clean_id)

    for memory_id in stale_or_auto_ids:
        try:
            update_memory(memory_id, actor=actor, expires_at=now_stamp)
            stats["stale_expired"] += 1
            if memory_id in auto_expire_ids:
                stats["contradictions_auto_expired"] += 1
        except Exception:
            log.warning("memory synthesis: expire %s failed", memory_id, exc_info=True)

    return stats


async def _post_memory_contradiction_cards(contradictions: list[dict]) -> int:
    """Post one #leader-actions card per leader-judgment contradiction.

    Metric and current-state contradictions are handled automatically by
    expiring the stale memory. Cards are reserved for cases Elixir cannot
    recompute, such as policy, leader preference, or human context.
    """
    review_items = _leader_review_contradictions(contradictions)
    if not review_items:
        return 0
    try:
        channel_config = prompts.discord_singleton_lane("arena-relay")
    except Exception:
        log.info("memory contradiction cards skipped: arena-relay unavailable", exc_info=True)
        return 0
    channel = bot.get_channel(channel_config["id"])
    if not channel:
        log.warning("memory contradiction cards skipped: arena-relay channel not found")
        return 0

    posted = 0
    channel_name = getattr(channel, "name", "arena-relay")
    channel_kind = getattr(channel, "type", "text")
    if channel_kind is not None:
        channel_kind = str(channel_kind)
    for item in review_items[:MEMORY_CONTRADICTION_CARD_LIMIT]:
        memory_id = item.get("memory_id")
        stored = (item.get("stored") or "").strip() or "—"
        live = (item.get("live") or "").strip() or "—"
        suggested = (item.get("suggested_action") or "").strip() or "review"
        prompt_text = f"Review memory #{memory_id}: stored `{stored}` but live state shows `{live}`."
        rationale = f"Weekly synthesis flagged this memory as contradicting live clan state. Suggested: {suggested}."
        action = await asyncio.to_thread(
            db.create_leader_action_recommendation,
            action_type="memory_review",
            objective="memory_hygiene",
            prompt_text=prompt_text,
            rationale=rationale,
            target_channel_key="arena-relay",
            target_channel_id=channel_config["id"],
            source_signal_key=f"memory_contradiction:{memory_id}",
            source_signal_type="memory_contradiction",
            ui_version=LEADER_ACTION_UI_VERSION,
        )
        if not action or action.get("source_message_id"):
            continue
        sent_messages = await post_leader_action_card(channel, action, copy_messages=[])
        if not isinstance(sent_messages, list):
            sent_messages = []
        first_message = sent_messages[0] if sent_messages else None
        await asyncio.to_thread(
            db.save_message,
            _channel_scope(channel), "assistant", prompt_text,
            summary=f"Leader action R{action.get('action_id')}: memory review",
            channel_id=channel_config["id"],
            channel_name=channel_name,
            channel_kind=channel_kind,
            workflow="arena-relay",
            event_type="memory_contradiction",
            discord_message_id=getattr(first_message, "id", None),
            raw_json={"leader_action": action},
        )
        posted += 1
    return posted


async def _memory_synthesis_cycle():
    """Weekly memory-synthesis job.

    Runs Sunday late by default. Assembles the week's memories + posts +
    live state, hands them to ``run_memory_synthesis``, persists the
    resulting arcs, and expires stale entries. There is no digest post.
    Metric/current-state contradictions are expired automatically and logged
    to #elixir-log; only human-judgment contradictions may create
    #leader-actions cards.
    """
    runtime_status.mark_job_start("memory_synthesis")

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

    if MEMORY_SYNTHESIS_DRY_RUN:
        if digest:
            log.info("memory_synthesis dry_run digest preview: %s", digest[:400])
        runtime_status.mark_job_success(
            "memory_synthesis",
            f"dry_run: arcs={stats['arcs_requested']} stale={stats['stale_requested']} contradictions={stats['contradictions_flagged']}",
        )
        return

    if digest:
        # The digest stays in durable memory as the week's canonical summary —
        # it just no longer ships to Discord as a report.
        await asyncio.to_thread(
            upsert_weekly_summary_memory,
            event_type="weekly_memory_synthesis",
            title="Weekly Memory Synthesis",
            body=digest,
            scope="leadership",
            tags=["weekly", "memory", "synthesis"],
            metadata={
                "workflow": "memory_synthesis",
                "arcs_written": stats["arcs_written"],
                "stale_expired": stats["stale_expired"],
                "contradictions_flagged": stats["contradictions_flagged"],
            },
        )

    cards_posted = await _post_memory_contradiction_cards(contradictions)
    if stats.get("contradictions_auto_expired") or stats.get("contradictions_leader_review"):
        lines = [
            "🧠 Memory synthesis hygiene",
            f"Auto-expired metric/current-state memories: {stats.get('contradictions_auto_expired', 0)}",
            f"Leader-review cards: {cards_posted}",
        ]
        auto_ids = _auto_expire_contradiction_ids(contradictions)
        if auto_ids:
            lines.append("Auto-expired IDs: " + ", ".join(f"`#{memory_id}`" for memory_id in auto_ids[:8]))
        try:
            await elixir_log.post_event_async("\n".join(lines))
        except Exception:
            log.warning("memory_synthesis: elixir-log hygiene summary failed", exc_info=True)
    runtime_status.mark_job_success(
        "memory_synthesis",
        "synthesis complete "
        f"(arcs={stats['arcs_written']}, stale={stats['stale_expired']}, "
        f"auto_expired={stats.get('contradictions_auto_expired', 0)}, "
        f"contradiction_cards={cards_posted})",
    )
