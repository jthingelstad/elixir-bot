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
        "signals_by_lane": {
            "war": [],
            "battle_mode": [],
            "milestone": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        },
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
        signals_by_lane={
            "clan_event": [hp],
            "war": [],
            "battle_mode": [],
            "milestone": [],
            "leadership": [],
            "system": [],
        },
    )
    cls = gate.classify(read)
    assert cls["tier"] == "deliberate"
    assert "member_joined" in cls["reason"]


def test_classify_deliberate_on_due_revisit():
    cls = gate.classify(_empty_read(due_revisits=[{"signal_key": "k1"}]))
    assert cls["tier"] == "deliberate"


def test_classify_deliberate_on_legendary_badge():
    """A one-off Legendary badge is notable — it must reach the brain, never be
    gated to silence by the cheap triage."""
    sig = {
        "signal_key": "badge_earned:#Y:Chaos_S2",
        "event_type": "badge_earned",
        "badge_tier": "legendary",
        "badge_name": "Chaos_S2",
    }
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        }
    )
    cls = gate.classify(read)
    assert cls["tier"] == "deliberate"
    assert "notable" in cls["reason"]


def test_classify_deliberate_on_arena_climb():
    sig = {
        "signal_key": "arena_changed:#Y:1",
        "event_type": "arena_changed",
        "arena_name": "Spirit Square",
    }
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        }
    )
    assert gate.classify(read)["tier"] == "deliberate"


def test_classify_routine_badge_still_triage_not_deliberate():
    """A leveled/routine badge is NOT notable — it stays in the cheap triage lane."""
    sig = {
        "signal_key": "badge_earned:#Y:MasteryLog",
        "event_type": "badge_earned",
        "badge_tier": "routine",
        "badge_name": "MasteryLog",
    }
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        }
    )
    assert gate.classify(read)["tier"] == "triage"


def test_classify_heartbeat_quiet_stretch_deliberates():
    """A long quiet stretch WITH a real soft signal → deliberate (compose a
    heartbeat roundup), rather than gating to silence."""
    sig = {
        "signal_key": "card_level_milestone:#Y:1",
        "event_type": "card_level_milestone",
    }
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        },
        posting_pulse={"is_quiet_stretch": True, "hours_since_last_post": 12.0},
    )
    cls = gate.classify(read)
    assert cls["tier"] == "deliberate"
    assert "quiet stretch" in cls["reason"]


def test_classify_not_quiet_routine_soft_stays_triage():
    """Same routine signal, but NOT a quiet stretch → stays triage (cheap)."""
    sig = {
        "signal_key": "card_level_milestone:#Y:1",
        "event_type": "card_level_milestone",
    }
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        },
        posting_pulse={"is_quiet_stretch": False, "hours_since_last_post": 2.0},
    )
    assert gate.classify(read)["tier"] == "triage"


def test_classify_triage_on_soft_milestone():
    sig = {
        "signal_key": "card_level_milestone:#Y:1",
        "event_type": "card_level_milestone",
    }
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        },
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


def _anniversary_read(*, posted: bool):
    """A read with a join-anniversary sitting in cake_days_today AND the
    clan_event lane. When ``posted`` is true, channel_memory carries a fulfilled
    post that already named the member today (the over-escalation setup)."""
    cake = {
        "type": "join_anniversary",
        "name": "kiruba",
        "subject_tag": "#KIR",
        "signal_key": "join_anniversary:#KIR:2026-07-14",
        "months": 3,
        "is_annual": False,
    }
    lane_sig = {
        "event_type": "join_anniversary",
        "subject_tag": "#KIR",
        "signal_key": "join_anniversary:#KIR:2026-07-14",
    }
    read = _empty_read(
        generated_at="2026-07-14T11:05:00Z",
        cake_days_today=[cake],
        signals_by_lane={
            "clan_event": [lane_sig],
            "war": [],
            "battle_mode": [],
            "milestone": [],
            "leadership": [],
            "system": [],
        },
    )
    if posted:
        read["channel_memory"] = {
            "announcements": {
                "recent_posts": [
                    {
                        "posted": True,
                        "posted_at": "2026-07-14T05:05:45Z",
                        "preview": "**3 months in.** kiruba and ryguy67 both hit the 3-month mark today.",
                    }
                ]
            }
        }
    return read


def test_already_posted_cake_day_does_not_escalate():
    """The over-escalation leak (#120): an anniversary already posted earlier today
    keeps re-triggering the gate. It must be filtered from BOTH the cake context
    and the clan_event lane so it can't push a triage into a wasted Sonnet run."""
    read = _anniversary_read(posted=True)
    assert gate._fresh_cake_days(read) == []  # filtered from cake context
    assert gate._soft_lane_signals(read) == []  # filtered from the lane too
    cls = gate.classify(read)
    assert cls["tier"] == "skip"  # nothing fresh left → free silence
    assert "cake" not in cls["reason"].lower()
    # and the triage (if it ran) would see no cake day to re-post
    import json

    triage_payload = json.loads(gate._compact_read_for_triage(read))
    assert triage_payload["cake_days_today"] == []
    assert triage_payload["soft_signals"] == []


def test_fresh_cake_day_still_reaches_triage():
    """The fix must NOT suppress a genuine, not-yet-posted anniversary — with no
    prior post today it stays a live cake day the gate can act on."""
    read = _anniversary_read(posted=False)
    assert [c["name"] for c in gate._fresh_cake_days(read)] == ["kiruba"]
    assert gate.classify(read)["tier"] == "triage"
    import json

    triage_payload = json.loads(gate._compact_read_for_triage(read))
    assert len(triage_payload["cake_days_today"]) == 1


def test_hard_post_not_double_counted_as_soft():
    """A hard-post is mirrored into signals_by_lane; it must not also register as
    a soft-lane signal (which would be a redundant classification)."""
    hp = {"signal_key": "member_joined:#X:t", "event_type": "member_joined"}
    read = _empty_read(
        hard_post_signals=[hp],
        signals_by_lane={
            "clan_event": [hp],
            "war": [],
            "battle_mode": [],
            "milestone": [],
            "leadership": [],
            "system": [],
        },
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
        signals_by_lane={
            "clan_event": [hp],
            "war": [],
            "battle_mode": [],
            "milestone": [],
            "leadership": [],
            "system": [],
        },
    )
    out = gate.decide(read, triage_fn=lambda r: {"decision": "silent", "reason": "x"})
    assert out["deliberate"] is True
    assert out["tier"] == "deliberate"


def test_decide_triage_silent_is_gated_silence(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")
    sig = {"signal_key": "m:1", "event_type": "card_level_milestone"}
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        }
    )
    out = gate.decide(
        read, triage_fn=lambda r: {"decision": "silent", "reason": "in cooldown"}
    )
    assert out["deliberate"] is False
    assert out["decider"] == "triage"
    assert "cooldown" in out["silence_reason"]


def test_decide_triage_post_escalates_to_brain(monkeypatch):
    monkeypatch.setenv("ELIXIR_AWARENESS_GATE", "1")
    sig = {"signal_key": "m:1", "event_type": "card_level_milestone"}
    read = _empty_read(
        signals_by_lane={
            "milestone": [sig],
            "war": [],
            "battle_mode": [],
            "clan_event": [],
            "leadership": [],
            "system": [],
        }
    )
    out = gate.decide(
        read, triage_fn=lambda r: {"decision": "post", "reason": "fresh unlock"}
    )
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
    v = gate.triage(
        _empty_read(),
        generate=lambda s, u: '{"decision": "silent", "reason": "routine"}',
    )
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
