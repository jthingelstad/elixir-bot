"""Silence detector: quiet is an alarm (confidence hardening, 2026-07-05)."""
import db
from runtime import health


def _clear(conn):
    conn.execute("DELETE FROM communication_intents")
    conn.execute("DELETE FROM leader_action_recommendations")
    conn.execute("DELETE FROM recognition_ledger")
    conn.commit()


def _fulfilled_intent(conn, *, hours_ago: int):
    """Seed one fulfilled intent (+ its ledger claim for the FK) N hours old."""
    conn.execute(
        "INSERT INTO recognition_ledger (recognition_key, stream, event_refs_json, "
        "score, claimed_at) VALUES ('k','x','{}',0,strftime('%Y-%m-%dT%H:%M:%SZ','now'))")
    conn.execute(
        "INSERT INTO communication_intents (recognition_key, intent_type, lane, scope, "
        "payload_json, status, attempts, created_at, expires_at, fulfilled_at) VALUES "
        "('k','celebrate:x','battle-feed','public','{}','fulfilled',1,"
        "strftime('%Y-%m-%dT%H:%M:%SZ','now',?),"
        "strftime('%Y-%m-%dT%H:%M:%SZ','now','+6 hours'),"
        "strftime('%Y-%m-%dT%H:%M:%SZ','now',?))",
        (f"-{hours_ago + 1} hours", f"-{hours_ago} hours"))
    conn.commit()


def _proposed_card(conn, *, hours_ago: int, copy_message_id=None):
    conn.execute(
        "INSERT INTO leader_action_recommendations (action_key, action_type, objective, "
        "status, prompt_text, rationale, proposed_at, created_at, updated_at, is_test, "
        "copy_message_id) VALUES ('a1','promotion_recommendation','x','proposed','p','r',"
        "strftime('%Y-%m-%dT%H:%M:%SZ','now',?),strftime('%Y-%m-%dT%H:%M:%SZ','now',?),"
        "strftime('%Y-%m-%dT%H:%M:%SZ','now'),0,?)",
        (f"-{hours_ago} hours", f"-{hours_ago} hours", copy_message_id))
    conn.commit()


def test_no_silence_when_recent_output():
    conn = db.get_connection()
    _clear(conn)
    _fulfilled_intent(conn, hours_ago=1)
    assert health.check_output_silence(conn) == []


def test_flags_total_silence():
    conn = db.get_connection()
    _clear(conn)
    _fulfilled_intent(conn, hours_ago=20)
    assert any("no Discord output" in p for p in health.check_output_silence(conn))


def test_flags_leader_action_recommended_but_never_posted():
    """The exact can_post_leader_action signature."""
    conn = db.get_connection()
    _clear(conn)
    _fulfilled_intent(conn, hours_ago=1)  # recent output isolates signal (b)
    _proposed_card(conn, hours_ago=3)
    assert any("never posted" in p for p in health.check_output_silence(conn))


def test_posted_leader_action_does_not_flag():
    conn = db.get_connection()
    _clear(conn)
    _fulfilled_intent(conn, hours_ago=1)
    _proposed_card(conn, hours_ago=3, copy_message_id="123456")
    assert not any("never posted" in p for p in health.check_output_silence(conn))
