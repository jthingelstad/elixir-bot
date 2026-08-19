"""The scoped responder — floor, escalation, and the single delivery path.

The responder is the first thing that can post to the clan without the brain
having thought about it, so the tests that matter are the ones that constrain
what it may do when the model underperforms:

- an uncovered hard-post floor must FAIL the wake, not ship a partial post
- a wake may only fail upward (cheap model → strong model → daily brain)
- delivery must go through the caller's deliver_fn, never a path of its own

No model is called. run_turn is stubbed with the episodes a model would produce.
"""

from __future__ import annotations

import pytest

from agent import chassis
from runtime.awareness import respond

JOIN_KEY = "member_joined:#AAA:2026-08-04T12:00:00Z"


def _wake(**kw):
    base = {
        "wake_class": "immediate",
        "wake_model": "lightweight",
        "reason": "1 immediate event(s): member_joined",
        "events": [
            {
                "signal_key": JOIN_KEY,
                "event_type": "member_joined",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-04T12:00:00Z",
                "payload": {"name": "blackberry", "trophies": 12535},
            }
        ],
    }
    base.update(kw)
    return base


def _episode(posts, **kw):
    base = {"job": "welcome", "workflow": "wake_response", "posts": posts, "rejections": []}
    base.update(kw)
    return base


def _good_post(covers=(JOIN_KEY,)):
    return {
        "channel": "announcements",
        "content": "**Welcome to POAP KINGS.** Glad to have you.",
        "covers_signal_keys": list(covers),
        "clan_chat": ["Welcome to POAP KINGS, blackberry."],
    }


@pytest.fixture
def seeded(engine_conn):
    # clan_memberships carries an FK to clans as well as players.
    engine_conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', '2026-08-04', 1)"
    )
    engine_conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, display_name, "
        "first_seen_at, last_seen_at) VALUES ('#AAA','blackberry','blackberry',"
        "'2026-08-04','2026-08-04')"
    )
    engine_conn.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
        "VALUES ('#AAA', '#J2RGCRVG', '2026-08-04T12:00:00Z', 'api')"
    )
    engine_conn.commit()
    return engine_conn


def test_a_join_maps_to_the_welcome_job():
    assert respond.job_for(_wake()["events"]) == "welcome"


def test_a_mixed_wake_falls_through_to_the_daily_brain():
    """Two jobs for one moment would mean two authors — the divergence this
    whole design exists to avoid. Paying a brain tick is the cheaper mistake."""
    events = _wake()["events"] + [
        {"signal_key": "week_finished:134:3", "event_type": "week_finished"}
    ]
    assert respond.job_for(events) is None


def test_the_seed_precomputes_whether_this_is_a_return(seeded):
    """Left to infer from a stint list, a model reads a returning member as
    brand new — and the welcome is wrong in a way members notice."""
    seed = respond.build_seed(_wake()["events"], seeded)
    member = seed["events"][0]["member"]
    assert member["name"] == "blackberry"
    assert member["is_returning"] is False

    seeded.execute("UPDATE clan_memberships SET left_at = '2026-06-01' WHERE player_tag='#AAA'")
    seeded.execute(
        "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
        "VALUES ('#AAA', '#J2RGCRVG', '2026-08-04T12:00:00Z', 'api')"
    )
    seeded.commit()
    assert respond.build_seed(_wake()["events"], seeded)["events"][0]["member"]["is_returning"]


def test_the_seed_carries_the_live_join_floor(seeded):
    """Never a remembered number: a stale one told a member who joined at 7,053
    that they were clear of a '2,000-trophy entry line' when the floor was
    7,000."""
    seed = respond.build_seed(_wake()["events"], seeded)
    assert "required_trophies" in seed["clan"]


def test_an_uncovered_floor_fails_the_wake_and_delivers_nothing(seeded, monkeypatch):
    """The safety property. Coverage is checked against what reached the
    outbox, never against what the model claimed."""
    delivered = []
    monkeypatch.setattr(
        chassis, "run_turn", lambda a, s, **kw: _episode([_good_post(covers=["something_else"])])
    )
    outcome = respond.respond(
        _wake(),
        deliver_fn=lambda read, plan: delivered.append(plan) or {"delivered": 1},
        conn=seeded,
    )
    assert outcome["handled"] is False
    assert delivered == [], "a post that misses the floor must never be delivered"


