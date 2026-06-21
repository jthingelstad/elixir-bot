"""Thin projection-DB helper (stdlib sqlite3).

The single place projection DB access lives. If SQLAlchemy Core is ever justified
for query ergonomics, it goes here and nowhere else (see config.py rationale).
Followers write their projection rows AND their tracking position through the same
connection/transaction, satisfying the §4.1a co-location rule.
"""
from __future__ import annotations

import sqlite3

from event_core import config

TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS projection_tracking (
    projection_name      TEXT PRIMARY KEY,
    last_global_position INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT
);
"""

# Ingest progress so backfill/live ingest processes each raw archive row exactly
# once across runs. Content-hash dedup alone is not replay-idempotent (replaying
# history from the start re-detects old values as changes); this high-water mark
# on raw_api_payloads.payload_id is the idempotency guard.
INGEST_CURSOR_DDL = """
CREATE TABLE IF NOT EXISTS ingest_cursor (
    source           TEXT PRIMARY KEY,
    last_payload_id  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.PROJECTIONS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(TRACKING_DDL)
    conn.execute(INGEST_CURSOR_DDL)
    conn.commit()
    return conn
