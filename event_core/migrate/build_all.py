"""Stage 3 — build all three v5 stores from the frozen archive (one command).

Production build entrypoint:
  1. build_foundation  -> event store + World projections (elixir-v5.db)
  2. advance()         -> Mind layer (detectors/leadership/policy/detections),
                          run from position 0 == full Mind build
  3. copy_survivors    -> operational survivors into elixir-v5.db
  4. build_memory_db   -> elixir-v5-memory.db

Reproducible + idempotent: re-running rebuilds deterministically. This is the
command Stage 3 of the cutover runbook executes.
"""
from __future__ import annotations

import json

from event_core import config


def build_all() -> dict:
    from event_core import build_foundation, db
    from event_core.application import ObservedWorld
    from event_core.live.engine import advance
    from event_core.migrate import build_memory_db, build_projection_db

    foundation = build_foundation.build()

    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    conn = db.connect(config.PROJECTIONS_DB)
    mind = advance(app, conn)
    conn.close()

    survivors = build_projection_db.copy_survivors()
    memory = build_memory_db.build()

    return {
        "foundation_parity": {k: v for k, v in foundation["parity"].items()},
        "mind": mind,
        "survivors": survivors["survivors"],
        "memory": {k: memory[k] for k in ("db", "fts_rows")},
    }


if __name__ == "__main__":
    print(json.dumps(build_all(), indent=2, default=str))
