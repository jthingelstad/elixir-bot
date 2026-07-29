"""H1/H2 regression: @managed_connection owns the commit.

A decorated writer must commit ONLY when it opened the connection itself. When
handed a borrowed connection (the engine tick's conn), it must NOT commit — a
mid-step commit would prematurely persist that step's partial work and defeat
the tick's per-step rollback guard.
"""

from __future__ import annotations

import db
from storage.leader_actions import create_leader_action_recommendation


def _seed_player(conn):
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#X','Xavier','x','x')"
    )
    conn.commit()


def test_conn_none_call_persists():
    """With conn=None the decorator owns + commits, so a fresh connection sees it."""
    setup = db.get_connection()
    try:
        _seed_player(setup)
    finally:
        setup.close()

    created = create_leader_action_recommendation(
        action_type="promotion_recommendation",
        objective="Promote X",
        rationale="strong",
        target_player_tag="#X",
        source_signal_key="test:persist",
        source_signal_type="manual",
    )
    aid = created["action_id"]

    check = db.get_connection()
    try:
        row = check.execute(
            "SELECT action_id FROM leader_action_recommendations WHERE action_id=?",
            (aid,),
        ).fetchone()
        assert row is not None  # committed by the decorator
    finally:
        check.close()


def test_memory_store_conn_none_persists():
    """memory_store.create_memory with conn=None commits via its decorator."""
    from memory_store import create_memory, get_memory, get_memory_connection

    created = create_memory(
        body="Leader noted strong attendance.",
        source_type="leader_note",
        is_inference=False,
        confidence=1.0,
        created_by="leader:jamie",
    )
    check = get_memory_connection()
    try:
        assert get_memory(created["memory_id"], conn=check) is not None
    finally:
        check.close()


def test_memory_store_borrowed_conn_not_committed():
    """create_memory on a borrowed conn must not commit — rollback discards it."""
    from memory_store import create_memory, get_memory_connection

    conn = get_memory_connection()
    try:
        created = create_memory(
            body="ephemeral note",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader:jamie",
            conn=conn,
        )
        mid = created["memory_id"]
        assert (
            conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (mid,)).fetchone() is not None
        )
        conn.rollback()
        assert conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (mid,)).fetchone() is None
    finally:
        conn.close()


def test_borrowed_conn_is_not_committed_by_writer():
    """With a borrowed conn, the writer must NOT commit — rolling back the
    borrowed conn discards the write (proving no premature mid-step commit)."""
    conn = db.get_connection()
    try:
        _seed_player(conn)
        created = create_leader_action_recommendation(
            action_type="promotion_recommendation",
            objective="Promote X",
            rationale="strong",
            target_player_tag="#X",
            source_signal_key="test:borrowed",
            source_signal_type="manual",
            conn=conn,
        )
        aid = created["action_id"]
        # Visible on the same uncommitted transaction...
        assert (
            conn.execute(
                "SELECT 1 FROM leader_action_recommendations WHERE action_id=?", (aid,)
            ).fetchone()
            is not None
        )
        # ...but the writer did NOT commit, so a rollback discards it.
        conn.rollback()
        assert (
            conn.execute(
                "SELECT 1 FROM leader_action_recommendations WHERE action_id=?", (aid,)
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_action_writer_has_no_decision_case_schema_dependency(engine_conn):
    """The action writer remains independent of the retired case schema."""
    assert (
        engine_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='idx_leader_actions_case'"
        ).fetchone()
        is None
    )
    assert "case_id" not in {
        row[1] for row in engine_conn.execute("PRAGMA table_info(leader_action_recommendations)")
    }

    created = create_leader_action_recommendation(
        action_type="promotion_recommendation",
        objective="Promote X",
        rationale="strong",
        target_player_tag="#X",
        source_signal_key="test:no-case-schema",
        source_signal_type="manual",
        conn=engine_conn,
    )

    assert created["action_type"] == "promotion_recommendation"
    assert "case_id" not in created
