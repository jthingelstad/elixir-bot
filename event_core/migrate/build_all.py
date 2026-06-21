"""Stage 3 — rebuild the v5 stores from the frozen archive (migration-era tool).

  1. build_foundation  -> event store + World projections
  2. advance()         -> Mind layer (detectors/leadership/policy/detections)
  3. build_memory_db   -> memory DB

DANGER post-consolidation: build_foundation wipes EVENTS_DB/PROJECTIONS_DB and
rebuilds from the FROZEN legacy oracle. Now that elixir-v5.db is the live
operational DB, build_foundation refuses to run against it (and copy_survivors,
which clobbered live tables with stale legacy data, has been removed from this
pipeline). Use this only against throwaway/isolated stores.
"""
from __future__ import annotations

import json

from event_core import config


def build_all() -> dict:
    from event_core import build_foundation, db
    from event_core.application import ObservedWorld
    from event_core.live.engine import advance
    from event_core.migrate import build_memory_db

    foundation = build_foundation.build()

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    mind = advance(app, conn)
    conn.close()

    # copy_survivors was retired at the v5 consolidation: operational survivors now
    # live in the consolidated operational DB, not copied from the frozen legacy.
    memory = build_memory_db.build()

    return {
        "foundation_parity": {k: v for k, v in foundation["parity"].items()},
        "mind": mind,
        "memory": {k: memory[k] for k in ("db", "fts_rows")},
    }


if __name__ == "__main__":
    print(json.dumps(build_all(), indent=2, default=str))