def test_a_turn_that_produces_nothing_leaves_it_for_the_daily_brain(seeded, monkeypatch):
    monkeypatch.setattr(chassis, "run_turn", lambda a, s, **kw: _episode([]))
    outcome = respond.respond(_wake(), deliver_fn=lambda read, plan: {"delivered": 1}, conn=seeded)
    assert outcome["handled"] is False
    assert "daily deliberation" in outcome["reason"]


def test_a_wake_can_only_fail_upward(seeded, monkeypatch):
    """Haiku first; if it produces nothing usable, Sonnet composes. A wake never
    degrades into silence."""
    tiers = []

    def _run(attention, seed, **kw):
        tiers.append(attention.budget.model_family)
        if attention.budget.model_family == "lightweight":
            return _episode([])  # nothing usable
        return _episode([_good_post()], workflow="wake_response_chat")

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )
    assert tiers == ["lightweight", "chat"]
    assert outcome["handled"] is True and outcome["tier"] == "chat"


def test_a_sonnet_wake_does_not_start_on_haiku(seeded, monkeypatch):
    """The registry's wake_model is the composer tier, not a starting point."""
    tiers = []

    def _run(attention, seed, **kw):
        tiers.append(attention.budget.model_family)
        return _episode([_good_post()])

    monkeypatch.setattr(chassis, "run_turn", _run)
    respond.respond(
        _wake(wake_model="chat"),
        deliver_fn=lambda read, plan: {"delivered": 1, "failed": False},
        conn=seeded,
    )
    assert tiers == ["chat"]


def test_delivery_goes_through_the_callers_deliver_fn(seeded, monkeypatch):
    """The responder must not own a delivery path. What it hands over has to be
    a plan deliver_posts understands, carrying this wake's floor as
    hard_post_signals."""
    captured = {}

    def _deliver(read, plan):
        captured["read"] = read
        captured["plan"] = plan
        return {"delivered": 1, "failed": False}

    monkeypatch.setattr(chassis, "run_turn", lambda a, s, **kw: _episode([_good_post()]))
    outcome = respond.respond(_wake(), deliver_fn=_deliver, conn=seeded)

    assert outcome["handled"] is True
    assert [s["signal_key"] for s in captured["read"]["hard_post_signals"]] == [JOIN_KEY]
    post = captured["plan"]["posts"][0]
    assert post["channel"] == "announcements"
    assert post["clan_chat"], "the in-game sibling must survive into the plan"


def test_the_responder_ships_disabled(monkeypatch):
    """Phase 1 lands OFF. The wake evaluator keeps shadowing either way."""
    monkeypatch.delenv("ELIXIR_WAKE_RESPONDER", raising=False)
    assert respond.responder_enabled() is False
    monkeypatch.setenv("ELIXIR_WAKE_RESPONDER", "1")
    assert respond.responder_enabled() is True


def test_an_escalation_keeps_the_failed_rung_in_the_recorded_episode(seeded, monkeypatch):
    """A won wake must still explain what the cheap tier did.

    Over the Phase 2 exit-gate window 10 of 41 wakes escalated, and because only
    the winning tier's episode was stored, not one of them left a durable trace
    of WHY Haiku lost — the diagnosis had to come from a log line that says
    nothing but "produced no post". Same blindness the fully-failed path was
    fixed for, one rung down.
    """

    def _run(attention, seed, **kw):
        if attention.budget.model_family == "lightweight":
            return _episode([], rejections=["bounced once, then gave up"])
        return _episode([_good_post()], workflow="wake_response_chat")

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )

    assert outcome["handled"] is True and outcome["tier"] == "chat"
    preceding = outcome["episode"]["preceding_attempts"]
    assert len(preceding) == 1
    assert preceding[0]["workflow"] == "wake_response"
    assert preceding[0]["rejections"] == ["bounced once, then gave up"]
    # The winner still identifies itself as the winner: job/workflow/tier are
    # what record_episode() writes into its own columns.
    assert outcome["episode"]["workflow"] == "wake_response_chat"


def test_a_wake_won_on_the_first_tier_carries_no_preceding_attempts(seeded, monkeypatch):
    """The common case stays exactly as small as it was."""
    monkeypatch.setattr(chassis, "run_turn", lambda a, s, **kw: _episode([_good_post()]))
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )
    assert "preceding_attempts" not in outcome["episode"]
