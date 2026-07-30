"""`leader_action_recommendations` timestamps must stay one shape.

The table carries two in production: 122 rows written with a trailing 'Z'
through 2026-07-15, and 75 naive ones from 2026-07-05 onward. That writer is
gone — `_db._utcnow()` is naive and is now the only source — but the mixed
history is still there to be compared against.

It has not bitten yet purely by luck. 'Z' (0x5A) sorts after every digit, so a
`proposed_at >= cutoff` string compare happens to give the right answer while
the same compare with `<` would not. That is the same class of trap as the
CR-compact `battle_time` bound: no exception, no empty result, just a silently
wrong window. These tests pin the writer to one shape and keep the readers
honest about the history.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

import db
from runtime.leader_action_policy import count_open_leader_actions

NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


@pytest.fixture
def stamp_db(tmp_path, monkeypatch):
    path = str(tmp_path / "stamps.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        yield conn
    finally:
        conn.close()


def _card(conn, key: str):
    return db.create_leader_action_recommendation(
        action_type="in_game_relay",
        objective=f"o{key}",
        prompt_text="p",
        rationale="r",
        source_signal_key=key,
        source_signal_type="test",
        action_key=key,
        conn=conn,
    )


def test_a_new_card_is_written_naive_not_z_suffixed(stamp_db):
    action = _card(stamp_db, "fresh")
    stamp_db.commit()
    assert NAIVE.fullmatch(action["proposed_at"]), (
        f"proposed_at is not the canonical naive shape: {action['proposed_at']!r}"
    )


def test_a_decision_is_written_naive_not_z_suffixed(stamp_db):
    action = _card(stamp_db, "decided")
    stamp_db.commit()
    decided = db.decide_leader_action(
        action["action_id"], status=db.ACTION_DONE, discord_user_id=1, emoji="✅", conn=stamp_db
    )
    assert NAIVE.fullmatch(decided["decided_at"]), (
        f"decided_at is not the canonical naive shape: {decided['decided_at']!r}"
    )


def test_the_backlog_count_reads_a_z_suffixed_row_correctly(stamp_db):
    """A legacy 'Z' row inside the window must still count as open.

    This is the assertion that would have caught the format split. The row is
    written the way the pre-2026-07-15 writer wrote it.
    """
    action = _card(stamp_db, "legacy")
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp_db.execute(
        "UPDATE leader_action_recommendations SET proposed_at = ?, source_message_id = '900' "
        "WHERE action_id = ?",
        (recent, action["action_id"]),
    )
    stamp_db.commit()

    assert count_open_leader_actions(conn=stamp_db) == 1, (
        "a 'Z'-suffixed open card was not counted against the backlog cap"
    )


def test_the_backlog_count_excludes_a_z_suffixed_row_outside_the_window(stamp_db):
    """The complement: the same legacy shape, too old, must fall out."""
    action = _card(stamp_db, "legacy-old")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp_db.execute(
        "UPDATE leader_action_recommendations SET proposed_at = ?, source_message_id = '901' "
        "WHERE action_id = ?",
        (old, action["action_id"]),
    )
    stamp_db.commit()

    assert count_open_leader_actions(conn=stamp_db) == 0, (
        "a stale 'Z'-suffixed card still counted, which deadlocks the posting cap"
    )
