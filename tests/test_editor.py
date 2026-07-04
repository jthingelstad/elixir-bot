"""The Editor (editor.md): verdict-flow goldens with a stubbed critic, the
EddiePlayz grounding regression, rubric retrieval, feeders, and the strict-
JSON verdict contract's tolerant parser. The critic itself is an LLM call —
always stubbed here (conftest blocks real API calls anyway)."""

from __future__ import annotations

import json

import pytest

from engine import delivery, editor
from engine.recognition import compose as engine_compose

NOW = "2026-07-04T18:00:00Z"


def _verdict(verdict, critique="", dims=None):
    return json.dumps({
        "verdict": verdict,
        "dimensions": dims or {d: {"ok": verdict == "pass", "note": ""}
                               for d in editor.DIMENSIONS},
        "critique": critique,
    })


def _stub_critic(monkeypatch, responses):
    """critic_fn stub yielding canned responses in order; records prompts."""
    calls = []
    seq = iter(responses)

    def fake(system, user):
        calls.append({"system": system, "user": user})
        return next(seq)

    monkeypatch.setattr(editor, "critic_fn", fake)
    return calls


def _raise_welcome(conn, payload=None):
    intent_id = delivery.raise_intent(
        conn, None, "clan:member_joined", "clan-events", "public",
        payload or {"subject_tag": "#V8V0GLR9J", "event_type": "member_joined",
                    "name": "EddiePlayz", "player_name": "EddiePlayz",
                    "trophies": 10396, "role": "member"},
        NOW,
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM communication_intents WHERE intent_id = ?", (intent_id,)
    ).fetchone()


def _verdict_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM editor_verdicts ORDER BY verdict_id").fetchall()]


# ------------------------------------------------------- verdict contract

def test_parse_verdict_clean_json():
    v = editor.parse_verdict(_verdict("pass"))
    assert v["verdict"] == "pass" and v["dimensions"]


def test_parse_verdict_tolerates_fences_and_prose():
    fenced = f"```json\n{_verdict('revise', 'fix it')}\n```"
    assert editor.parse_verdict(fenced)["verdict"] == "revise"
    wrapped = f"Here is my judgment: {_verdict('fallback')} — done."
    assert editor.parse_verdict(wrapped)["verdict"] == "fallback"


@pytest.mark.parametrize("bad", [
    "", "not json at all", "{broken json", '{"verdict": "maybe"}',
    '{"critique": "no verdict field"}',
])
def test_parse_verdict_garbage_maps_to_error(bad):
    assert editor.parse_verdict(bad)["verdict"] == "error"


def test_parse_verdict_error_never_leaks_pass():
    # 'error' is the wrapper's word, never the critic's — a critic emitting it
    # literally is malformed output, not a sanctioned verdict.
    assert editor.parse_verdict('{"verdict": "error"}')["verdict"] == "error"


# ------------------------------------------------------- gate flow goldens

def test_gate_pass_sends_original(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [_verdict("pass")])
    out = editor.gate(engine_conn, intent, "Welcome, EddiePlayz — 10,396 trophies!",
                      lambda i: pytest.fail("no recompose on pass"), NOW)
    assert out == "Welcome, EddiePlayz — 10,396 trophies!"
    rows = _verdict_rows(engine_conn)
    assert [r["verdict"] for r in rows] == ["pass"]
    assert rows[0]["original_copy"] == rows[0]["final_copy"] == out


def test_gate_revise_then_pass(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [
        _verdict("revise", "drop the invented card-collection claim"),
        _verdict("pass"),
    ])
    seen = {}

    def recompose(revised_intent):
        payload = json.loads(revised_intent["payload_json"])
        seen["critique"] = payload.get("editor_critique")
        return "Welcome, EddiePlayz — over 10k trophies. Glad to have you."

    out = editor.gate(engine_conn, intent, "Welcome — a deep card collection!",
                      recompose, NOW)
    assert out.startswith("Welcome, EddiePlayz — over 10k")
    assert "card-collection" in seen["critique"]
    rows = _verdict_rows(engine_conn)
    assert [r["verdict"] for r in rows] == ["revise"]
    assert rows[0]["original_copy"] == "Welcome — a deep card collection!"
    assert rows[0]["final_copy"] == out
    trace = json.loads(rows[0]["dimensions_json"])
    assert trace["round1"]["verdict"] == "revise"
    assert trace["round2"]["verdict"] == "pass"


