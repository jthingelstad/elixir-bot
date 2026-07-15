"""Durable clan memory for Elixir releases."""

from __future__ import annotations

from memory_store import list_memories
from storage.contextual_memory import upsert_release_memory


def _releases(conn):
    return list_memories(
        viewer_scope="system_internal",
        include_system_internal=True,
        filters={"event_type": "elixir_release", "event_id": "blazing-balloon"},
        limit=5,
        conn=conn,
    )


def test_upsert_release_memory_records(engine_conn):
    mem = upsert_release_memory(
        name="Blazing Balloon",
        date="2026-07-08",
        tag="blazing-balloon",
        subject="Shiny",
        body="I learned to shine.",
        url="https://github.com/x/y/releases/tag/blazing-balloon",
        conn=engine_conn,
    )
    assert mem and mem["title"] == "Elixir release — Blazing Balloon (2026-07-08)"
    assert mem["body"] == "I learned to shine."
    rows = _releases(engine_conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Elixir release — Blazing Balloon (2026-07-08)"


def test_upsert_release_memory_is_idempotent(engine_conn):
    upsert_release_memory(
        name="Blazing Balloon",
        date="2026-07-08",
        tag="blazing-balloon",
        body="v1",
        conn=engine_conn,
    )
    upsert_release_memory(
        name="Blazing Balloon",
        date="2026-07-08",
        tag="blazing-balloon",
        body="v2 — updated",
        conn=engine_conn,
    )
    rows = _releases(engine_conn)
    assert len(rows) == 1  # upsert, not duplicate
    assert "v2" in rows[0]["body"]
