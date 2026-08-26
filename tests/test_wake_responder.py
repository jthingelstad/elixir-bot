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


def _tier(attention):
    return "chat" if attention.workflow == "wake_response_chat" else "lightweight"


def _followup_wake():
    return _wake(
        reason="1 immediate event(s): followup_due",
        events=[
            {
                "signal_key": "followup_due:17",
                "event_type": "followup_due",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-20T12:00:00Z",
                "payload": {"followup_id": 17, "why": "ask how the new phone worked out"},
            }
        ],
    )


def _arena_wake():
    return _wake(
        reason="1 batch event(s): arena_changed",
        events=[
            {
                "signal_key": "arena_changed:#AAA:54000020",
                "event_type": "arena_changed",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-20T17:07:47Z",
                "payload": {"name": "blackberry", "arena_name": "Valkalla"},
            }
        ],
    )


def _champion_wake():
    return _wake(
        reason="1 immediate event(s): champion_league_reached",
        events=[
            {
                "signal_key": "champion_league_reached:#AAA:5",
                "event_type": "champion_league_reached",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-24T08:32:48Z",
                "payload": {"league": "Grand Champion", "league_tier": 5},
            }
        ],
    )


def _arena_batch_wake():
    return _wake(
        reason="2 batch event(s): arena_changed",
        events=[
            {
                "signal_key": "arena_changed:#AAA:54000020",
                "event_type": "arena_changed",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-25T21:08:30Z",
                "payload": {"name": "blackberry", "arena_name": "Valkalla"},
            },
            {
                "signal_key": "arena_changed:#BBB:54000141",
                "event_type": "arena_changed",
                "subject_tag": "#BBB",
                "observed_at": "2026-08-25T21:18:34Z",
                "payload": {"name": "blueberry", "arena_name": "Magic Academy"},
            },
        ],
    )


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


def test_an_explicit_followup_silence_is_consumed_without_delivery(seeded, monkeypatch):
    monkeypatch.setattr(
        chassis,
        "run_turn",
        lambda a, s, **kw: _episode(
            [],
            job="followup",
            intentionally_silent=True,
            silence_reason="The member is already back and playing.",
        ),
    )
    delivered = []
    outcome = respond.respond(
        _followup_wake(),
        deliver_fn=lambda read, plan: delivered.append(plan),
        conn=seeded,
    )

    assert outcome["consumed"] is True
    assert outcome["intentionally_silent"] is True
    assert outcome["delivered"] == 0
    assert delivered == []


def test_a_followup_may_stage_clan_chat_without_discord(seeded, monkeypatch):
    post = {
        "channel": "clan_chat",
        "content": "How did the new phone work out?",
        "clan_chat": ["How did the new phone work out?"],
        "covers_signal_keys": ["followup_due:17"],
    }
    monkeypatch.setattr(
        chassis,
        "run_turn",
        lambda a, s, **kw: _episode([post], job="followup"),
    )
    captured = {}

    def _deliver(read, plan):
        captured["plan"] = plan
        return {"delivered": 1, "failed": False}

    outcome = respond.respond(_followup_wake(), deliver_fn=_deliver, conn=seeded)

    assert outcome["consumed"] is True
    assert outcome["intentionally_silent"] is False
    assert captured["plan"]["posts"][0]["channel"] == "clan_chat"


