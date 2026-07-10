"""Dormant engine delivery path (kept for ELIXIR_AWARENESS_LIVE=0 rollback):
compose → delivery.consume → per-lane send, with NO editor gate (the gate was
retired 2026-07-10 when the brain became sole poster). Covers the basic deliver
+ status machine and the §7 meta-marker deterministic-fallback guard.
"""
from __future__ import annotations

from engine import delivery

NOW = "2026-07-05T00:00:00Z"


def _raise(conn):
    intent_id = delivery.raise_intent(
        conn, None, "celebrate:collection_level_milestone", "member-highlights", "public",
        {"subject_tag": "#A", "event_type": "collection_level_milestone",
         "player_name": "Alice", "milestone": 1700, "collection_level": 1712},
        NOW,
    )
    conn.commit()
    return intent_id


def _run(conn, compose_fn):
    """Drive delivery.consume with NO gate (the live/rollback path)."""
    sent = []

    def send_fn(lane, copy, *a):
        sent.append((lane, copy))
        return f"msg-{len(sent)}"

    counters = delivery.consume(conn, send_fn, compose_fn, NOW, editor_gate=None)
    return counters, sent


def _intent_status(conn, intent_id):
    return conn.execute(
        "SELECT status FROM communication_intents WHERE intent_id=?", (intent_id,)
    ).fetchone()[0]


def test_pipeline_delivers_composed_copy(engine_conn):
    iid = _raise(engine_conn)
    counters, sent = _run(engine_conn, lambda i: "Alice hit level 45 — nice.")
    assert counters["delivered"] == 1
    assert _intent_status(engine_conn, iid) == "fulfilled"
    assert sent and sent[0][1] == "Alice hit level 45 — nice."


def test_pipeline_meta_copy_uses_deterministic_fallback(engine_conn):
    """Meta/blank compose output (§7 guard) renders deterministically instead of
    sending the model's meta-commentary as copy."""
    iid = _raise(engine_conn)
    counters, sent = _run(engine_conn, lambda i: "I'm unable to process this signal data.")
    assert counters["delivered"] == 1
    assert _intent_status(engine_conn, iid) == "fulfilled"
    assert "unable to" not in sent[0][1].lower()  # fell back to a grounded render
