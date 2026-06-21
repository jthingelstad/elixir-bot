"""Paths and environment for the three v5 databases.

Persistence decision (2026-06-21): native `eventsourcing.sqlite` for the event
store + stdlib `sqlite3` for projections (behind `event_core.db`). SQLAlchemy was
considered and declined — SQLite-only makes the native event-store module leaner,
projections are mechanical upserts, and a second DB idiom mid-rewrite isn't worth
it. If projection query ergonomics ever justify it, SQLAlchemy Core can be added
inside `event_core.db` alone. See docs migration plan §2/§4.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The library-owned event store (write model).
EVENTS_DB = os.environ.get("ELIXIR_V5_EVENTS_DB", str(ROOT / "elixir-v5-events.db"))
# Our projections / read models + (eventually) operational survivors.
PROJECTIONS_DB = os.environ.get("ELIXIR_V5_DB", str(ROOT / "elixir-v5.db"))
# Memory / embeddings (split out; not used by the foundation slice).
MEMORY_DB = os.environ.get("ELIXIR_V5_MEMORY_DB", str(ROOT / "elixir-v5-memory.db"))
# Frozen pre-v5 production DB: backfill source + parity oracle.
LEGACY_DB = os.environ.get("ELIXIR_LEGACY_DB", str(ROOT / "elixir.db.legacy"))


def configure_eventstore_env(dbname: str | None = None) -> None:
    """Point the eventsourcing library at our SQLite event store.

    Must be called before constructing an Application.
    """
    os.environ["PERSISTENCE_MODULE"] = "eventsourcing.sqlite"
    os.environ["SQLITE_DBNAME"] = dbname or EVENTS_DB