def test_a_bare_arena_relay_is_rejected_before_delivery(seeded, monkeypatch):
    """Natural action 310 was exactly this shape: a bare arena wake staged only
    clan chat, creating a leader relay that the ratified job contract forbids.
    The invalid rung must fail upward without reaching the shared outbox.
    """
    seen_surfaces = []
    delivered = []

    def _run(attention, seed, **kw):
        seen_surfaces.append(attention.surfaces)
        if _tier(attention) == "lightweight":
            return _episode(
                [
                    {
                        "channel": "clan_chat",
                        "content": "blackberry reached Valkalla.",
                        "clan_chat": ["blackberry reached Valkalla."],
                        "covers_signal_keys": ["arena_changed:#AAA:54000020"],
                    }
                ],
                job="milestone_batch",
            )
        return _episode(
            [
                {
                    "channel": "elixir",
                    "content": "**Valkalla.** blackberry's climb is backed by a real run.",
                    "covers_signal_keys": ["arena_changed:#AAA:54000020"],
                }
            ],
            job="milestone_batch",
            workflow="wake_response_chat",
        )

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _arena_wake(),
        deliver_fn=lambda read, plan: delivered.append(plan) or {"delivered": 1, "failed": False},
        conn=seeded,
    )

    assert all(chassis.SURFACE_CLAN_CHAT not in surfaces for surfaces in seen_surfaces)
    assert outcome["handled"] is True and outcome["tier"] == "chat"
    assert len(delivered) == 1
    assert delivered[0]["posts"] == [
        {
            "channel": "elixir",
            "content": "**Valkalla.** blackberry's climb is backed by a real run.",
            "covers_signal_keys": ["arena_changed:#AAA:54000020"],
        }
    ]
    assert outcome["episode"]["preceding_attempts"][0]["rejections"] == [
        "missing required surface discord:elixir"
    ]


def test_a_batch_that_selects_one_arena_post_does_not_force_clan_chat(seeded, monkeypatch):
    """Natural R320 came from two arena signals that merely co-arrived.

    The composer selected one worthwhile Discord post. Treating input count as
    a genuine roundup forced five retries until it added a low-value clan-chat
    sibling, which leadership rejected. Keep the surface available for a real
    roundup, but do not require it unless a deterministically eligible milestone
    such as Champion is present.
    """
    seen_surfaces = []
    delivered = []

    def _run(attention, seed, **kw):
        seen_surfaces.append(attention.surfaces)
        return _episode(
            [
                {
                    "channel": "elixir",
                    "content": "**Valkalla.** blackberry's climb is backed by a real run.",
                    "covers_signal_keys": ["arena_changed:#AAA:54000020"],
                }
            ],
            job="milestone_batch",
        )

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _arena_batch_wake(),
        deliver_fn=lambda read, plan: delivered.append(plan) or {"delivered": 1, "failed": False},
        conn=seeded,
    )

    assert seen_surfaces == [frozenset({"discord:elixir", "clan_chat"})]
    assert outcome["handled"] is True and outcome["tier"] == "lightweight"
    assert outcome["attempts"] == 1
    assert delivered[0]["posts"][0].get("clan_chat") is None


def test_a_champion_arrival_without_its_eligible_relay_fails_upward(seeded, monkeypatch):
    """Natural episode 56 contradicted the job contract after posting Discord.

    Champion-tier arrivals retain both surfaces. A composer that omits the
    eligible clan-chat sibling must fail upward instead of redefining the bar
    in its closing prose.
    """
    delivered = []

    def _run(attention, seed, **kw):
        discord_post = {
            "channel": "elixir",
            "content": "**Grand Champion.** blackberry's Ranked grind is paying off.",
            "covers_signal_keys": ["champion_league_reached:#AAA:5"],
        }
        if _tier(attention) == "lightweight":
            return _episode([discord_post], job="milestone_batch")
        return _episode(
            [
                {
                    **discord_post,
                    "clan_chat": ["blackberry reached Grand Champion in Ranked. Great climb!"],
                }
            ],
            job="milestone_batch",
            workflow="wake_response_chat",
        )

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _champion_wake(),
        deliver_fn=lambda read, plan: delivered.append(plan) or {"delivered": 1, "failed": False},
        conn=seeded,
    )

    assert outcome["handled"] is True and outcome["tier"] == "chat"
    assert len(delivered) == 1
    assert delivered[0]["posts"][0]["clan_chat"]
    assert outcome["episode"]["preceding_attempts"][0]["rejections"] == [
        "missing required surface clan_chat"
    ]


def test_a_wake_can_only_fail_upward(seeded, monkeypatch):
    """Haiku first; if it produces nothing usable, Sonnet composes. A wake never
    degrades into silence."""
    tiers = []

    def _run(attention, seed, **kw):
        tiers.append(_tier(attention))
        if _tier(attention) == "lightweight":
            return _episode([])  # nothing usable
        return _episode([_good_post()], workflow="wake_response_chat")

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )
    # The cheap tier gets a second, nudged attempt before the escalation — see
    # test_a_tier_that_ends_without_posting_is_nudged_before_escalating. The
    # direction is what this test pins: never downward, never silence.
    assert tiers == ["lightweight", "lightweight", "chat"]
    assert outcome["handled"] is True and outcome["tier"] == "chat"


