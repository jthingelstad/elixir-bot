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


def _contract_dead_conversation_schema(conn) -> None:
    """Model #224's future contract migration without touching the live DB."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE conversation_threads_contract (
            thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            UNIQUE(scope_type, scope_key)
        );
        INSERT INTO conversation_threads_contract (thread_id, scope_type, scope_key)
        SELECT thread_id, scope_type, scope_key FROM conversation_threads;

        CREATE TABLE messages_contract (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id TEXT UNIQUE,
            thread_id INTEGER NOT NULL
                REFERENCES conversation_threads_contract(thread_id) ON DELETE CASCADE,
            channel_id TEXT,
            discord_user_id TEXT REFERENCES discord_users(discord_user_id) ON DELETE SET NULL,
            member_id INTEGER,
            author_type TEXT NOT NULL,
            workflow TEXT,
            event_type TEXT,
            content TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL,
            raw_json TEXT,
            intent_id INTEGER
        );
        INSERT INTO messages_contract (
            message_id, discord_message_id, thread_id, channel_id,
            discord_user_id, member_id, author_type, workflow, event_type,
            content, summary, created_at, raw_json, intent_id
        )
        SELECT
            message_id, discord_message_id, thread_id, channel_id,
            discord_user_id, member_id, author_type, workflow, event_type,
            content, summary, created_at, raw_json, intent_id
        FROM messages;

        CREATE TABLE channel_state_contract (
            channel_id TEXT PRIMARY KEY,
            last_summary TEXT
        );
        INSERT INTO channel_state_contract (channel_id, last_summary)
        SELECT channel_id, last_summary FROM channel_state;

        DROP TABLE messages;
        DROP TABLE conversation_threads;
        DROP TABLE channel_state;
        DROP TABLE arena_relay_screenshot_observations;
        DROP TABLE discord_channels;

        ALTER TABLE conversation_threads_contract RENAME TO conversation_threads;
        ALTER TABLE messages_contract RENAME TO messages;
        ALTER TABLE channel_state_contract RENAME TO channel_state;

        CREATE INDEX idx_threads_scope
            ON conversation_threads(scope_type, scope_key);
        CREATE INDEX idx_messages_thread_time
            ON messages(thread_id, created_at DESC);
        CREATE INDEX idx_messages_intent
            ON messages(intent_id, created_at DESC);
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


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
        assert state["last_summary"] == "War recap covering rankings and player highlights."

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


def test_message_paths_work_after_dead_schema_contract():
    conn = _memory_conn()
    try:
        _contract_dead_conversation_schema(conn)

        message_id = db.save_message(
            "channel:ch-contract",
            "assistant",
            "A compact response after the schema contract.",
            channel_id="ch-contract",
            channel_name="ask-elixir",
            channel_kind="text",
            workflow="interactive",
            conn=conn,
        )
        update_message_summary(message_id, "Contracted schema response.", conn=conn)

        thread = conn.execute(
            "SELECT * FROM conversation_threads WHERE scope_key = 'ch-contract'"
        ).fetchone()
        assert set(thread.keys()) == {"thread_id", "scope_type", "scope_key"}
        assert (
            conn.execute(
                "SELECT channel_id FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()[0]
            == "ch-contract"
        )
        assert db.get_channel_state("ch-contract", conn=conn) == {
            "channel_id": "ch-contract",
            "last_summary": "Contracted schema response.",
        }
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
