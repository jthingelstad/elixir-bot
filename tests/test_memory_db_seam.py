"""v5.1 memory seam tests (memory.md D1, ratified 2026-07-04).

This file's old subject — the separate elixir-v5-memory.db seam — is retired:
memories live in the ENGINE DB. These tests pin the new seam: default
connections land in the operational DB and FTS stays in sync via triggers.
"""

from __future__ import annotations

import db
from memory_store import (
    create_memory,
    get_memory,
    get_memory_connection,
    search_memories,
)


def test_memory_schema_is_owned_by_central_contract():
    conn = db.get_connection()
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'memories_fts'"
        ).fetchone()
    finally:
        conn.close()


def test_default_writes_land_in_engine_db():
    memory = create_memory(
        body="seam test body",
        source_type="system",
        is_inference=False,
        confidence=1.0,
        created_by="test",
        scope="public",
        title="Seam test",
    )
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT kind, scope FROM memories WHERE memory_id = ?",
            (memory["memory_id"],),
        ).fetchone()
        assert row is not None and row["kind"] == "system"
    finally:
        conn.close()


def test_explicit_conn_threads_through():
    conn = get_memory_connection()
    try:
        memory = create_memory(
            body="threaded",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="test",
            scope="leadership",
            title="Thread",
            conn=conn,
        )
        assert get_memory(memory["memory_id"], conn=conn) is not None
    finally:
        conn.close()


def test_fts_triggers_keep_search_in_sync():
    conn = get_memory_connection()
    try:
        create_memory(
            body="Unique-token zephyrqualm appears here.",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader:test",
            scope="leadership",
            title="Zephyr",
            conn=conn,
        )
        results = search_memories("zephyrqualm", viewer_scope="leadership", conn=conn)
        assert any("zephyrqualm" in (r.memory.get("body") or "") for r in results)
    finally:
        conn.close()