def test_a_sonnet_wake_does_not_start_on_haiku(seeded, monkeypatch):
    """The registry's wake_model is the composer tier, not a starting point."""
    tiers = []

    def _run(attention, seed, **kw):
        tiers.append(_tier(attention))
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
        if _tier(attention) == "lightweight":
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


def test_a_tier_that_ends_without_posting_is_nudged_before_escalating(seeded, monkeypatch):
    """The measured escalation signature: no post, no rejection, no error.

    Ten of 41 wakes over the Phase 2 gate ended exactly this way and went
    straight to Sonnet. The model had read its tools and then written prose
    instead of calling the posting tool — the same mistake the validator bounce
    already fixes in-loop, so it gets the same treatment at the same price
    before the stronger model is paid for.
    """
    seen = []

    def _run(attention, seed, **kw):
        seen.append((_tier(attention), kw.get("nudge")))
        if kw.get("nudge"):
            return _episode([_good_post()])
        return _episode([])

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )

    assert outcome["handled"] is True
    assert outcome["tier"] == "lightweight", "the nudge must win on the cheap tier"
    assert [tier for tier, _ in seen] == ["lightweight", "lightweight"]
    assert seen[0][1] is None and seen[1][1], "exactly one nudge, on the retry"


def test_a_soft_arena_wake_is_not_nudged_into_posting_its_no_post_verdict(seeded, monkeypatch):
    """A routine arena climb may be left for the daily brain.

    The responder previously told an empty soft wake to call a posting tool,
    which made its internal "No post — routine arena climb" verdict member
    visible. Recovery nudges are only valid when the event carries a hard-post
    obligation.
    """
    seen = []
    delivered = []

    def _run(attention, seed, **kw):
        seen.append((_tier(attention), kw.get("nudge")))
        if kw.get("nudge"):
            return _episode(
                [
                    {
                        "channel": "elixir",
                        "content": "No post — routine arena climbs.",
                        "covers_signal_keys": ["arena_changed:#AAA:54000020"],
                    }
                ],
                job="milestone_batch",
            )
        return _episode([], job="milestone_batch")

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _arena_wake(),
        deliver_fn=lambda read, plan: delivered.append(plan) or {"delivered": 1},
        conn=seeded,
    )

    assert outcome["handled"] is False
    assert delivered == []
    assert [nudge for _, nudge in seen] == [None, None]


def test_a_tier_that_bounced_is_escalated_rather_than_nudged(seeded, monkeypatch):
    """A turn with rejections was trying. Another round at the same tier spends
    money to repeat a failure the model already could not fix."""
    seen = []

    def _run(attention, seed, **kw):
        seen.append((_tier(attention), kw.get("nudge")))
        if _tier(attention) == "lightweight":
            return _episode([], rejections=["stray quotation mark"])
        return _episode([_good_post()], workflow="wake_response_chat")

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )

    assert outcome["tier"] == "chat"
    assert [tier for tier, _ in seen] == ["lightweight", "chat"]
    assert all(nudge is None for _, nudge in seen)


def test_the_nudge_does_not_stop_a_wake_from_escalating(seeded, monkeypatch):
    """It buys one cheap attempt; it must not become a way to fail quietly."""
    seen = []

    def _run(attention, seed, **kw):
        seen.append(_tier(attention))
        if _tier(attention) == "lightweight":
            return _episode([])
        return _episode([_good_post()], workflow="wake_response_chat")

    monkeypatch.setattr(chassis, "run_turn", _run)
    outcome = respond.respond(
        _wake(), deliver_fn=lambda read, plan: {"delivered": 1, "failed": False}, conn=seeded
    )

    assert outcome["handled"] is True and outcome["tier"] == "chat"
    assert seen == ["lightweight", "lightweight", "chat"]
