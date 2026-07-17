"""Delivery: at-least-once semantics (runtime.md §5)."""

from __future__ import annotations

from engine import delivery

NOW = "2026-07-01T12:00:00Z"
LATER_7H = "2026-07-01T19:30:00Z"


def _raise(conn, n=1, now=NOW):
    ids = []
    for i in range(n):
        ids.append(
            delivery.raise_intent(
                conn,
                None,
                "celebrate",
                "member-highlights",
                "public",
                {"i": i, "subject_tag": "#A"},
                now,
            )
        )
    conn.commit()
    return ids


def _statuses(conn):
    return [
        r["status"]
        for r in conn.execute("SELECT status FROM communication_intents ORDER BY intent_id")
    ]


def test_fulfil_only_after_confirmed_send(legacy_engine_conn):
    _raise(legacy_engine_conn)

    def send_fail(lane, copy):
        raise RuntimeError("discord down")

    delivery.consume(legacy_engine_conn, send_fail, lambda i: "copy", NOW)
    assert _statuses(legacy_engine_conn) == ["failed"]
    row = legacy_engine_conn.execute(
        "SELECT attempts, last_error, fulfilled_at FROM communication_intents"
    ).fetchone()
    assert row["attempts"] == 1 and row["fulfilled_at"] is None
    assert "discord down" in (row["last_error"] or "")


def test_send_failure_stops_batch_and_retries_oldest_first(legacy_engine_conn):
    _raise(legacy_engine_conn, n=3)
    sent = []

    def send_first_fails(lane, copy):
        if not sent:
            sent.append("fail")
            raise RuntimeError("boom")
        sent.append(copy)
        return f"msg-{len(sent)}"

    delivery.consume(legacy_engine_conn, send_first_fails, lambda i: "copy", NOW)
    # first failed, batch stopped — the other two remain pending
    assert _statuses(legacy_engine_conn) == ["failed", "pending", "pending"]
    # next consume retries oldest (the failed one) first, then the rest
    delivery.consume(legacy_engine_conn, send_first_fails, lambda i: "copy", NOW)
    assert _statuses(legacy_engine_conn) == ["fulfilled"] * 3


def test_six_hour_expiry(legacy_engine_conn):
    _raise(legacy_engine_conn, now=NOW)
    counters = delivery.consume(
        legacy_engine_conn, lambda lane, c: "m1", lambda i: "copy", LATER_7H
    )
    assert _statuses(legacy_engine_conn) == ["expired"]
    assert counters["expired"] == 1


def test_delivered_message_id_recorded(legacy_engine_conn):
    _raise(legacy_engine_conn)
    delivery.consume(legacy_engine_conn, lambda lane, c: "msg-123", lambda i: "copy", NOW)
    row = legacy_engine_conn.execute(
        "SELECT status, discord_message_id FROM communication_intents"
    ).fetchone()
    assert row["status"] == "fulfilled" and row["discord_message_id"] == "msg-123"


def test_meta_marker_copy_falls_back_to_render(legacy_engine_conn):
    from engine.recognition.compose import looks_like_meta

    assert looks_like_meta("As an AI, I'm skipping post today")
    assert not looks_like_meta("Alice just hit Arena 16 — huge!")
    _raise(legacy_engine_conn)
    sent = []

    def send(lane, copy):
        sent.append(copy)
        return "m1"

    # compose returns meta-text → delivery must fall back, never post the meta
    delivery.consume(legacy_engine_conn, send, lambda i: "skipping post — data inconsistent", NOW)
    assert sent, "fallback copy should still post"
    assert not looks_like_meta(sent[0])


def test_lane_failure_does_not_block_other_lanes(legacy_engine_conn):
    # Cold review 2026-07-04 #2: fail-stop is PER LANE.
    delivery.raise_intent(
        legacy_engine_conn,
        None,
        "celebrate",
        "member-highlights",
        "public",
        {"subject_tag": "#A"},
        NOW,
    )
    delivery.raise_intent(
        legacy_engine_conn,
        None,
        "clan",
        "clan-events",
        "public",
        {"subject_tag": "#B"},
        NOW,
    )
    delivery.raise_intent(
        legacy_engine_conn,
        None,
        "celebrate",
        "member-highlights",
        "public",
        {"subject_tag": "#C"},
        NOW,
    )
    legacy_engine_conn.commit()

    def send_highlights_broken(lane, copy):
        if lane == "member-highlights":
            raise RuntimeError("lane down")
        return "msg-ok"

    counters = delivery.consume(legacy_engine_conn, send_highlights_broken, lambda i: "copy", NOW)
    rows = {
        r["lane"]: r["status"]
        for r in legacy_engine_conn.execute(
            "SELECT lane, status FROM communication_intents WHERE intent_id IN (1, 2)"
        )
    }
    # clan-events delivered despite member-highlights failing
    assert rows["clan-events"] == "fulfilled"
    assert rows["member-highlights"] == "failed"
    # the second highlights intent waited (lane fail-stop), not failed
    third = legacy_engine_conn.execute(
        "SELECT status FROM communication_intents WHERE intent_id = 3"
    ).fetchone()
    assert third["status"] == "pending"
    assert counters["delivered"] == 1 and counters["failed"] == 1


def test_terminal_status_commits_immediately(legacy_engine_conn):
    # Cold review 2026-07-04 #1: a fulfilled mark must survive an exception
    # escaping later in the same consume (no rollback-resend of posted msgs).
    _raise(legacy_engine_conn, n=2)
    calls = []

    def send_then_explode(lane, copy):
        calls.append(1)
        if len(calls) == 1:
            return "msg-1"
        raise KeyboardInterrupt("simulated escape mid-consume")

    try:
        delivery.consume(legacy_engine_conn, send_then_explode, lambda i: "copy", NOW)
    except KeyboardInterrupt:
        pass
    legacy_engine_conn.rollback()  # simulate the tick guard skipping its commit
    assert _statuses(legacy_engine_conn)[0] == "fulfilled"  # survived the rollback
