"""Re-proposing a card must not leave the leader reading mixed vintages.

`create_leader_action_recommendation` UPSERTs on `action_key`, so an engine
that keeps recommending the same thing refreshes one row rather than stacking
duplicates. 30 of the action_ids in production are burned by exactly this, so
the conflict branch is a live path, not an edge case.

It refreshed `rationale` unconditionally while never touching `prompt_text`.
On an open card that means the "Why" moves and the "Decision" does not — the
leader reads a fresh justification under stale instructions. On a decided card
it rewrote the reasoning behind a decision that had already been made.
"""

from __future__ import annotations

import pytest

import db


@pytest.fixture
def card_db(tmp_path, monkeypatch):
    path = str(tmp_path / "reproposal.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        yield conn
    finally:
        conn.close()


KEY = "engine:kick:#SAME"


def _by_key(conn):
    from storage.leader_actions import get_leader_action_by_key

    return get_leader_action_by_key(KEY, conn=conn)


def _propose(conn, *, prompt: str, rationale: str, copy: str | None = None):
    return db.create_leader_action_recommendation(
        action_type="kick_recommendation",
        objective="Remove an inactive member",
        prompt_text=prompt,
        rationale=rationale,
        target_player_tag="#SAME",
        source_signal_key=KEY,
        source_signal_type="test",
        action_key=KEY,
        copy_original_text=copy,
        conn=conn,
    )


def test_an_open_card_refreshes_decision_and_why_together(card_db):
    _propose(card_db, prompt="Kick #SAME — 8 days idle.", rationale="8 days, full roster.")
    card_db.commit()

    _propose(card_db, prompt="Kick #SAME — 15 days idle.", rationale="15 days, full roster.")
    card_db.commit()

    after = _by_key(card_db)
    assert after["rationale"] == "15 days, full roster."
    assert after["prompt_text"] == "Kick #SAME — 15 days idle.", (
        "the Why refreshed but the Decision did not — the leader reads mixed vintages"
    )


def test_a_decided_card_is_frozen(card_db):
    action = _propose(card_db, prompt="Kick #SAME — 8 days idle.", rationale="8 days.")
    card_db.commit()
    db.decide_leader_action(
        action["action_id"],
        status=db.ACTION_DONE,
        discord_user_id=1,
        emoji="✅",
        conn=card_db,
    )
    card_db.commit()

    _propose(card_db, prompt="Kick #SAME — 30 days idle.", rationale="30 days.")
    card_db.commit()

    after = _by_key(card_db)
    assert after["status"] == db.ACTION_DONE
    assert after["prompt_text"] == "Kick #SAME — 8 days idle."
    assert after["rationale"] == "8 days.", "a re-proposal rewrote the reasoning behind a decision"


def test_a_leader_copy_edit_survives_a_re_proposal(card_db):
    action = _propose(card_db, prompt="p", rationale="r", copy="Generated copy.")
    card_db.commit()
    db.update_leader_action_copy_text(
        action["action_id"], copy_text="Jamie's own wording.", discord_user_id=1, conn=card_db
    )
    card_db.commit()

    _propose(card_db, prompt="p2", rationale="r2", copy="Regenerated copy.")
    card_db.commit()

    after = _by_key(card_db)
    assert after["copy_current_text"] == "Jamie's own wording.", (
        "a regenerated copy overwrote what the leader typed"
    )


def test_a_re_proposal_does_not_wipe_a_suppression_window(card_db):
    """A decline note like "revisit in a month" writes `expires_at`, which the
    re-nomination gate reads. No producer passes expires_at, so the old
    unconditional assignment cleared that hold on the next re-proposal."""
    action = _propose(card_db, prompt="p", rationale="r")
    card_db.commit()
    db.decide_leader_action(
        action["action_id"],
        status=db.ACTION_REJECTED,
        discord_user_id=1,
        emoji="❌",
        decision_note="revisit in a month",
        conn=card_db,
    )
    card_db.commit()
    held_until = _by_key(card_db)["expires_at"]
    assert held_until, "the note did not produce a suppression window"

    _propose(card_db, prompt="p", rationale="r")
    card_db.commit()

    after = _by_key(card_db)
    assert after["expires_at"] == held_until, "the re-nomination hold was silently cleared"
