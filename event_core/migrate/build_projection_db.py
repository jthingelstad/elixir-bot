"""Stage 2 — operational survivors into elixir-v5.db (the v5 schema baseline).

Projection tables are created by the projection runners (self-CREATE); the library
owns the event store. This module copies the remaining operational-survivor tables
(Discord plumbing, llm_calls, prompt/project tracking, system signals) from the
frozen legacy DB into elixir-v5.db. Runs AFTER build_foundation (which wipes and
recreates elixir-v5.db) and only touches survivor tables.

The legacy 54-migration chain (db/_migrations.py) is retired at decommission
(Stage 8); the v5 baseline is "projections self-create + these survivors copied".
"""
from __future__ import annotations

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