def test_gate_revise_twice_falls_back_to_render(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [
        _verdict("revise", "still template-grade"),
        _verdict("revise", "STILL template-grade"),
    ])
    out = editor.gate(engine_conn, intent, "Welcome!", lambda i: "Welcome!!", NOW)
    assert out == engine_compose.render_intent(intent)  # deterministic fallback
    assert [r["verdict"] for r in _verdict_rows(engine_conn)] == ["fallback"]


def test_gate_fallback_verdict_uses_render(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [_verdict("fallback", "facts too thin")])
    out = editor.gate(engine_conn, intent, "some copy",
                      lambda i: pytest.fail("no recompose on fallback"), NOW)
    assert out == engine_compose.render_intent(intent)
    assert [r["verdict"] for r in _verdict_rows(engine_conn)] == ["fallback"]


def test_gate_critic_error_fails_open(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, ["complete garbage, not json"])
    out = editor.gate(engine_conn, intent, "the original copy", None, NOW)
    assert out == "the original copy"                       # fail-open
    assert [r["verdict"] for r in _verdict_rows(engine_conn)] == ["error"]


def test_gate_critic_raising_fails_open(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)

    def boom(system, user):
        raise RuntimeError("api down")

    monkeypatch.setattr(editor, "critic_fn", boom)
    out = editor.gate(engine_conn, intent, "the original copy", None, NOW)
    assert out == "the original copy"
    assert [r["verdict"] for r in _verdict_rows(engine_conn)] == ["error"]


def test_gate_meta_marker_revision_falls_back(engine_conn, monkeypatch):
    # A revision that comes back as meta-speak must not reach Discord.
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [_verdict("revise", "be concrete")])
    out = editor.gate(engine_conn, intent, "Welcome!",
                      lambda i: "unable to compose this post", NOW)
    assert out == engine_compose.render_intent(intent)
    assert [r["verdict"] for r in _verdict_rows(engine_conn)] == ["fallback"]


def test_gate_disabled_via_env(engine_conn, monkeypatch):
    monkeypatch.setenv("ELIXIR_EDITOR_ENABLED", "0")
    intent = _raise_welcome(engine_conn)
    monkeypatch.setattr(editor, "critic_fn",
                        lambda s, u: pytest.fail("critic must not run when disabled"))
    assert editor.gate(engine_conn, intent, "copy", None, NOW) == "copy"


# --------------------------------------------- EddiePlayz grounding golden

def test_eddieplayz_grounding_regression(engine_conn, monkeypatch):
    """The founding failure (editor.md): facts WITHOUT any card-collection
    detail + copy claiming one → the gate must revise it away. Uses a
    rule-based grounding critic to prove the plumbing feeds the critic both
    the facts JSON and the copy."""
    intent = _raise_welcome(engine_conn)  # facts: trophies 10396, no cards

    def grounding_critic(system, user):
        facts_part, _, copy_part = user.partition("THE COPY TO JUDGE:")
        if "card collection" in copy_part and "collection" not in facts_part:
            return _verdict("revise", "'deep card collection' is not in the facts")
        return _verdict("pass")

    monkeypatch.setattr(editor, "critic_fn", grounding_critic)
    bad = ("Welcome to POAP KINGS, EddiePlayz. Over 10k trophies and a deep "
           "card collection — real strength.")
    good = "Welcome to POAP KINGS, EddiePlayz. Over 10k trophies — real strength."
    out = editor.gate(engine_conn, intent, bad, lambda i: good, NOW)
    assert out == good
    rows = _verdict_rows(engine_conn)
    assert rows[0]["verdict"] == "revise" and rows[0]["original_copy"] == bad


