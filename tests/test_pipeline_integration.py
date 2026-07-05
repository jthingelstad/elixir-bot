"""End-to-end compose → Editor gate → delivery integration — the seam the
replay/sim harness skips (they use render_intent + no gate). This is where
verdict-shape / recompose / lazy-import bugs live. The LLM boundary is stubbed
(the critic + a canned compose_fn), but the REAL gate logic, verdict recording,
delivery status machine, and per-lane send run.
"""
from __future__ import annotations

import json

from engine import delivery, editor

NOW = "2026-07-05T00:00:00Z"


def _verdict(v, critique=""):
    return json.dumps({
        "verdict": v,
        "dimensions": {d: {"ok": v == "pass", "note": ""} for d in editor.DIMENSIONS},
        "critique": critique,
    })


def _stub_critic(monkeypatch, responses):
    seq = iter(responses)
    monkeypatch.setattr(editor, "critic_fn", lambda system, user: next(seq))


def _raise(conn):
    intent_id = delivery.raise_intent(
        conn, None, "celebrate:level_up", "member-highlights", "public",
        {"subject_tag": "#A", "event_type": "level_up", "player_name": "Alice",
         "level": 45, "prev_level": 44},
        NOW,
    )
    conn.commit()
    return intent_id


def _run(conn, compose_fn):
    """Drive delivery.consume with the REAL editor gate wired in."""
    sent = []
    def send_fn(lane, copy, *a):
        sent.append((lane, copy))
        return f"msg-{len(sent)}"
    counters = delivery.consume(conn, send_fn, compose_fn, NOW, editor_gate=editor.gate)
    return counters, sent


def _intent_status(conn, intent_id):
    return conn.execute(
        "SELECT status FROM communication_intents WHERE intent_id=?", (intent_id,)
    ).fetchone()[0]


def _verdicts(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM editor_verdicts ORDER BY verdict_id").fetchall()]


def test_pipeline_pass_delivers_and_records_verdict(engine_conn, monkeypatch):
    iid = _raise(engine_conn)
    _stub_critic(monkeypatch, [_verdict("pass")])
    counters, sent = _run(engine_conn, lambda i: "Alice hit level 45 — nice.")
    assert counters["delivered"] == 1
    assert _intent_status(engine_conn, iid) == "fulfilled"
    assert sent and sent[0][1] == "Alice hit level 45 — nice."
    rows = _verdicts(engine_conn)
    assert [r["verdict"] for r in rows] == ["pass"]


def test_pipeline_revise_then_pass_recomposes_once(engine_conn, monkeypatch):
    iid = _raise(engine_conn)
    _stub_critic(monkeypatch, [_verdict("revise", "too flat"), _verdict("pass")])
    seen = {}

    def compose_fn(intent):
        crit = json.loads(intent["payload_json"]).get("editor_critique")
        if crit:
            seen["revised"] = True
            return "Alice powered up to 45 — climbing fast!"
        return "Alice hit 45."

    counters, sent = _run(engine_conn, compose_fn)
    assert seen.get("revised")  # the recompose-once path ran
    assert counters["delivered"] == 1
    assert _intent_status(engine_conn, iid) == "fulfilled"
    assert sent[0][1] == "Alice powered up to 45 — climbing fast!"  # revised copy sent
    assert [r["verdict"] for r in _verdicts(engine_conn)] == ["revise"]


def test_pipeline_critic_raises_fails_open_sends_original(engine_conn, monkeypatch):
    iid = _raise(engine_conn)

    def boom(system, user):
        raise RuntimeError("critic API down")
    monkeypatch.setattr(editor, "critic_fn", boom)

    counters, sent = _run(engine_conn, lambda i: "Alice hit level 45.")
    # fail-open: the original copy still delivers
    assert counters["delivered"] == 1
    assert _intent_status(engine_conn, iid) == "fulfilled"
    assert sent[0][1] == "Alice hit level 45."
    # an 'error' verdict is recorded (visible), not swallowed
    assert [r["verdict"] for r in _verdicts(engine_conn)] == ["error"]


def test_pipeline_meta_copy_uses_deterministic_fallback(engine_conn, monkeypatch):
    """Meta/blank compose output bypasses the gate and renders deterministically
    (the §7 guard) — the gate must NEVER judge fallback copy."""
    iid = _raise(engine_conn)
    _stub_critic(monkeypatch, [_verdict("pass")])  # should not be consulted
    counters, sent = _run(engine_conn, lambda i: "I'm unable to process this signal data.")
    assert counters["delivered"] == 1
    assert _intent_status(engine_conn, iid) == "fulfilled"
    assert "unable to" not in sent[0][1].lower()  # fell back to grounded render
    assert _verdicts(engine_conn) == []  # gate skipped for fallback copy
