"""Build elixir-v5-memory.db (durable knowledge: clan_memories* only).

Sourced from the LIVE operational DB (db.DB_PATH) so the current authoritative
memories/embeddings are copied — NOT the frozen legacy oracle. memory_episodes/
memory_facts stay in the operational DB and are intentionally excluded here.

Schema-first: the canonical clan_memory schema (incl. FTS5 sync triggers) is
created from memory_store.CLAN_MEMORY_SCHEMA_SQL, then data is copied in. The
INSERT into clan_memories fires the FTS triggers, so search stays in sync going
forward — closing the trigger-missing gap of the old build.

Idempotent: rebuilds the file from scratch each run.
"""
from __future__ import annotations

import os
import sqlite3

from event_core import config

# Plain clan_memory* tables to copy (virtual/shadow FTS + vec tables are recreated
# by the schema/sqlite-vec setup, not file-copied). clan_memory_index_status is
# seeded by the schema, so it is not copied.
_COPY_TABLES = (
    "clan_memories",
    "clan_memory_tags",
    "clan_memory_tag_links",
    "clan_memory_member_links",
    "clan_memory_event_links",
    "clan_memory_evidence_refs",
    "clan_memory_versions",
    "clan_memory_audit_log",
    "clan_memory_embeddings",
)


def build(source_path: str | None = None, out_path: str | None = None) -> dict:
    from memory_store import CLAN_MEMORY_SCHEMA_SQL, get_memory_connection

    if source_path is None:
        import db as _opdb

        source_path = _opdb.DB_PATH
    out_path = out_path or config.MEMORY_DB
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(out_path + suffix):
            os.remove(out_path + suffix)

    src = sqlite3.connect(source_path)
    src.row_factory = sqlite3.Row
    # get_memory_connection creates the canonical clan_memory schema (tables, FTS,
    # triggers, indexes) and loads sqlite-vec on the fresh out file.
    out = get_memory_connection(out_path)
    try:
        src_tables = {
            r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        out.execute(f"ATTACH DATABASE '{source_path}' AS src")
        copied = {}
        for t in _COPY_TABLES:
            if t not in src_tables:
                copied[t] = "absent-in-source"
                continue
            out.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
            copied[t] = out.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        out.commit()  # DETACH cannot run inside a transaction
        out.execute("DETACH DATABASE src")
        out.commit()

        fts_rows = out.execute("SELECT count(*) FROM clan_memories_fts").fetchone()[0]
        return {"db": out_path, "tables_copied": copied, "fts_rows": fts_rows}
    finally:
        src.close()
        out.close()


if __name__ == "__main__":
    import json

    print(json.dumps(build(), indent=2, default=str))
