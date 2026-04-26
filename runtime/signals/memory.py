"""Memory persistence helpers for signal-generated posts."""

from __future__ import annotations

import asyncio
import logging

from storage.contextual_memory import upsert_race_streak_memory, upsert_war_recap_memory

log = logging.getLogger("elixir")


async def _post_signal_memory(body, outcome, signals):
    try:
        from agent.memory_tasks import extract_inference_facts, save_inference_facts

        context_label = f"signal:{outcome.get('intent', 'unknown')} in #{outcome.get('target_channel_key', 'unknown')}"
        facts = await asyncio.to_thread(extract_inference_facts, body, context_label)
        if facts:
            channel_id = outcome.get("target_channel_id")
            await asyncio.to_thread(save_inference_facts, facts, channel_id)
    except Exception:
        log.warning("_post_signal_memory failed", exc_info=True)


def _store_recap_memories_for_signal_batch(signal_batch, posts, channel_id):
    body = "\n\n".join((post or "").strip() for post in (posts or []) if (post or "").strip())
    if not body:
        return None
    recap = upsert_war_recap_memory(
        signals=signal_batch,
        body=body,
        channel_id=channel_id,
        workflow="observation",
    )
    streak_signal_types = {"war_week_complete", "war_completed"}
    for signal in (signal_batch or []):
        if signal.get("type") in streak_signal_types:
            season_id = signal.get("season_id")
            week = signal.get("week")
            if week is None and signal.get("section_index") is not None:
                week = int(signal["section_index"]) + 1
            race_rank = signal.get("race_rank") or signal.get("rank")
            if season_id is not None and week is not None and race_rank is not None:
                try:
                    upsert_race_streak_memory(
                        season_id=season_id,
                        week=week,
                        race_rank=race_rank,
                    )
                except Exception:
                    log.warning("Failed to update race streak memory", exc_info=True)
            break
    return recap
