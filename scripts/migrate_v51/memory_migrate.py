"""v5.1 memory migration — memory.md §3 (M1–M3). Content, not structure.

Reads the OLD memory DB read-only; writes the new `memories`/`memory_tags`/
`memory_log` tables in the engine DB. Idempotent: clears and reloads the new
tables (safe — they are only written by this script until the seam-swap
deploy). memory_episodes already live in the engine DB (T12) and stay in
place — M2 is a verify, not a copy.

Usage:
    uv run python scripts/migrate_v51/memory_migrate.py \
        --db elixir-v51.db --memory-db elixir-v5-memory.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

KIND_MAP = {
    "leader_note": "leader_note",
    "elixir_inference": "inference",
    "elixir_synthesis": "synthesis",
    "system": "system",
}


def run(db_path: str, memory_db_path: str) -> int:
    from db.schema import apply_schema_migrations

    old = sqlite3.connect(f"file:{memory_db_path}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    new = sqlite3.connect(db_path)
    new.row_factory = sqlite3.Row
    new.execute("PRAGMA busy_timeout = 30000")
    apply_schema_migrations(new)

    # Idempotency: clear-and-reload (memory_log of the migration reload too).
    new.execute("DELETE FROM memory_tags")
    new.execute("DELETE FROM memory_log")
    new.execute("DELETE FROM memories")

    # M1 — clan_memories → memories (ids preserved; scope system_internal→leadership)
    src_rows = old.execute(
        """SELECT memory_id, created_at, updated_at, created_by, source_type,
                  confidence, scope, status, title, body, summary, member_tag,
                  channel_id, event_type, event_id, expires_at
           FROM clan_memories"""
    ).fetchall()
    migrated = 0
    for r in src_rows:
        kind = KIND_MAP.get(r["source_type"], "system")
        scope = "leadership" if r["scope"] == "system_internal" else r["scope"]
        ek = None
        if r["event_type"] and r["event_id"]:
            ek = f"{r['event_type']}:{r['event_id']}"
        elif r["event_type"]:
            ek = r["event_type"]
        new.execute(
            """INSERT INTO memories (memory_id, kind, title, body, summary, scope,
                   confidence, member_tag, channel_key, source_event_key,
                   created_by, created_at, updated_at, expires_at, retired_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["memory_id"],
                kind,
                r["title"] or (r["body"] or "")[:80] or "(untitled)",
                r["body"],
                r["summary"],
                scope,
                float(r["confidence"] or 0.9),
                r["member_tag"],
                r["channel_id"],
                ek,
                r["created_by"],
                r["created_at"],
                r["updated_at"],
                r["expires_at"],
                r["updated_at"] if r["status"] in ("archived", "deleted") else None,
            ),
        )
        migrated += 1

    # Tags: flatten tag registry + links → memory_tags
    tag_rows = old.execute(
        """SELECT l.memory_id, t.tag FROM clan_memory_tag_links l
           JOIN clan_memory_tags t ON t.tag_id = l.tag_id"""
    ).fetchall()
    for r in tag_rows:
        new.execute(
            "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
            (r["memory_id"], (r["tag"] or "").strip().lower()),
        )

    # M2 re-key — member episodes carried Gen-A integer member_id keys
    # (found live 2026-07-04: tag lookups returned nothing since the cut).
    # Map via the archive's members table; idempotent (skips '#'-keyed rows).
    archive = os.path.join(os.path.dirname(db_path) or ".", "elixir-v5-archive-2026H2.db")
    rekeyed = 0
    if os.path.exists(archive):
        arch = sqlite3.connect(f"file:{archive}?immutable=1", uri=True)
        id_to_tag = {
            str(r[0]): r[1] for r in arch.execute("SELECT member_id, player_tag FROM members")
        }
        arch.close()
        for r in new.execute(
            "SELECT DISTINCT subject_key FROM memory_episodes "
            "WHERE subject_type='member' AND subject_key NOT LIKE '#%'"
        ).fetchall():
            tag = id_to_tag.get(str(r[0]))
            if tag:
                new.execute(
                    "UPDATE memory_episodes SET subject_key = ? "
                    "WHERE subject_type='member' AND subject_key = ?",
                    (tag, r[0]),
                )
                rekeyed += 1

    # M3 — memory_facts (engine DB, dead since 06-16) → one legacy system memory
    facts = (
        new.execute(
            "SELECT subject_type, subject_key, fact_type, fact_value, updated_at "
            "FROM memory_facts ORDER BY subject_type, subject_key"
        ).fetchall()
        if new.execute("SELECT 1 FROM sqlite_master WHERE name='memory_facts'").fetchone()
        else []
    )
    legacy_added = 0
    if facts:
        body = "\n".join(
            f"[{f['subject_type']}:{f['subject_key']}] {f['fact_type']} = {f['fact_value']} ({f['updated_at']})"
            for f in facts
        )
        new.execute(
            """INSERT INTO memories (kind, title, body, summary, scope, confidence,
                   created_by, created_at, updated_at)
               VALUES ('system', 'Legacy memory_facts export (pre-v5.1)', ?,
                       'One-time export of the retired Gen A memory_facts table.',
                       'leadership', 1.0, 'migration:memory_migrate',
                       datetime('now'), datetime('now'))""",
            (body,),
        )
        legacy_added = 1
    new.execute(
        "INSERT INTO memory_log (memory_id, action, actor, at, diff_json) "
        "VALUES (0, 'created', 'migration:memory_migrate', datetime('now'), "
        "json_object('migrated', ?, 'tags', ?, 'legacy_facts_rollup', ?))",
        (migrated, len(tag_rows), legacy_added),
    )
    new.commit()

    # ---- Parity (memory.md §3) ----
    print("=== memory migration parity ===")
    total = new.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    print(f"memories: {total} (source {migrated} + legacy rollup {legacy_added})")
    print("per kind (new):")
    for r in new.execute(
        "SELECT kind, COUNT(*) FROM memories WHERE created_by != 'migration:memory_migrate' GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"  {r[0]}: {r[1]}")
    print("per source_type (old):")
    for r in old.execute(
        "SELECT source_type, COUNT(*) FROM clan_memories GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"  {r[0]}: {r[1]}")
    new_tags = new.execute("SELECT COUNT(*) FROM memory_tags").fetchone()[0]
    old_tags = old.execute("SELECT COUNT(*) FROM clan_memory_tag_links").fetchone()[0]
    print(f"tag links: new {new_tags} / old {old_tags}")
    episodes = new.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0]
    unrekeyed = new.execute(
        "SELECT COUNT(*) FROM memory_episodes "
        "WHERE subject_type='member' AND subject_key NOT LIKE '#%'"
    ).fetchone()[0]
    print(
        f"episodes (in place, M2 verify): {episodes}; member keys re-keyed: "
        f"{rekeyed} subjects ({unrekeyed} unmappable remain)"
    )
    fts = new.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
    print(f"FTS index rows: {fts} (must equal memories: {total})")
    ok = (
        (total == migrated + legacy_added)
        and (new_tags <= old_tags)
        and episodes > 0
        and fts == total
    )
    print("PARITY:", "PASS" if ok else "FAIL")
    old.close()
    new.close()
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="elixir-v51.db")
    parser.add_argument("--memory-db", default="elixir-v5-memory.db")
    args = parser.parse_args()
    return run(args.db, args.memory_db)


if __name__ == "__main__":
    sys.exit(main())
