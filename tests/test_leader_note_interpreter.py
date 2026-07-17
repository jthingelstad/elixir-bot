"""The async leader-note interpreter (v7): a classification is persisted and its
effect applied; a low-confidence behaviour-changing effect is downgraded to a
safe no-op; an LLM failure leaves the note as an uninterpreted annotation.
"""

from __future__ import annotations

import elixir_agent
import runtime.leader_note_interpreter as interp

NOW = "2026-07-01T12:00:00"


def _insert_declined_card(conn, *, key, note, tag="#AAA"):
    conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, proposed_at, "
        " created_at, updated_at, target_player_tag, target_player_name, "
        " decision_note, is_test) "
        "VALUES (?, 'kick_recommendation', 'o', 'rejected', 'p', ?, ?, ?, ?, "
        " 'Idle', ?, 0)",
        (key, NOW, NOW, NOW, tag, note),
    )
    conn.commit()
    return conn.execute(
        "SELECT action_id FROM leader_action_recommendations WHERE action_key = ?",
        (key,),
    ).fetchone()[0]


def _premise_flag(conn, action_id):
    return conn.execute(
        "SELECT premise_rejected FROM leader_action_recommendations "
        "WHERE action_id = ?",
        (action_id,),
    ).fetchone()[0]


def test_interpretation_persists_and_applies(engine_conn, monkeypatch):
    action_id = _insert_declined_card(engine_conn, key="i1", note="give him a week")
    monkeypatch.setattr(
        elixir_agent,
        "interpret_leader_note",
        lambda ctx: {
            "effect": {"kind": "timing_hold", "hold_days": 7, "confidence": 0.9},
            "reading": "hold 1 week",
        },
    )
    updated = interp._interpret_and_persist(action_id)
    assert updated["note_interpret_status"] == "interpreted"
    assert updated["note_interpret"]["kind"] == "timing_hold"
    assert updated["note_interpret"]["applied"] is True
    assert updated["note_interpret"]["reading"] == "hold 1 week"


def test_low_confidence_premise_is_downgraded_to_noop(engine_conn, monkeypatch):
    action_id = _insert_declined_card(engine_conn, key="i2", note="eh, maybe wrong")
    monkeypatch.setattr(
        elixir_agent,
        "interpret_leader_note",
        lambda ctx: {
            "effect": {"kind": "invalidate_premise", "confidence": 0.3},
            "reading": "premise rejected",
        },
    )
    updated = interp._interpret_and_persist(action_id)
    assert updated["note_interpret_status"] == "interpreted"
    assert updated["note_interpret"]["applied"] is False
    # The risky effect was NOT applied — premise stays un-rejected.
    assert not _premise_flag(engine_conn, action_id)


def test_llm_failure_marks_note_uninterpreted(engine_conn, monkeypatch):
    action_id = _insert_declined_card(engine_conn, key="i3", note="something")
    monkeypatch.setattr(
        elixir_agent,
        "interpret_leader_note",
        lambda ctx: {"_error": "boom"},
    )
    updated = interp._interpret_and_persist(action_id)
    assert updated["note_interpret_status"] == "failed"
    assert not _premise_flag(engine_conn, action_id)


def test_undo_reverses_a_persisted_effect(engine_conn, monkeypatch):
    action_id = _insert_declined_card(engine_conn, key="i4", note="give him a month")
    monkeypatch.setattr(
        elixir_agent,
        "interpret_leader_note",
        lambda ctx: {
            "effect": {"kind": "timing_hold", "hold_days": 30, "confidence": 0.9},
            "reading": "hold 1 month",
        },
    )
    interp._interpret_and_persist(action_id)
    assert (
        engine_conn.execute(
            "SELECT expires_at FROM leader_action_recommendations WHERE action_id = ?",
            (action_id,),
        ).fetchone()[0]
        is not None
    )

    undone = interp.undo_note_interpretation(action_id)
    assert undone["note_interpret_status"] == "undone"
    assert (
        engine_conn.execute(
            "SELECT expires_at FROM leader_action_recommendations WHERE action_id = ?",
            (action_id,),
        ).fetchone()[0]
        is None
    )
