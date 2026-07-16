"""The post-compose editorial critic: engine.editor.judge + rubric + ledger.

The critic is fail-open by contract — every error path must resolve to a verdict
that lets the original copy ship, never a raised exception that could block a post.
"""

from __future__ import annotations

from engine import editor


def test_editor_enabled_toggle(monkeypatch):
    monkeypatch.delenv("ELIXIR_EDITOR_GATE", raising=False)
    assert editor.editor_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("ELIXIR_EDITOR_GATE", off)
        assert editor.editor_enabled() is False
    monkeypatch.setenv("ELIXIR_EDITOR_GATE", "1")
    assert editor.editor_enabled() is True


def test_parse_verdict_is_tolerant():
    assert editor._parse_verdict('{"verdict":"pass"}')["verdict"] == "pass"
    assert (
        editor._parse_verdict('```json\n{"verdict":"revise"}\n```')["verdict"]
        == "revise"
    )
    assert (
        editor._parse_verdict('noise {"verdict":"fallback"} tail')["verdict"]
        == "fallback"
    )
    # Off-contract or unparseable resolves to error → caller ships original copy.
    assert editor._parse_verdict("not json at all")["verdict"] == "error"
    assert editor._parse_verdict('{"verdict":"maybe"}')["verdict"] == "error"
    assert editor._parse_verdict("")["verdict"] == "error"
    assert editor._parse_verdict(None)["verdict"] == "error"


def test_judge_fails_open_when_the_model_raises():
    def boom(_user):
        raise RuntimeError("api down")

    result = editor.judge(copy="anything", facts={}, llm_fn=boom)
    assert result["verdict"] == "error"


def test_judge_returns_parsed_verdict_via_seam():
    def seam(_user):
        return '{"verdict":"fallback","critique":"invented number","dimensions":{"grounding":{"ok":false}}}'

    result = editor.judge(
        copy="pax has 9999", facts={"pax": 3000}, lane="elixir", llm_fn=seam
    )
    assert result["verdict"] == "fallback"
    assert result["dimensions"]["grounding"]["ok"] is False


def test_judge_feeds_copy_facts_rubric_and_lane_to_the_model():
    captured = {}

    def seam(user):
        captured["user"] = user
        return '{"verdict":"pass"}'

    editor.judge(
        copy="the composed copy",
        facts={"trophies": 7500},
        recent_copies=["an older post"],
        rubric=[{"kind": "exemplar", "lesson": "ground every number"}],
        lane="announcements",
        llm_fn=seam,
    )
    user = captured["user"]
    assert "the composed copy" in user
    assert "7500" in user
    assert "announcements" in user
    assert "ground every number" in user


def test_build_rubric_context_splits_exemplars_and_antipatterns(engine_conn):
    editor._add_editorial_memory(
        engine_conn,
        title="Good exemplar",
        body="ground every number in facts",
        kind_tag="exemplar",
        event_key="editorial_copy_edit:1",
        confidence=0.8,
        created_by="test",
    )
    editor._add_editorial_memory(
        engine_conn,
        title="Bad anti-pattern",
        body="never restate a raw signal",
        kind_tag="anti-pattern",
        event_key="editorial_deletion:1",
        confidence=0.75,
        created_by="test",
    )
    engine_conn.commit()

    rubric = editor.build_rubric_context(engine_conn)
    kinds = {r["kind"] for r in rubric}
    titles = {r["title"] for r in rubric}
    assert kinds == {"exemplar", "anti-pattern"}
    assert {"Good exemplar", "Bad anti-pattern"} <= titles


def test_record_verdict_persists_to_the_ledger(engine_conn):
    result = {
        "verdict": "revise",
        "critique": "thin substance",
        "dimensions": {"substance": {"ok": False, "note": "raw restatement"}},
    }
    vid = editor.record_verdict(
        engine_conn,
        result=result,
        intent_key=None,
        loop_number=42,
        lane="elixir",
        original_copy="before text",
        final_copy="after text",
        covers=["war_champ_lead_change:134:#X"],
        model="claude-sonnet-x",
    )
    assert vid is not None
    row = engine_conn.execute(
        "SELECT * FROM editor_verdicts WHERE verdict_id=?", (vid,)
    ).fetchone()
    assert row["verdict"] == "revise"
    assert row["loop_number"] == 42
    assert row["lane"] == "elixir"
    assert row["original_copy"] == "before text"
    assert row["final_copy"] == "after text"
    assert "war_champ_lead_change" in row["covers_json"]


