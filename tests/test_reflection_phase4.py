"""Phase 4 — the learning loop, and the three properties that make it safe.

A lesson written here is injected into EVERY chassis turn from the moment it
exists, unchallenged, until a human deletes it. That asymmetry — cheap to write,
expensive to be wrong, invisible once it is in — is what these tests are about.
They are less about the feature working and more about it being unable to run
away.
"""

from __future__ import annotations

import pytest

from engine import editor
from runtime.jobs import _reflection


@pytest.fixture
def posted_intent(engine_conn):
    engine_conn.execute(
        "INSERT INTO awareness_delivery_intents "
        "(intent_key, lane, content, covers_json, post_json, status, attempts, "
        " created_at, updated_at, fulfilled_at, discord_message_id) "
        "VALUES (?,?,?,?,?,'fulfilled',1,?,?,?,?)",
        (
            "awareness:test1",
            "announcements",
            "**Welcome to POAP KINGS, someone.**",
            '["member_joined:#AAA"]',
            "{}",
            "2026-08-19T01:00:00Z",
            "2026-08-19T01:00:00Z",
            "2026-08-19T01:00:00Z",
            "999123",
        ),
    )
    engine_conn.commit()
    return "999123"


# --- the reaction feeder ------------------------------------------------------


def test_a_reaction_is_evidence_and_never_an_injected_lesson(engine_conn, posted_intent):
    """The property that keeps the 12-slot injection budget meaningful.

    The chassis selects lessons by the `editorial` tag. A raw reaction landing in
    that set would evict a real lesson in order to tell every future turn that
    somebody once pressed a thumbs-up.
    """
    memory_id = editor.record_post_reaction(
        engine_conn, discord_message_id=posted_intent, emoji="👍", reactor_id="42"
    )
    assert memory_id

    tags = {
        r[0]
        for r in engine_conn.execute(
            "SELECT tag FROM memory_tags WHERE memory_id = ?", (memory_id,)
        )
    }
    assert "editorial-feeder" in tags
    assert "editorial" not in tags, "a reaction must not be injected as guidance"
    assert "reaction" in tags


def test_a_reaction_on_something_elixir_did_not_post_is_ignored(engine_conn):
    """The join is the delivery intent. Without it, every reaction in every
    channel would become editorial evidence about Elixir's writing."""
    assert (
        editor.record_post_reaction(
            engine_conn, discord_message_id="not-an-elixir-post", emoji="👍", reactor_id="42"
        )
        is None
    )


def test_adding_and_removing_the_same_reaction_records_both(engine_conn, posted_intent):
    """A leader who takes a thumbs-up back has changed their mind, and the
    reflection should see the change rather than only the first half."""
    added = editor.record_post_reaction(
        engine_conn, discord_message_id=posted_intent, emoji="👍", reactor_id="42"
    )
    removed = editor.record_post_reaction(
        engine_conn, discord_message_id=posted_intent, emoji="👍", reactor_id="42", removed=True
    )
    assert added and removed and added != removed


def test_the_same_reaction_twice_is_recorded_once(engine_conn, posted_intent):
    """Discord can redeliver; the event key is what makes that harmless."""
    first = editor.record_post_reaction(
        engine_conn, discord_message_id=posted_intent, emoji="👍", reactor_id="42"
    )
    again = editor.record_post_reaction(
        engine_conn, discord_message_id=posted_intent, emoji="👍", reactor_id="42"
    )
    assert first and again is None


# --- the lesson caps ----------------------------------------------------------


def _lesson(title="Vary the welcome opener", confidence=0.8, evidence="awareness:test1"):
    return {
        "title": title,
        "body": "Three welcomes opened with the same clause.",
        "evidence": evidence,
        "confidence": confidence,
    }


