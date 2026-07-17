"""End-to-end coverage for the leader-note effect appliers (v7): a timing hold
writes/reverts ``expires_at``, and a persist-context effect raises/retires a
durable member shield the kick/role gates honour.
"""

from __future__ import annotations

from engine import leader_note_effects as fx
from engine.management import _has_member_shield

NOW = "2026-07-01T12:00:00"


def _insert_card(conn, *, key, tag="#AAA", name="Idle"):
    conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, proposed_at, "
        " created_at, updated_at, target_player_tag, target_player_name, is_test) "
        "VALUES (?, 'kick_recommendation', 'o', 'rejected', 'p', ?, ?, ?, ?, ?, 0)",
        (key, NOW, NOW, NOW, tag, name),
    )
    conn.commit()
    return conn.execute(
        "SELECT action_id FROM leader_action_recommendations WHERE action_key = ?",
        (key,),
    ).fetchone()[0]


def _expires_at(conn, action_id):
    return conn.execute(
        "SELECT expires_at FROM leader_action_recommendations WHERE action_id = ?",
        (action_id,),
    ).fetchone()[0]


def test_timing_hold_sets_and_reverts_expiry(engine_conn):
    action_id = _insert_card(engine_conn, key="hold1")
    assert _expires_at(engine_conn, action_id) is None

    result = fx.apply_leader_note_effect(action_id, {"kind": "timing_hold", "hold_days": 30})
    assert result["applied"] and result["kind"] == "timing_hold"
    assert _expires_at(engine_conn, action_id) is not None
    assert result["prior"] == {"expires_at": None}

    fx.revert_leader_note_effect(action_id, result)
    assert _expires_at(engine_conn, action_id) is None


def test_persist_context_shields_member_and_reverts(engine_conn):
    action_id = _insert_card(engine_conn, key="ctx1", tag="#BBB", name="AltGuy")
    assert _has_member_shield("#BBB") is False

    result = fx.apply_leader_note_effect(
        action_id,
        {
            "kind": "persist_context",
            "context_kind": "alt",
            "context_fact": "second account of a founder",
            "confidence": 0.9,
        },
    )
    assert result["applied"] and result["context_kind"] == "alt"
    assert _has_member_shield("#BBB") is True

    fx.revert_leader_note_effect(action_id, result)
    assert _has_member_shield("#BBB") is False


def test_low_value_effect_is_a_safe_noop(engine_conn):
    action_id = _insert_card(engine_conn, key="none1")
    result = fx.apply_leader_note_effect(action_id, {"kind": "none"})
    assert result["applied"] is False
    assert _expires_at(engine_conn, action_id) is None
