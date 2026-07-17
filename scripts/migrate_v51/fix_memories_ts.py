"""One-time: normalize memories/memory_log timestamps to the engine
Z-convention ('%Y-%m-%dT%H:%M:%SZ').

Cold review 2026-07-04 #8: day one of the memory cutover left three formats
in string-compared columns (migrated Z-suffixed, new writes T-no-Z from the
old _utcnow, one space-format row). Idempotent — already-normalized rows
match the target format and are skipped.

Usage: uv run python scripts/migrate_v51/fix_memories_ts.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _normalize(value: str | None) -> str | None:
    if not value:
        return value
    v = str(value).strip().replace(" ", "T")
    v = v.split(".")[0]
    v = v[:-1] if v.endswith("Z") else v
    if len(v) == 16:  # minutes precision → add seconds
        v = v + ":00"
    return v + "Z"


def main() -> int:
    db_path = os.getenv(
        "ELIXIR_DB_PATH",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "elixir-v51.db",
        ),
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    fixed = 0
    for table, id_col, cols in (
        (
            "memories",
            "memory_id",
            ("created_at", "updated_at", "expires_at", "retired_at"),
        ),
        ("memory_log", "log_id", ("at",)),
    ):
        for row in conn.execute(f"SELECT {id_col}, {', '.join(cols)} FROM {table}").fetchall():
            updates = {}
            for col in cols:
                val = row[col]
                norm = _normalize(val)
                if norm != val:
                    updates[col] = norm
            if updates:
                sets = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(
                    f"UPDATE {table} SET {sets} WHERE {id_col} = ?",
                    (*updates.values(), row[id_col]),
                )
                fixed += 1
    conn.commit()
    conn.close()
    print(f"normalized timestamps on {fixed} row(s); done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
