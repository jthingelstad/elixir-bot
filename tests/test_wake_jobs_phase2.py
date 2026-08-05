"""Phase 2: roster wakes, per-job surfaces, and the v4 canary.

The design rule these guard is the one the plan calls binding: wake behaviour
differences are REGISTRY DATA. The day `respond.py` grows an `if event_type ==`
branch we are rebuilding v4's delivery.py — so these tests assert that adding a
job means adding a row, and that the rows say what a month of real posts said.
"""

from __future__ import annotations

import json

import pytest

from runtime.awareness import divergence, respond

# --------------------------------------------------------------------- registry


def test_every_job_has_a_prompt_file():
    """`job_prompt` maps `_` to `-`, so `role_change` reads `role-change.md`.

    A missing file raises inside `assemble_system`, i.e. at the moment a real
    member event fires in production — the most expensive place to find a typo.
    """
    import prompts

    for spec in respond.JOBS:
        text = prompts.job_prompt(spec.name)
        assert text.strip(), f"job {spec.name!r} has no prompt file"
        assert "# Job:" in text, f"job {spec.name!r} prompt is not a job file"


def test_job_by_event_type_is_derived_not_hand_written():
    """Two lists of the same fact drift. The map is built from the specs."""
    expected = {et: spec.name for spec in respond.JOBS for et in spec.event_types}
    assert respond.JOB_BY_EVENT_TYPE == expected


def test_no_event_type_is_claimed_by_two_jobs():
    seen: dict[str, str] = {}
    for spec in respond.JOBS:
        for event_type in spec.event_types:
            assert event_type not in seen, (
                f"{event_type} claimed by both {seen.get(event_type)} and {spec.name}"
            )
            seen[event_type] = spec.name


def test_every_claimed_event_type_exists_in_the_contract_registry():
    """A job claiming a typo'd event type would simply never fire."""
    from engine.event_contracts import EVENT_CONTRACTS

    for spec in respond.JOBS:
        for event_type in spec.event_types:
            assert event_type in EVENT_CONTRACTS, f"{event_type} is not a declared event"


def test_every_claimed_event_type_actually_wakes():
    """A job for a `digest`/`never` event is dead code: no wake ever carries it."""
    from engine.event_contracts import wake_policy

    for spec in respond.JOBS:
        for event_type in spec.event_types:
            wake_class, _ = wake_policy(event_type)
            assert wake_class in ("immediate", "batch"), (
                f"{event_type} is {wake_class}; a job for it would never run"
            )


@pytest.mark.parametrize(
    "job,lanes,clan_chat",
    [
        # Measured over 31 days of delivered intents, not chosen:
        ("welcome", ("announcements",), True),  # 12/12 announcements, 10/10 clan chat
        ("farewell", ("announcements",), True),  # 6/6 announcements, 3/7 clan chat
        ("role_change", ("announcements",), False),  # 7/7 announcements, 0/7 clan chat
        ("podium", ("announcements",), False),  # 1/1 announcements, 0/1 clan chat
        ("milestone_batch", ("elixir",), True),  # 28/28 elixir
    ],
)
def test_job_surfaces_match_the_measured_editorial_judgment(job, lanes, clan_chat):
    spec = respond.job_spec(job)
    assert spec is not None
    assert respond.lanes_for(spec) == lanes
    assert (respond._CLAN_CHAT in spec.surfaces) is clan_chat


def test_a_role_change_cannot_reach_the_elixir_lane():
    """The strict Discord split: roster facts go to announcements, never #elixir.

    The posting tool offers both lanes to every turn, so this is enforced against
    the job's declared surfaces at execution, not by trusting the prompt.
    """
    spec = respond.job_spec("role_change")
    assert respond._ELIXIR not in spec.surfaces
    assert "elixir" not in respond.lanes_for(spec)


# ------------------------------------------------------------------- the seed


def test_pending_events_carry_their_payload(engine_conn):
    """Without this every Phase 2 job composes blind.

    The responder has always read `event["payload"]`, but `pending_events` never
    selected `payload_json` — so the branch was live only in tests. A farewell
    could not see the leader's note, and a role change could not tell a promotion
    from a demotion.
    """
    from runtime.awareness import wake

    engine_conn.execute(
        "INSERT INTO clan_events (dedup_key, event_type, clan_tag, subject_tag, observed_at, "
        "timing, payload_json, scope, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "role_changed:#AAA:elder:2026-08-05T00:00:00Z",
            "role_changed",
            "#J2RGCRVG",
            "#AAA",
            "2026-08-05T00:00:00Z",
            "estimated",
            json.dumps(
                {
                    "name": "Tere",
                    "direction": "promoted",
                    "new_role": "elder",
                    "prev_role": "member",
                }
            ),
            "public",
            "2026-08-05T00:00:00Z",
        ),
    )
    engine_conn.commit()
    events = [e for e in wake.pending_events(engine_conn) if e["event_type"] == "role_changed"]
    assert events, "the event should be pending"
    payload = events[0].get("payload")
    assert payload and payload["direction"] == "promoted"
    assert payload["new_role"] == "elder"
    assert payload["name"] == "Tere", "the emitter now stamps the name into the floor"