# ----------------------------------------------------------------- rubric

def test_seed_and_rubric_retrieval(engine_conn):
    from scripts.seed_editorial_rubric import seed

    counters = seed(engine_conn)
    assert counters["added"] == 6
    assert seed(engine_conn) == {"added": 0, "skipped": 6}  # idempotent

    rubric = editor.build_rubric_context(engine_conn, "member_joined", "clan-events")
    assert "ANTI-PATTERN" in rubric and "EXEMPLAR" in rubric
    assert "sikander" in rubric or "template" in rubric

    import memory_store
    rows = memory_store.select_memories(
        tags=["editorial"], viewer_scope="leadership", limit=20, conn=engine_conn)
    assert len(rows) == 6
    assert all("editorial" in (r.get("tags") or []) for r in rows)
    # tag filter is conjunctive
    antis = memory_store.select_memories(
        tags=["editorial", "anti-pattern"], viewer_scope="leadership",
        limit=20, conn=engine_conn)
    assert 0 < len(antis) < 6
    assert all("anti-pattern" in r["tags"] for r in antis)


def test_recent_copies_come_from_verdict_trace(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [_verdict("pass")])
    editor.gate(engine_conn, intent, "A fine grounded post.", None, NOW)
    assert editor.recent_copies(engine_conn) == ["A fine grounded post."]


# ---------------------------------------------------------------- feeders

def test_sweep_prompt_feedback(engine_conn):
    engine_conn.execute(
        """INSERT INTO prompt_feedback
               (assistant_discord_message_id, workflow, channel_name,
                discord_user_id, feedback_value, response_preview,
                recorded_at, updated_at)
           VALUES ('m1', 'channel_update', 'player-highlights', 'u1', 'down',
                   'momentum is real', ?, ?),
                  ('m2', 'channel_update', 'clan-events', 'u1', 'up',
                   'Welcome Andy — 620 trophies in two days.', ?, ?)""",
        (NOW, NOW, NOW, NOW),
    )
    engine_conn.commit()
    counters = editor.sweep_prompt_feedback(engine_conn, NOW)
    assert counters == {"anti_pattern": 1, "exemplar": 1, "skipped": 0}
    # dedupe on re-run
    assert editor.sweep_prompt_feedback(engine_conn, NOW)["skipped"] == 2
    tags = {r["tag"] for r in engine_conn.execute(
        "SELECT tag FROM memory_tags").fetchall()}
    assert {"editorial", "anti-pattern", "exemplar", "candidate"} <= tags


def test_record_deleted_post_links_intent(engine_conn):
    intent = _raise_welcome(engine_conn)
    engine_conn.execute(
        "UPDATE communication_intents SET status='fulfilled', discord_message_id='99887' "
        "WHERE intent_id = ?", (intent["intent_id"],))
    engine_conn.commit()
    mid = editor.record_deleted_post(engine_conn, "99887", "clan-events",
                                     "Welcome — a deep card collection!")
    assert mid is not None
    row = engine_conn.execute(
        "SELECT body FROM memories WHERE memory_id = ?", (mid,)).fetchone()
    assert "deleted this Elixir post" in row["body"]
    assert "member_joined" in row["body"]        # intent facts attached
    assert editor.record_deleted_post(engine_conn, "99887", "clan-events", "x") is None


def test_record_copy_edit_pair(engine_conn):
    mid = editor.record_copy_edit_pair(
        engine_conn, 42, "Robotic kick notice.", "Warm, specific kick notice.")
    assert mid is not None
    body = engine_conn.execute(
        "SELECT body FROM memories WHERE memory_id = ?", (mid,)).fetchone()["body"]
    assert "BEFORE" in body and "AFTER" in body
    assert editor.record_copy_edit_pair(engine_conn, 42, "a", "b") is None  # dedup
    assert editor.record_copy_edit_pair(engine_conn, 43, "same", "same") is None


