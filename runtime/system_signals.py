from __future__ import annotations

import logging

from storage.contextual_memory import upsert_race_streak_memory

log = logging.getLogger(__name__)


def _seed_race_streak_memory(*, conn=None) -> None:
    """Seed the race win streak identity memory on first deploy."""
    from memory_store import list_memories

    existing = list_memories(
        viewer_scope="system_internal",
        include_system_internal=True,
        filters={"event_type": "clan_identity", "event_id": "race_win_streak"},
        limit=1,
        conn=conn,
    )
    if existing:
        return  # Already seeded
    try:
        upsert_race_streak_memory(season_id=0, week=0, race_rank=1, conn=conn)
        log.info("Seeded race win streak identity memory")
    except Exception:
        log.warning("Failed to seed race streak memory", exc_info=True)


def seed_startup_state(*, conn=None) -> None:
    """One-time state seeding on boot.

    Was `queue_startup_system_signals`, which also queued nine hardcoded
    `capability_unlock` announcements ("Achievement Unlocked: New Brain") into
    `system_signals`. That queue had no drain -- its publisher was removed from
    Discord and the Observatory never picked it up -- and release news now goes
    out through the real release flow (RELEASES.md + #announcements + email via
    scripts/cut_release.py). Retired with the queue in #212; the memory seeding
    it also did is live and stays.
    """
    _seed_race_streak_memory(conn=conn)


__all__ = ["seed_startup_state"]
