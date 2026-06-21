"""One-shot v5 DB consolidation (plan A3): merge the v5 projection tables into the
operational DB so a single file (elixir-v5.db) holds everything.

The v5 projection file holds ONLY the 12 v5-owned tables (projections + tracking +
ingest_cursor + battle_telemetry), whose names are all distinct from the v4
operational tables — so this copies them in wholesale. It preserves
projection_tracking verbatim (incl. consumer:discord) so the eventual restart's
catch_up drains flood-safe rather than re-posting.

Usage (cutover does the renames; this only does the in-file merge):
    python scripts/consolidate_v5.py <operational_db> <projections_db>
Returns a JSON report; exits non-zero if integrity_check != 'ok'.
"""
from __future__ import annotations

import json
import sqlite3
import sys


def merge_projections_into(operational_path: str, projections_path: str) -> dict:
    conn = sqlite3.connect(operational_path)
    try:
        conn.execute(f"ATTACH DATABASE '{projections_path}' AS v5src")
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM v5src.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        collisions = sorted(set(tables) & existing)
        if collisions:
            raise RuntimeError(
                f"refusing to merge: v5 tables collide with operational tables: {collisions}"
            )

        copied = {}
        for t in tables:
            ddl = conn.execute(
                "SELECT sql FROM v5src.sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()[0]
            conn.execute(ddl)
            conn.execute(f'INSERT INTO main."{t}" SELECT * FROM v5src."{t}"')
            for (idx_sql,) in conn.execute(
                "SELECT sql FROM v5src.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (t,),
            ):
                conn.execute(idx_sql)
            copied[t] = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE v5src")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

        # sanity: the discord consumer drain position must survive the merge
        consumer_pos = None
        row = conn.execute(
            "SELECT last_global_position FROM projection_tracking WHERE projection_name='consumer:discord'"
        ).fetchone()
        if row:
            consumer_pos = row[0]
        conn.commit()
        return {
            "merged_tables": copied,
            "integrity": integrity,
            "consumer_discord_position": consumer_pos,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: consolidate_v5.py <operational_db> <projections_db>", file=sys.stderr)
        sys.exit(2)
    result = merge_projections_into(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["integrity"] == "ok" else 1)