def test_a_lesson_without_evidence_is_dropped(engine_conn):
    """Property 2. An unfalsifiable instruction with a permanent audience is the
    worst thing this job could produce."""
    written = _reflection._persist_lessons(
        engine_conn, [_lesson(evidence=""), _lesson(evidence="   ")], ""
    )
    assert written == []


def test_a_low_confidence_lesson_is_dropped(engine_conn):
    """It still reaches every turn, so 'possible' is not a high enough bar."""
    assert _reflection._persist_lessons(engine_conn, [_lesson(confidence=0.2)], "") == []


def test_no_more_than_three_lessons_are_written_in_one_night(engine_conn):
    """Property 1, enforced in code rather than requested in the prompt, because
    a request is a suggestion and a model asked for lessons will find some."""
    lessons = [_lesson(title=f"Lesson {i}", evidence=f"awareness:evidence-{i}") for i in range(10)]
    written = _reflection._persist_lessons(engine_conn, lessons, "")
    assert len(written) <= _reflection.MAX_LESSONS_PER_NIGHT == 3


def test_the_same_evidence_does_not_produce_a_second_copy_of_a_lesson(engine_conn):
    """Deduped on the evidence, not the wording: tomorrow's pass re-reads the
    same 24h window at the boundary and must not restate yesterday's rule."""
    first = _reflection._persist_lessons(engine_conn, [_lesson()], "")
    second = _reflection._persist_lessons(
        engine_conn, [_lesson(title="Completely different words, same evidence")], ""
    )
    assert len(first) == 1 and second == []


def test_a_written_lesson_is_injected_and_carries_its_evidence(engine_conn):
    """The other half of the tagging rule: a real lesson SHOULD reach turns."""
    [memory_id] = _reflection._persist_lessons(engine_conn, [_lesson()], "")
    tags = {
        r[0]
        for r in engine_conn.execute(
            "SELECT tag FROM memory_tags WHERE memory_id = ?", (memory_id,)
        )
    }
    assert "editorial" in tags and "lesson" in tags
    body = engine_conn.execute(
        "SELECT body FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()[0]
    assert "EVIDENCE:" in body


# --- the job's own restraint --------------------------------------------------


def test_reflection_ships_disabled(monkeypatch):
    """Writing lessons that reach every turn is what earns a flag."""
    from runtime.prompt_feedback import reflection_enabled

    monkeypatch.delenv("ELIXIR_REFLECTION", raising=False)
    assert reflection_enabled() is False
    monkeypatch.setenv("ELIXIR_REFLECTION", "1")
    assert reflection_enabled() is True


def test_a_quiet_day_makes_no_model_call(engine_conn, monkeypatch):
    """Paying a model to be told nothing happened is not a learning loop."""
    import asyncio

    monkeypatch.setenv("ELIXIR_REFLECTION", "1")
    called = {"n": 0}

    import elixir_agent

    monkeypatch.setattr(
        elixir_agent, "run_reflection", lambda ctx: called.__setitem__("n", called["n"] + 1)
    )
    monkeypatch.setattr(
        _reflection,
        "build_reflection_context",
        lambda conn, hours=24: {"intents": [], "reactions": [], "silences": []},
    )
    asyncio.run(_reflection._reflection_cycle())
    assert called["n"] == 0


def test_the_reflection_workflow_is_toolless(engine_conn):
    """It reasons only about the evidence it was handed. A tool would let it go
    and find a fact to justify a lesson it already wanted to write."""
    from agent.workflow_registry import get_workflow_spec

    spec = get_workflow_spec("reflection")
    assert spec.tools_allowed is False
    assert not spec.tools


def test_reflection_is_not_budget_gated_but_also_not_essential():
    """A scheduled job: outside the discretionary ceiling entirely, per the
    rule that a cost control for conversation must not delete promised work —
    and not ESSENTIAL either, because it carries no hard post."""
    from agent import spend_budget

    assert "reflection" not in spend_budget.BUDGETED
    assert "reflection" not in spend_budget.ESSENTIAL
