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
