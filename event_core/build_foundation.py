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
    from event_core.backfill import backfill_players
    from event_core.parity import check_player_profile_parity
    from event_core.projections.player_state import PlayerCurrentProfile

    app = ObservedWorld()
    bf = backfill_players(app)

    conn = db.connect(config.PROJECTIONS_DB)
    proj = PlayerCurrentProfile(app, conn)
    proj.reset()
    applied = proj.run()
    conn.close()

    parity = check_player_profile_parity()
    return {"backfill": bf, "projection_events_applied": applied, "parity": parity}


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, default=str))
