"""Tests for the awareness cost gate (runtime/awareness/gate.py).

The gate sits in front of the expensive Sonnet brain: hard-posts and explicit
revisits deliberate; soft-signal ticks go through a cheap Haiku triage that can
only gate (never post); empty ticks are a deterministic silence with no LLM.
"""

import elixir  # noqa: F401  (full runtime init before importing internals)

from runtime.awareness import gate


# ---------------------------------------------------------------------------
# Deterministic classification
# ---------------------------------------------------------------------------

def _empty_read(**over):
    read = {
        "generated_at": "2026-07-12T22:00:00Z",
        "signals_by_lane": {"war": [], "battle_mode": [], "milestone": [],
                            "clan_event": [], "leadership": [], "system": []},
        "hard_post_signals": [],
        "cake_days_today": [],
        "decision_cases": {"due": [], "open": []},
        "management": {"actionable": {"kick": [], "promote": [], "demote": []}},
        "due_revisits": [],
    }
    read.update(over)
    return read


def test_classify_skip_on_fully_empty_read():
    cls = gate.classify(_empty_read())
    assert cls["tier"] == "skip"


def test_classify_deliberate_on_hard_post():
    hp = {"signal_key": "member_joined:#X:t", "event_type": "member_joined"}
    read = _empty_read(
        hard_post_signals=[hp],
        signals_by_lane={"clan_event": [hp], "war": [], "battle_mode": [],
                         "milestone": [], "leadership": [], "system": []},
    )
    cls = gate.classify(read)
    assert cls["tier"] == "deliberate"
    assert "member_joined" in cls["reason"]


def test_classify_deliberate_on_due_revisit():
    cls = gate.classify(_empty_read(due_revisits=[{"signal_key": "k1"}]))
    assert cls["tier"] == "deliberate"


def test_classify_triage_on_soft_milestone():
    sig = {"signal_key": "card_level_milestone:#Y:1", "event_type": "card_level_milestone"}
    read = _empty_read(
        signals_by_lane={"milestone": [sig], "war": [], "battle_mode": [],
                         "clan_event": [], "leadership": [], "system": []},
    )
    cls = gate.classify(read)
    assert cls["tier"] == "triage"


def test_classify_triage_on_standing_context_only():
    """Cake days / due cases / mgmt candidates are persistent standing state —
    they route to (cheap) triage, never an unconditional deliberate."""
    for over in (
        {"cake_days_today": [{"member_ref": "Fullboat"}]},
        {"decision_cases": {"due": [{"case_id": 366}], "open": []}},
        {"management": {"actionable": {"kick": ["#Z"], "promote": [], "demote": []}}},
    ):
        cls = gate.classify(_empty_read(**over))
        assert cls["tier"] == "triage", over


def test_hard_post_not_double_counted_as_soft():
    """A hard-post is mirrored into signals_by_lane; it must not also register as
    a soft-lane signal (which would be a redundant classification)."""
    hp = {"signal_key": "member_joined:#X:t", "event_type": "member_joined"}
    read = _empty_read(
        hard_post_signals=[hp],
        signals_by_lane={"clan_event": [hp], "war": [], "battle_mode": [],
                         "milestone": [], "leadership": [], "system": []},
    )
    assert gate._soft_lane_signals(read) == []


# ---------------------------------------------------------------------------
# Orchestration: decide()
# ---------------------------------------------------------------------------

def test_decide_skip_is_silence_no_triage(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")
    called = {"n": 0}

    def _triage(read):
        called["n"] += 1
        return {"decision": "silent", "reason": "x"}

    out = gate.decide(_empty_read(), triage_fn=_triage)
    assert out["deliberate"] is False
    assert out["decider"] == "gate"
    assert called["n"] == 0  # no triage call on a fully-empty tick


def test_decide_hard_post_deliberates_without_triage(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")
    hp = {"signal_key": "member_joined:#X:t", "event_type": "member_joined"}
    read = _empty_read(
        hard_post_signals=[hp],
        signals_by_lane={"clan_event": [hp], "war": [], "battle_mode": [],
                         "milestone": [], "leadership": [], "system": []},
    )
    out = gate.decide(read, triage_fn=lambda r: {"decision": "silent", "reason": "x"})
    assert out["deliberate"] is True
    assert out["tier"] == "deliberate"


def test_decide_triage_silent_is_gated_silence(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")
    sig = {"signal_key": "m:1", "event_type": "card_level_milestone"}
    read = _empty_read(signals_by_lane={"milestone": [sig], "war": [], "battle_mode": [],
                                        "clan_event": [], "leadership": [], "system": []})
    out = gate.decide(read, triage_fn=lambda r: {"decision": "silent", "reason": "in cooldown"})
    assert out["deliberate"] is False
    assert out["decider"] == "triage"
    assert "cooldown" in out["silence_reason"]


def test_decide_triage_post_escalates_to_brain(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")
    sig = {"signal_key": "m:1", "event_type": "card_level_milestone"}
    read = _empty_read(signals_by_lane={"milestone": [sig], "war": [], "battle_mode": [],
                                        "clan_event": [], "leadership": [], "system": []})
    out = gate.decide(read, triage_fn=lambda r: {"decision": "post", "reason": "fresh unlock"})
    assert out["deliberate"] is True
    assert "escalated" in out["reason"]


def test_decide_disabled_always_deliberates(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "0")
    out = gate.decide(_empty_read())  # fully empty, but gate off
    assert out["deliberate"] is True
    assert out["tier"] == "disabled"


def test_triage_failsafe_to_post_on_error(monkeypatch):
    """A triage call that raises / returns junk must fail SAFE to post (escalate),
    never silently drop the tick."""
    def _boom(system, user):
        raise RuntimeError("api down")

    v = gate.triage(_empty_read(), generate=_boom)
    assert v["decision"] == "post"

    v2 = gate.triage(_empty_read(), generate=lambda s, u: "")
    assert v2["decision"] == "post"

    v3 = gate.triage(_empty_read(), generate=lambda s, u: "no clear verdict here")
    assert v3["decision"] == "post"


def test_triage_parses_json_verdict():
    v = gate.triage(_empty_read(), generate=lambda s, u: '{"decision": "silent", "reason": "routine"}')
    assert v == {"decision": "silent", "reason": "routine"}
    # tolerant of code fences / surrounding prose
    fenced = '```json\n{"decision": "post", "reason": "new cake day"}\n```'
    v2 = gate.triage(_empty_read(), generate=lambda s, u: fenced)
    assert v2["decision"] == "post"


# ---------------------------------------------------------------------------
# Loop integration: the gate actually skips the brain
# ---------------------------------------------------------------------------

def test_loop_gated_silence_skips_the_brain(monkeypatch):
    """On a fully-empty read the loop records a deterministic silence and NEVER
    calls the expensive brain."""
    from unittest.mock import patch
    from runtime.awareness import loop as loop_mod

    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")

    with patch("agent.workflows.run_awareness_tick") as brain:
        counters = loop_mod.run_awareness_loop()

    brain.assert_not_called()
    assert counters["chose_silence"] is True
    assert counters["gate_tier"] == "skip"
    assert counters["tool_calls"] == 0
