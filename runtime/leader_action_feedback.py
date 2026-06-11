"""Agentic synthesis loop for #arena-relay leader action feedback."""

from __future__ import annotations

import asyncio
import logging

import db

log = logging.getLogger("elixir.leader_action_feedback")


def refresh_leader_action_feedback_profile(*, action_type: str | None, limit: int = 50) -> dict | None:
    clean_type = (action_type or "").strip()
    if not clean_type:
        return None
    context = db.build_leader_action_feedback_synthesis_context(
        action_type=clean_type,
        limit=limit,
    )
    if not (context.get("counts") or {}).get("total"):
        return None

    import elixir_agent

    profile = elixir_agent.synthesize_leader_action_feedback(context)
    if not isinstance(profile, dict) or profile.get("_error"):
        log.warning(
            "leader_action_feedback synthesis failed action_type=%s error=%s",
            clean_type,
            profile.get("_error") if isinstance(profile, dict) else type(profile).__name__,
        )
        return None
    return db.upsert_leader_action_feedback_profile(
        action_type=clean_type,
        profile=profile,
    )


async def _refresh_leader_action_feedback_profile_async(action_type: str) -> None:
    try:
        await asyncio.to_thread(refresh_leader_action_feedback_profile, action_type=action_type)
    except Exception:
        log.warning("leader_action_feedback background refresh failed action_type=%s", action_type, exc_info=True)


def queue_leader_action_feedback_refresh(action_type: str | None):
    clean_type = (action_type or "").strip()
    if not clean_type:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return refresh_leader_action_feedback_profile(action_type=clean_type)
    return loop.create_task(_refresh_leader_action_feedback_profile_async(clean_type))


__all__ = [
    "queue_leader_action_feedback_refresh",
    "refresh_leader_action_feedback_profile",
]
