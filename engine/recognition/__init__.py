"""Recognition orchestration — tick step 6 (runtime.md §2; recognition.md §1).

Battle + player streams feed one shared celebrate pipeline so same-tick
coalescing works across both (a trophy push and a level-up in the same tick
are one post, not two); clan and war moments take the direct path. Cursors:
'recognize:battle' tracks battle_events.rowid; the others track event_id.
"""

from __future__ import annotations

import logging

from engine.db import cursor_set

log = logging.getLogger("elixir.engine.recognition")


def run_recognizers(conn, clock: dict | None, now: str) -> dict:
    """Run all four recognizers over events since their cursors. Returns
    counters. Cursor discipline (runtime.md §2): each section advances its own
    cursor only after its work lands; a throwing section leaves its cursor
    unmoved and the idempotent ledger/dedup keys make re-processing safe."""
    from engine.recognition import recognizers as R

    counters: dict[str, int] = {}

    battle_cands, battle_pos = R.battle_candidates(conn, now)
    player_cands, player_pos = R.player_candidates(conn)
    counters.update(R.run_celebrate_pipeline(conn, battle_cands + player_cands, now))
    if battle_pos:
        cursor_set(conn, "recognize:battle", battle_pos)
    if player_pos:
        cursor_set(conn, "recognize:player", player_pos)

    counters.update(R.clan_recognizer(conn, now))
    counters.update(R.war_recognizer(conn, clock, now))
    return counters
