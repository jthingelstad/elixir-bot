"""Stage 2 — operational survivors into elixir-v5.db (RETIRED at consolidation).

Historical: during the migration this copied operational-survivor tables from the
frozen legacy DB into a freshly-rebuilt elixir-v5.db. After the v5 consolidation
(elixir-v5.db == the live operational DB), this is DANGEROUS — it would overwrite
live operational tables with stale frozen-legacy data. It is no longer called by
build_all and is guarded to refuse running against the operational DB.
"""
from __future__ import annotations

import os
import sqlite3

from event_core import config

# Classified from the 82 legacy tables: not memory (-> memory DB) and not
# event-core-replaced (-> projections/events). `awards` is borderline (may later
# become a season-award projection) — copied to preserve data, flagged in STATUS.
SURVIVOR_TABLES = (
    "discord_users",
    "discord_links",
    "discord_channels",
    "channel_state",
    "conversation_threads",
    "messages",
    "llm_calls",
    "system_signals",
    "cake_day_announcements",
    "prompt_failures",
    "prompt_feedback",
    "elixir_improvement_suggestions",
    "elixir_projects",
    "project_event_links",
    "awards",
)


def copy_survivors(legacy_path: str | None = None, target_path: str | None = None) -> dict:
    legacy_path = legacy_path or config.LEGACY_DB
    target_path = target_path or config.PROJECTIONS_DB

    try:
        import db as _opdb

        if os.path.realpath(target_path) == os.path.realpath(_opdb.DB_PATH):
            raise RuntimeError(
                f"copy_survivors refused: target {target_path} is the live operational "
                "DB. This function copies STALE frozen-legacy tables and was retired at "
                "the v5 consolidation — operational survivors already live in the DB."
            )
    except ImportError:
        pass

    conn = sqlite3.connect(target_path)
    conn.execute(f"ATTACH DATABASE '{legacy_path}' AS src")
    copied = {}
    try:
        for t in SURVIVOR_TABLES:
            ddl = conn.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not ddl:
                copied[t] = "absent-in-legacy"
                continue
            conn.execute(f"DROP TABLE IF EXISTS main.{t}")
            conn.execute(ddl[0])
            conn.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t}")
            for (idx_sql,) in conn.execute(
                "SELECT sql FROM src.sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,)
            ):
                conn.execute(idx_sql)
            copied[t] = conn.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        conn.commit()
        conn.execute("DETACH DATABASE src")
        return {"db": target_path, "survivors": copied}
    finally:
        conn.close()


if __name__ == "__main__":
    import json

    print(json.dumps(copy_survivors(), indent=2, default=str))
