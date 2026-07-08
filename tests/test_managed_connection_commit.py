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
    conn.execute("INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
                 "VALUES ('#X','Xavier','x','x')")
    conn.commit()


def test_conn_none_call_persists():
    """With conn=None the decorator owns + commits, so a fresh connection sees it."""
    setup = db.get_connection()
    try:
        _seed_player(setup)
    finally:
        setup.close()

    created = create_leader_action_recommendation(
        action_type="promotion_recommendation", objective="Promote X",
        rationale="strong", target_player_tag="#X",
        source_signal_key="test:persist", source_signal_type="manual",
    )
    aid = created["action_id"]

    check = db.get_connection()
    try:
        row = check.execute(
            "SELECT action_id FROM leader_action_recommendations WHERE action_id=?", (aid,)
        ).fetchone()
        assert row is not None       # committed by the decorator
    finally:
        check.close()


def test_borrowed_conn_is_not_committed_by_writer():
    """With a borrowed conn, the writer must NOT commit — rolling back the
    borrowed conn discards the write (proving no premature mid-step commit)."""
    conn = db.get_connection()
    try:
        _seed_player(conn)
        created = create_leader_action_recommendation(
            action_type="promotion_recommendation", objective="Promote X",
            rationale="strong", target_player_tag="#X",
            source_signal_key="test:borrowed", source_signal_type="manual",
            conn=conn,
        )
        aid = created["action_id"]
        # Visible on the same uncommitted transaction...
        assert conn.execute(
            "SELECT 1 FROM leader_action_recommendations WHERE action_id=?", (aid,)
        ).fetchone() is not None
        # ...but the writer did NOT commit, so a rollback discards it.
        conn.rollback()
        assert conn.execute(
            "SELECT 1 FROM leader_action_recommendations WHERE action_id=?", (aid,)
        ).fetchone() is None
    finally:
        conn.close()
