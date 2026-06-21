"""Build the foundation slice end-to-end and report parity.

Clean-builds the v5 event store + projection DB from the frozen legacy archive:
backfill players -> run the current-profile projection -> exact-parity check.
Idempotent: deletes prior v5 events/projection DBs first so it is a from-zero
rebuild every run (this is also the replay-determinism harness).
"""
from __future__ import annotations

import json
import os

from event_core import config


def _rm(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


def build(clean: bool = True) -> dict:
    if clean:
        _rm(config.EVENTS_DB)
        _rm(config.PROJECTIONS_DB)

    config.configure_eventstore_env(config.EVENTS_DB)

    from event_core import db
    from event_core.application import ObservedWorld
    from event_core.backfill import backfill_battles, backfill_clans, backfill_players
    from event_core.parity import (
        check_battle_telemetry_parity,
        check_member_current_state_parity,
        check_player_profile_parity,
    )
    from event_core.projections.member_state import MemberCurrentState
    from event_core.projections.player_state import PlayerCurrentProfile

    app = ObservedWorld()
    bf_players = backfill_players(app)
    bf_clans = backfill_clans(app)
    bf_battles = backfill_battles()

    conn = db.connect(config.PROJECTIONS_DB)
    profile_proj = PlayerCurrentProfile(app, conn)
    profile_proj.reset()
    profile_applied = profile_proj.run()

    roster_proj = MemberCurrentState(app, conn)
    roster_proj.reset()
    roster_applied = roster_proj.run()
    conn.close()

    return {
        "backfill": {"players": bf_players, "clans": bf_clans, "battles": bf_battles},
        "projection_events_applied": {
            "profile": profile_applied,
            "roster": roster_applied,
        },
        "parity": {
            "player_profile": check_player_profile_parity(),
            "member_current_state": check_member_current_state_parity(),
            "battle_telemetry": check_battle_telemetry_parity(),
        },
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, default=str))