def test_an_unparseable_payload_does_not_lose_the_event(engine_conn):
    """A bad payload costs that event its detail, never the tick — the events
    beside it may carry a hard-post floor."""
    from runtime.awareness import wake

    engine_conn.execute(
        "INSERT INTO clan_events (dedup_key, event_type, clan_tag, subject_tag, observed_at, "
        "timing, payload_json, scope, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "member_joined:#BAD:2026-08-05T00:00:00Z",
            "member_joined",
            "#J2RGCRVG",
            "#BAD",
            "2026-08-05T00:00:00Z",
            "estimated",
            "{not json",
            "public",
            "2026-08-05T00:00:00Z",
        ),
    )
    engine_conn.commit()
    events = [e for e in wake.pending_events(engine_conn) if e["subject_tag"] == "#BAD"]
    assert len(events) == 1
    assert "payload" not in events[0]


# ------------------------------------------------------------ the floor record


def test_a_wake_that_fails_every_tier_names_the_floor_it_missed():
    """The exit gate asks for zero floor misses; nothing recorded them before.

    `episode` used to be set only on the successful tier, and the caller records
    nothing when it is absent — so a wake that failed everything left one log
    line and no durable trace, invisible to the very query meant to find it.
    """
    wake = {
        "wake_class": "immediate",
        "wake_model": "lightweight",
        "reason": "1 immediate event(s): member_left_verified",
        "events": [
            {
                "signal_key": "member_left_verified:#AAA:2026-08-05T00:00:00Z",
                "event_type": "member_left_verified",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-05T00:00:00Z",
                "payload": {"name": "someone", "tenure_days": 10},
            }
        ],
    }

    def _never_delivers(read, plan):  # pragma: no cover - not reached
        raise AssertionError("should not deliver when no tier produced a post")

    import agent.chassis as chassis_mod

    original = chassis_mod.run_turn
    chassis_mod.run_turn = lambda attention, seed, on_event=None: {
        "job": attention.job,
        "workflow": attention.workflow,
        "posts": [],
        "rejections": ["nope"],
    }
    try:
        outcome = respond.respond(wake, deliver_fn=_never_delivers)
    finally:
        chassis_mod.run_turn = original

    assert outcome["handled"] is False
    assert outcome["uncovered_floor"] == ["member_left_verified:#AAA:2026-08-05T00:00:00Z"]
    assert outcome["episode"]["floor"] == outcome["uncovered_floor"]
    assert outcome["episode"]["job"] == "farewell"


# ---------------------------------------------------------------- the canary


def test_divergence_flags_one_signal_covered_twice(engine_conn):
    """The v4 failure: two authors, one moment."""
    for key in ("intent-a", "intent-b"):
        engine_conn.execute(
            "INSERT INTO awareness_delivery_intents (intent_key, lane, content, covers_json, "
            "post_json, status, attempts, created_at, updated_at, fulfilled_at) "
            "VALUES (?,?,?,?,?,'fulfilled',1,?,?,?)",
            (
                key,
                "announcements",
                "text",
                json.dumps(["member_joined:#AAA:2026-08-05T00:00:00Z"]),
                json.dumps({"member_tags": ["#AAA"]}),
                "2026-08-05T00:00:00Z",
                "2026-08-05T00:00:00Z",
                "2026-08-05T00:00:00Z",
            ),
        )
    engine_conn.commit()
    result = divergence.check_divergence(hours=24 * 3650, conn=engine_conn)
    assert len(result["overlap"]) == 1
    assert result["overlap"][0]["signal_key"] == "member_joined:#AAA:2026-08-05T00:00:00Z"
    assert sorted(result["overlap"][0]["intents"]) == ["intent-a", "intent-b"]


def test_divergence_is_clean_when_each_signal_has_one_post(engine_conn):
    engine_conn.execute(
        "INSERT INTO awareness_delivery_intents (intent_key, lane, content, covers_json, "
        "post_json, status, attempts, created_at, updated_at, fulfilled_at) "
        "VALUES (?,?,?,?,?,'fulfilled',1,?,?,?)",
        (
            "solo",
            "announcements",
            "text",
            json.dumps(["member_joined:#AAA:2026-08-05T00:00:00Z"]),
            json.dumps({"member_tags": ["#AAA"]}),
            "2026-08-05T00:00:00Z",
            "2026-08-05T00:00:00Z",
            "2026-08-05T00:00:00Z",
        ),
    )
    engine_conn.commit()
    result = divergence.check_divergence(hours=24 * 3650, conn=engine_conn)
    assert result["overlap"] == []
