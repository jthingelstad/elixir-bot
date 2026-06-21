"""Stage 1 — build elixir-v5-memory.db from the frozen legacy DB.

Memory/embeddings split out (Core Decision 6). Plain content tables are copied via
ATTACH + INSERT…SELECT; the FTS5 and sqlite-vec virtual tables cannot be
file-copied, so they are recreated and rebuilt from content (FTS5 'rebuild'; the
vec table is empty — 0 embeddings — so nothing to re-insert).

Idempotent: rebuilds the file from scratch each run.
"""
from __future__ import annotations

import os
import sqlite3

import sqlite_vec

from event_core import config

VIRTUAL = {"clan_memories_fts", "clan_memory_vec"}


def _is_memory_table(name: str) -> bool:
    return name.startswith("clan_memor") or name.startswith("memory_")


def _is_virtual_or_shadow(name: str) -> bool:
    # the virtual tables themselves + their auto-managed shadow tables
    return name.startswith("clan_memories_fts") or name.startswith("clan_memory_vec")


def build(legacy_path: str | None = None, out_path: str | None = None) -> dict:
    legacy_path = legacy_path or config.LEGACY_DB
    out_path = out_path or config.MEMORY_DB
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(out_path + suffix):
            os.remove(out_path + suffix)

    src = sqlite3.connect(legacy_path)
    src.row_factory = sqlite3.Row
    out = sqlite3.connect(out_path)
    out.enable_load_extension(True)
    sqlite_vec.load(out)
    out.enable_load_extension(False)
    try:
        tables = [
            r["name"] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) if _is_memory_table(r["name"])
        ]
        plain = [t for t in tables if not _is_virtual_or_shadow(t)]

        out.execute(f"ATTACH DATABASE '{legacy_path}' AS src")
        copied = {}
        for t in plain:
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()["sql"]
            out.execute(ddl)
            out.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
            # plain-table indexes
            for (idx_sql,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,)
            ):
                out.execute(idx_sql)
            copied[t] = out.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        out.commit()  # DETACH cannot run inside a transaction
        out.execute("DETACH DATABASE src")

        # recreate virtual tables from their DDL, then rebuild FTS content
        for vt in ("clan_memories_fts", "clan_memory_vec"):
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (vt,)
            ).fetchone()
            if ddl:
                out.execute(ddl["sql"])
        out.execute("INSERT INTO clan_memories_fts(clan_memories_fts) VALUES('rebuild')")
        out.commit()

        fts_rows = out.execute("SELECT count(*) FROM clan_memories_fts").fetchone()[0]
        return {"db": out_path, "tables_copied": copied, "fts_rows": fts_rows}
    finally:
        src.close()
        out.close()


if __name__ == "__main__":
    import json

    print(json.dumps(build(), indent=2, default=str))
