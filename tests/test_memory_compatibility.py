"""Compatibility coverage for callers retained after the v5.1 memory rebuild.

The retired clan-memory schema, embeddings, evidence satellites, and
``memory_facts`` behavior are covered by the migration archive, not by live
runtime tests. Current store semantics live primarily in test_memory_v51.py.
"""

import pytest

import db
from memory_store import (
    MemoryValidationError,
    attach_tags,
    create_memory,
    list_memories,
)
from runtime.admin import _build_memory_report
from storage.contextual_memory import (
    archive_member_note_memory,
    upsert_member_note_memory,
)
from storage.messages import update_message_summary


def _memory_conn():
    from memory_store import ensure_memory_schema

    conn = db.get_connection()
    ensure_memory_schema(conn)
    return conn


def test_provenance_rules_and_retrieval_payload():
    conn = _memory_conn()
    try:
        item = create_memory(
            body="Pattern suggests slight disengagement.",
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.7,
            created_by="elixir",
            scope="leadership",
            conn=conn,
        )
        assert item["source_type"] == "elixir_inference"
        assert item["is_inference"] == 1
        assert item["confidence"] == 0.7

        with pytest.raises(MemoryValidationError):
            create_memory(
                body="bad",
                source_type="elixir_inference",
                is_inference=True,
                confidence=1.0,
                created_by="elixir",
                conn=conn,
            )
    finally:
        conn.close()


def test_member_note_memory_is_upserted_and_archived():
    conn = _memory_conn()
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "elder"}],
            conn=conn,
        )
        created = upsert_member_note_memory(
            member_tag="#ABC123",
            member_label="King Levy",
            note="Reliable war participant and strong leader presence.",
            conn=conn,
        )
        updated = upsert_member_note_memory(
            member_tag="#ABC123",
            member_label="King Levy",
            note="Reliable war participant and consistent clan leader.",
            conn=conn,
        )

        rows = list_memories(viewer_scope="leadership", conn=conn)
        assert len(rows) == 1
        assert rows[0]["memory_id"] == created["memory_id"] == updated["memory_id"]
        assert rows[0]["source_event_key"] == "member_note:#ABC123"
        assert rows[0]["body"].endswith("consistent clan leader.")

        archived = archive_member_note_memory(member_tag="#ABC123", conn=conn)
        assert archived["status"] == "archived"
        assert list_memories(viewer_scope="leadership", conn=conn) == []
    finally:
        conn.close()


def test_memory_report_includes_internal_search_and_conversation_counts():
    conn = _memory_conn()
    try:
        create_memory(
            body="Internal tuning note for memory index degradation.",
            summary="Memory index degradation",
            source_type="system",
            is_inference=False,
            confidence=1.0,
            created_by="system",
            scope="system_internal",
            conn=conn,
        )
        db.save_message(
            "leader:user123",
            "user",
            "Who should we promote?",
            discord_user_id="user123",
            username="jamie",
            display_name="Jamie",
            conn=conn,
        )

        report = _build_memory_report(
            query="index degradation",
            include_system_internal=True,
            limit=3,
            conn=conn,
        )
        assert "- View: `system_internal`" in report
        assert "Memory index degradation" in report
        assert "Conversation store:" in report
    finally:
        conn.close()


def test_message_summary_updates_current_channel_and_user_paths():
    conn = _memory_conn()
    try:
        channel_message_id = db.save_message(
            "channel:ch100",
            "assistant",
            "A detailed war recap covering rankings and highlights.",
            channel_id="ch100",
            channel_name="river-race",
            channel_kind="text",
            workflow="channel_update",
            conn=conn,
        )
        update_message_summary(
            channel_message_id,
            "War recap covering rankings and player highlights.",
            conn=conn,
        )
        state = db.get_channel_state("ch100", conn=conn)
        assert (
            state["last_summary"]
            == "War recap covering rankings and player highlights."
        )

        user_message_id = db.save_message(
            "leader:user789",
            "user",
            "A long message about war strategy.",
            discord_user_id="user789",
            username="strategist",
            display_name="Strategist",
            conn=conn,
        )
        update_message_summary(user_message_id, "War strategy discussion.", conn=conn)
        row = conn.execute(
            "SELECT summary FROM messages WHERE message_id = ?",
            (user_message_id,),
        ).fetchone()
        assert row["summary"] == "War strategy discussion."
    finally:
        conn.close()


def test_inference_writer_creates_current_memories():
    from agent.memory_tasks import save_inference_facts

    conn = _memory_conn()
    try:
        saved = save_inference_facts(
            [
                {
                    "title": "raquaza is war leader",
                    "body": "raquaza serves as the primary war leader and clan founder.",
                    "confidence": 0.9,
                    "scope": "leadership",
                    "tags": ["member-note", "leadership"],
                    "member_tag": None,
                },
                {
                    "title": "Free Pass Royale policy",
                    "body": "Free Pass Royale follows the ratified seasonal rotation.",
                    "confidence": 0.95,
                    "scope": "leadership",
                    "tags": ["decision", "war"],
                    "member_tag": None,
                },
            ],
            conn=conn,
        )
        memories = list_memories(
            viewer_scope="leadership",
            filters={"source_type": "elixir_inference"},
            conn=conn,
        )
        assert saved == len(memories) == 2
        assert all(memory["is_inference"] == 1 for memory in memories)
        assert all(float(memory["confidence"]) < 1.0 for memory in memories)
    finally:
        conn.close()


def test_leader_note_writer_preserves_tags_and_scope():
    conn = _memory_conn()
    try:
        memory = create_memory(
            title="Promotion freeze until next season",
            body="Leadership decided to freeze promotions until the next war season.",
            summary="Promotion freeze decision",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader:elixir-tool",
            scope="leadership",
            conn=conn,
        )
        attach_tags(
            memory["memory_id"],
            ["decision", "leadership"],
            actor="leader:elixir-tool",
            conn=conn,
        )

        rows = list_memories(viewer_scope="leadership", conn=conn)
        assert len(rows) == 1
        assert rows[0]["scope"] == "leadership"
        assert {"decision", "leadership"} <= set(rows[0]["tags"])
    finally:
        conn.close()
