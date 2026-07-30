"""The loop that asks "did the thing the leader said they did actually happen?"

The Clash Royale API is read-only, so Elixir cannot take an action itself. It
recommends, a human acts in the game, and the human marks the card. Outcome
evaluation is the ONLY mechanism that closes that loop — it diffs the baseline
captured at proposal against live state some hours after the decision.

That loop had never run. `refresh_due_leader_action_outcomes` filtered for
"still pending" in Python, AFTER `ORDER BY decided_at ASC LIMIT 20` had already
chosen the rows. The twenty oldest decided cards are precisely the ones long
since evaluated, so every daily run fetched the same twenty, skipped all
twenty, and returned an empty list. Live numbers when this was found: 132
decided cards, 105 still `pending_evaluation`, zero pending among the oldest
twenty.

The bug has no symptom. The job logs success, the scheduler reports a clean
run, and the queue it is supposed to drain grows forever behind it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
from storage.leader_actions import OUTCOME_EVALUATION_GRACE_HOURS


@pytest.fixture
def outcome_db(tmp_path, monkeypatch):
    path = str(tmp_path / "outcomes.db")
    original = db.get_connection
    monkeypatch.setattr(db, "get_connection", lambda *a, **k: original(path))
    conn = original(path)
    try:
        yield conn
    finally:
        conn.close()


def _stamp(hours_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _decided_card(conn, *, key: str, decided_hours_ago: float, pending: bool):
    """A done card carrying either a pending or an already-settled outcome."""
    action = db.create_leader_action_recommendation(
        action_type="kick_recommendation",
        objective=f"objective {key}",
        prompt_text=f"prompt {key}",
        rationale="r",
        target_player_tag="#OUT",
        source_signal_key=key,
        source_signal_type="test",
        action_key=key,
        conn=conn,
    )
    stamp = _stamp(decided_hours_ago)
    outcome = (
        '{"pending_evaluation": true, "decided_at": "%s"}' % stamp
        if pending
        else '{"pending_evaluation": false, "evaluated_at": "%s"}' % stamp
    )
    conn.execute(
        "UPDATE leader_action_recommendations SET status=?, decided_at=?, "
        "decided_by_discord_user_id='1', outcome_json=? WHERE action_id=?",
        (db.ACTION_DONE, stamp, outcome, action["action_id"]),
    )
    conn.commit()
    return action["action_id"]


def _pending_ids(conn) -> set[int]:
    rows = conn.execute(
        "SELECT action_id FROM leader_action_recommendations "
        "WHERE json_extract(outcome_json, '$.pending_evaluation') = 1"
    ).fetchall()
    return {row["action_id"] for row in rows}


def test_a_pending_card_behind_twenty_settled_ones_is_still_reached(outcome_db):
    """The exact production shape: a wall of old settled cards in front of the
    pending ones. With the filter in Python this returned nothing, forever."""
    for index in range(25):
        _decided_card(outcome_db, key=f"settled:{index}", decided_hours_ago=800, pending=False)
    target = _decided_card(outcome_db, key="pending:1", decided_hours_ago=48, pending=True)

    refreshed = db.refresh_due_leader_action_outcomes(limit=20)

    assert [a["action_id"] for a in refreshed] == [target], (
        "the pending card was never reached — the oldest settled cards ate the window"
    )
    assert target not in _pending_ids(outcome_db), "the outcome is still marked pending"


def test_a_card_inside_its_delay_is_left_alone(outcome_db):
    """kick_recommendation waits 24h. A card decided an hour ago is not due."""
    fresh = _decided_card(outcome_db, key="fresh", decided_hours_ago=1, pending=True)

    assert db.refresh_due_leader_action_outcomes(limit=20) == []
    assert fresh in _pending_ids(outcome_db), "a card was evaluated before it was due"


def test_a_long_overdue_card_is_settled_without_inventing_a_measurement(outcome_db):
    """`evaluate_leader_action` diffs the proposal baseline against state read
    NOW. Run 40 days late that comparison is noise, not data. Such a card must
    be closed as not-evaluated rather than handed a fabricated delta — and it
    must stop coming back."""
    stale = _decided_card(
        outcome_db,
        key="stale",
        decided_hours_ago=OUTCOME_EVALUATION_GRACE_HOURS + 500,
        pending=True,
    )

    db.refresh_due_leader_action_outcomes(limit=20)

    row = outcome_db.execute(
        "SELECT outcome_json FROM leader_action_recommendations WHERE action_id=?",
        (stale,),
    ).fetchone()
    assert '"not_evaluated": "window_passed"' in row["outcome_json"]
    assert stale not in _pending_ids(outcome_db), "a settled card stays in the queue forever"

    # And it does not reappear on the next run.
    assert db.refresh_due_leader_action_outcomes(limit=20) == []


def test_the_queue_drains_across_runs(outcome_db):
    """Each run must make progress. The old shape made zero progress no matter
    how many times it ran."""
    for index in range(5):
        _decided_card(outcome_db, key=f"p:{index}", decided_hours_ago=48, pending=True)

    first = db.refresh_due_leader_action_outcomes(limit=2)
    assert len(first) == 2
    assert len(_pending_ids(outcome_db)) == 3

    db.refresh_due_leader_action_outcomes(limit=2)
    db.refresh_due_leader_action_outcomes(limit=2)
    assert _pending_ids(outcome_db) == set(), "the queue never drained"