def test_judge_delivered_post_records_verdict_and_feeds_lesson(
    engine_conn, monkeypatch
):
    """Observe-and-learn: a fallback verdict on a delivered post is recorded to
    editor_verdicts AND becomes an editorial anti-pattern memory for next time —
    without altering anything that shipped."""
    monkeypatch.setenv("ELIXIR_EDITOR_GATE", "1")
    monkeypatch.setattr(editor, "_editor_model", lambda: "test-model")
    # No network: feed a fixed fallback verdict through the judge seam.
    monkeypatch.setattr(
        editor,
        "judge",
        lambda **kw: {
            "verdict": "fallback",
            "critique": "claims a rank not in facts",
            "dimensions": {"grounding": {"ok": False, "note": "invented #1"}},
        },
    )
    result = editor.judge_delivered_post(
        engine_conn,
        post={"covers_signal_keys": ["war_champ:1"], "channel": "elixir"},
        evidence={"our_fame": 0},
        lane="elixir",
        content="We're #1 in the race!",
        discord_message_id="555",
        loop_number=200,
    )
    assert result["verdict"] == "fallback"
    # Verdict persisted, keyed to the live delivery.
    row = engine_conn.execute(
        "SELECT verdict, loop_number, lane, model FROM editor_verdicts "
        "WHERE loop_number=200"
    ).fetchone()
    assert row["verdict"] == "fallback"
    assert row["lane"] == "elixir"
    assert row["model"] == "test-model"
    # Lesson fed to the editorial rubric for the next compose.
    mem = engine_conn.execute(
        "SELECT title, body FROM memories WHERE source_event_key = 'editor_verdict:555'"
    ).fetchone()
    assert mem is not None
    assert "fallback" in mem["title"]
    assert "claims a rank not in facts" in mem["body"]


def test_judge_delivered_post_pass_records_no_lesson(engine_conn, monkeypatch):
    monkeypatch.setenv("ELIXIR_EDITOR_GATE", "1")
    monkeypatch.setattr(editor, "_editor_model", lambda: None)
    monkeypatch.setattr(
        editor, "judge", lambda **kw: {"verdict": "pass", "critique": "grounded"}
    )
    editor.judge_delivered_post(
        engine_conn,
        post={"covers_signal_keys": [], "channel": "elixir"},
        evidence={},
        lane="elixir",
        content="A grounded post about the war.",
        discord_message_id="777",
        loop_number=201,
    )
    assert (
        engine_conn.execute(
            "SELECT verdict FROM editor_verdicts WHERE loop_number=201"
        ).fetchone()["verdict"]
        == "pass"
    )
    # A pass feeds no anti-pattern memory.
    assert (
        engine_conn.execute(
            "SELECT 1 FROM memories WHERE source_event_key = 'editor_verdict:777'"
        ).fetchone()
        is None
    )


def test_judge_delivered_post_is_noop_when_disabled(engine_conn, monkeypatch):
    monkeypatch.setenv("ELIXIR_EDITOR_GATE", "0")
    called = {"judge": False}

    def _should_not_run(**kw):
        called["judge"] = True
        return {"verdict": "pass"}

    monkeypatch.setattr(editor, "judge", _should_not_run)
    out = editor.judge_delivered_post(
        engine_conn,
        post={"covers_signal_keys": []},
        evidence={},
        lane="elixir",
        content="anything",
        discord_message_id="888",
        loop_number=202,
    )
    assert out is None
    assert called["judge"] is False
    assert (
        engine_conn.execute("SELECT COUNT(*) FROM editor_verdicts").fetchone()[0] == 0
    )


def test_judge_delivered_post_swallows_errors(engine_conn, monkeypatch):
    monkeypatch.setenv("ELIXIR_EDITOR_GATE", "1")

    def boom(**kw):
        raise RuntimeError("rubric query blew up")

    monkeypatch.setattr(editor, "build_rubric_context", boom)
    # Must not raise — the delivery record may never be disturbed by a quality read.
    out = editor.judge_delivered_post(
        engine_conn,
        post={"covers_signal_keys": []},
        evidence={},
        lane="elixir",
        content="anything",
        discord_message_id="999",
        loop_number=203,
    )
    assert out is None


def test_record_verdict_rejects_off_contract_verdict(engine_conn):
    # The CHECK constraint is the last backstop; an off-contract verdict string
    # is caught and logged, not raised (best-effort ledger write).
    vid = editor.record_verdict(
        engine_conn,
        result={"verdict": "bogus"},
        intent_key=None,
        loop_number=1,
        lane="elixir",
        original_copy="a",
        final_copy="a",
    )
    assert vid is None
