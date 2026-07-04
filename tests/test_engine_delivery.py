"""Delivery: at-least-once semantics (runtime.md §5)."""
from __future__ import annotations

from engine import delivery

NOW = "2026-07-01T12:00:00Z"
LATER_7H = "2026-07-01T19:30:00Z"


def _raise(conn, n=1, now=NOW):
    ids = []
    for i in range(n):
        ids.append(delivery.raise_intent(
            conn, None, "celebrate", "member-highlights", "public",
            {"i": i, "subject_tag": "#A"}, now))
    conn.commit()
    return ids


def _statuses(conn):
    return [r["status"] for r in conn.execute(
        "SELECT status FROM communication_intents ORDER BY intent_id")]


def test_fulfil_only_after_confirmed_send(engine_conn):
    _raise(engine_conn)

    def send_fail(lane, copy):
        raise RuntimeError("discord down")

    delivery.consume(engine_conn, send_fail, lambda i: "copy", NOW)
    assert _statuses(engine_conn) == ["failed"]
    row = engine_conn.execute(
        "SELECT attempts, last_error, fulfilled_at FROM communication_intents"
    ).fetchone()
    assert row["attempts"] == 1 and row["fulfilled_at"] is None
    assert "discord down" in (row["last_error"] or "")


def test_send_failure_stops_batch_and_retries_oldest_first(engine_conn):
    _raise(engine_conn, n=3)
    sent = []

    def send_first_fails(lane, copy):
        if not sent:
            sent.append("fail")
            raise RuntimeError("boom")
        sent.append(copy)
        return f"msg-{len(sent)}"

    delivery.consume(engine_conn, send_first_fails, lambda i: "copy", NOW)
    # first failed, batch stopped — the other two remain pending
    assert _statuses(engine_conn) == ["failed", "pending", "pending"]
    # next consume retries oldest (the failed one) first, then the rest
    delivery.consume(engine_conn, send_first_fails, lambda i: "copy", NOW)
    assert _statuses(engine_conn) == ["fulfilled"] * 3


def test_six_hour_expiry(engine_conn):
    _raise(engine_conn, now=NOW)
    counters = delivery.consume(engine_conn, lambda lane, c: "m1",
                                lambda i: "copy", LATER_7H)
    assert _statuses(engine_conn) == ["expired"]
    assert counters["expired"] == 1


def test_delivered_message_id_recorded(engine_conn):
    _raise(engine_conn)
    delivery.consume(engine_conn, lambda lane, c: "msg-123", lambda i: "copy", NOW)
    row = engine_conn.execute(
        "SELECT status, discord_message_id FROM communication_intents").fetchone()
    assert row["status"] == "fulfilled" and row["discord_message_id"] == "msg-123"


def test_meta_marker_copy_falls_back_to_render(engine_conn):
    from engine.recognition.compose import looks_like_meta
    assert looks_like_meta("As an AI, I'm skipping post today")
    assert not looks_like_meta("Alice just hit Arena 16 — huge!")
    _raise(engine_conn)
    sent = []

    def send(lane, copy):
        sent.append(copy)
        return "m1"

    # compose returns meta-text → delivery must fall back, never post the meta
    delivery.consume(engine_conn, send, lambda i: "skipping post — data inconsistent", NOW)
    assert sent, "fallback copy should still post"
    assert not looks_like_meta(sent[0])