# ------------------------------------------------------ delivery seam

def test_delivery_invokes_gate_only_for_composed_copy(engine_conn, monkeypatch):
    delivery.raise_intent(engine_conn, None, "clan:member_joined", "clan-events",
                          "public", {"event_type": "member_joined", "name": "A"}, NOW)
    delivery.raise_intent(engine_conn, None, "clan:member_joined", "clan-events",
                          "public", {"event_type": "member_joined", "name": "B"}, NOW)
    engine_conn.commit()
    gated, sent = [], []

    def compose_fn(intent):
        payload = json.loads(intent["payload_json"])
        return "composed copy" if payload.get("name") == "A" else None

    def editor_gate(conn, intent, copy, cfn, now):
        gated.append(copy)
        return copy.upper()

    delivery.consume(engine_conn, lambda lane, c: sent.append(c) or "m1",
                     compose_fn, NOW, editor_gate=editor_gate)
    assert gated == ["composed copy"]            # fallback copy never judged
    assert sent[0] == "COMPOSED COPY"
    assert sent[1].startswith("👋 Welcome")      # render_intent, ungated


def test_delivery_gate_raise_is_contained(engine_conn, monkeypatch):
    delivery.raise_intent(engine_conn, None, "clan:member_joined", "clan-events",
                          "public", {"event_type": "member_joined", "name": "A"}, NOW)
    engine_conn.commit()
    sent = []

    def bad_gate(conn, intent, copy, cfn, now):
        raise RuntimeError("gate bug")

    delivery.consume(engine_conn, lambda lane, c: sent.append(c) or "m1",
                     lambda i: "original", NOW, editor_gate=bad_gate)
    assert sent == ["original"]                  # belt-level fail-open


# ------------------------------------------------------- revise plumbing

def test_intent_context_surfaces_critique_as_instruction(engine_conn):
    intent = _raise_welcome(engine_conn)
    revised = editor.attach_critique(intent, "drop the invented claim")
    ctx = engine_compose.intent_context(engine_conn, revised)
    assert "drop the invented claim" in ctx
    assert "internal editor" in ctx
    # never as a fact the copy could quote
    facts_json = ctx.split("```json", 1)[1]
    assert "editor_critique" not in facts_json


# ------------------------------------------------------- weekly review

def test_compose_weekly_review(engine_conn, monkeypatch):
    intent = _raise_welcome(engine_conn)
    _stub_critic(monkeypatch, [_verdict("pass")])
    editor.gate(engine_conn, intent, "A grounded welcome post.", None, NOW)
    engine_conn.execute(
        "UPDATE communication_intents SET status='fulfilled', fulfilled_at=? "
        "WHERE intent_id = ?", (NOW, intent["intent_id"]))
    engine_conn.commit()

    review_json = json.dumps({
        "assessment": "Solid week; grounding held.",
        "drift_line": "Output stayed close to the exemplar class.",
        "proposed_rubric_entries": [
            {"kind": "exemplar", "title": "Grounded welcome",
             "body": "A grounded welcome post. — every claim traced."},
        ],
    })
    monkeypatch.setattr(editor, "critic_fn", lambda s, u: review_json)
    result = editor.compose_weekly_review(engine_conn, NOW)
    assert "drift" in result["report"].lower()
    assert result["verdict_counts"] == {"pass": 1}
    assert result["proposals"] == ["[exemplar] Grounded welcome"]
    # the proposal landed at confidence 0.6 with the proposed tag
    row = engine_conn.execute(
        """SELECT m.confidence FROM memories m
           JOIN memory_tags t ON t.memory_id = m.memory_id AND t.tag = 'proposed'
        """).fetchone()
    assert row["confidence"] == pytest.approx(0.6)
    # synthesis memory written
    assert engine_conn.execute(
        "SELECT COUNT(*) FROM memories WHERE kind = 'synthesis'").fetchone()[0] == 1
