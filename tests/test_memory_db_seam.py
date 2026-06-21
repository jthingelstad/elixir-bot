"""Tests for the durable-memory DB seam (elixir-v5-memory.db).

clan_memories* live in their own DB. memory_store ops default (conn=None) to that
DB via get_memory_connection; passing an explicit conn still threads through (used
by tests and the unified in-memory topology).
"""
from __future__ import annotations

import db
from memory_store import (
    CLAN_MEMORY_SCHEMA_SQL,
    create_memory,
    get_memory,
    get_memory_connection,
    search_memories,
    upsert_embedding,
)


def test_default_writes_go_to_memory_db_not_operational():
    mem = create_memory(
        body="Leader noted strong war attendance.",
        source_type="leader_note",
        is_inference=False,
        confidence=1.0,
        created_by="leader:test",
    )
    mid = mem["memory_id"]

    # present in the memory DB (default routing)
    assert get_memory(mid) is not None
    mc = get_memory_connection()
    try:
        assert mc.execute("SELECT COUNT(*) FROM clan_memories WHERE memory_id=?", (mid,)).fetchone()[0] == 1
    finally:
        mc.close()

    # absent from the operational DB
    oc = db.get_connection()
    try:
        assert oc.execute("SELECT COUNT(*) FROM clan_memories WHERE memory_id=?", (mid,)).fetchone()[0] == 0
    finally:
        oc.close()


def test_fts_triggers_keep_search_in_sync():
    create_memory(
        body="Unique-token zephyrqualm appears here.",
        source_type="leader_note",
        is_inference=False,
        confidence=1.0,
        created_by="leader:test",
    )
    results = search_memories("zephyrqualm", viewer_scope="leadership")
    assert any("zephyrqualm" in (r.memory.get("body") or "") for r in results)


def test_embedding_roundtrip_on_memory_db():
    mem = create_memory(
        body="Embeddable note.", source_type="leader_note", is_inference=False,
        confidence=1.0, created_by="leader:test",
    )
    upsert_embedding(mem["memory_id"], [0.1] * 1536)
    mc = get_memory_connection()
    try:
        row = mc.execute(
            "SELECT embedding_model FROM clan_memory_embeddings WHERE memory_id=?",
            (mem["memory_id"],),
        ).fetchone()
        assert row is not None
    finally:
        mc.close()


def test_memory_schema_matches_operational_clan_memories():
    """Guards against drift: CLAN_MEMORY_SCHEMA_SQL must track the operational
    clan_memories schema produced by the migration chain."""
    oc = db.get_connection()  # operational tmp, full v4 migrations
    mc = get_memory_connection()  # memory tmp, canonical schema
    try:
        def cols(c):
            return [
                (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                for r in c.execute("PRAGMA table_info(clan_memories)")
            ]

        assert cols(oc) == cols(mc)
        osql = oc.execute("SELECT sql FROM sqlite_master WHERE name='clan_memories'").fetchone()[0]
        # both carry the widened source_type CHECK (migration 29)
        assert "elixir_synthesis" in osql
        assert "elixir_synthesis" in CLAN_MEMORY_SCHEMA_SQL
    finally:
        oc.close()
        mc.close()
