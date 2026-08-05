"""The wake evaluator — Agentic Loop v2, Phase 0.

These pin the decisions, not the prose. The evaluator is the thing that will
eventually decide when Elixir speaks, so the properties that made the join
trigger safe to run every ten minutes have to hold here too: cursor-driven
pending, per-class high-water marks that bound a failing wake to one attempt,
min-lead suppression, and a hard daily budget.

Two of these tests exist because of specific history:

- ``test_subject_column_differs_per_stream`` — the four event tables do NOT
  agree on the subject column (player_events.player_tag vs clan_events
  .subject_tag vs war_events, which has none). A single `SELECT subject_tag`
  across all four raises OperationalError on two of them, and it would have
  raised only in production, where player_events actually has rows.
- ``test_shadow_and_live_marks_are_separate_namespaces`` — a shadow run that
  advanced the live marks would silently arm Phase 1 to skip every event the
  shadow already "fired" for, and those were never actually posted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from runtime.awareness import wake
from storage import telemetry

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(minutes_ago: int = 0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clan_event(conn, event_type, *, tag="#AAA", minutes_ago=0, key=None):
    stamp = _stamp(minutes_ago)
    conn.execute(
        "INSERT INTO clan_events (dedup_key, event_type, clan_tag, subject_tag, observed_at, "
        "timing, payload_json, created_at) VALUES (?, ?, '#J2RGCRVG', ?, ?, 'exact', '{}', ?)",
        (key or f"{event_type}:{tag}:{stamp}", event_type, tag, stamp, stamp),
    )
    conn.commit()


def _player_event(conn, event_type, *, tag="#AAA", minutes_ago=0):
    stamp = _stamp(minutes_ago)
    conn.execute(
        "INSERT INTO player_events (dedup_key, event_type, player_tag, observed_at, "
        "timing, payload_json, created_at) VALUES (?, ?, ?, ?, 'exact', '{}', ?)",
        (f"{event_type}:{tag}:{stamp}", event_type, tag, stamp, stamp),
    )
    conn.commit()


def _war_event(conn, event_type, *, minutes_ago=0):
    stamp = _stamp(minutes_ago)
    conn.execute(
        "INSERT INTO war_events (dedup_key, event_type, season_id, section_index, observed_at, "
        "timing, payload_json, created_at) VALUES (?, ?, 134, 3, ?, 'exact', '{}', ?)",
        (f"{event_type}:134:{stamp}", event_type, stamp, stamp),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("ELIXIR_WAKE_POLICY", "1")
    monkeypatch.setenv("ELIXIR_WAKE_SHADOW", "1")


def test_a_join_is_an_immediate_wake(engine_conn):
    _clan_event(engine_conn, "member_joined")
    decision = wake.evaluate(now=NOW)
    assert len(decision["wakes"]) == 1
    fired = decision["wakes"][0]
    assert fired["wake_class"] == "immediate"
    assert fired["wake_model"] == "lightweight"
    assert [e["event_type"] for e in fired["events"]] == ["member_joined"]


def test_digest_events_never_wake(engine_conn):
    """Most events are texture. card_unlocked must not wake the clan's voice."""
    _player_event(engine_conn, "card_unlocked")
    _player_event(engine_conn, "career_wins_milestone")
    decision = wake.evaluate(now=NOW)
    assert decision["wakes"] == []
    assert decision["pending"] == 2, "still pending for the daily deliberation"


def test_subject_column_differs_per_stream(engine_conn):
    """player_events keys on player_tag, clan_events on subject_tag, war_events
    on neither. One SELECT across all four must not raise."""
    _player_event(engine_conn, "arena_changed")
    _clan_event(engine_conn, "member_joined")
    _war_event(engine_conn, "week_finished")
    events = wake.pending_events(engine_conn)
    by_type = {e["event_type"]: e for e in events}
    assert by_type["arena_changed"]["subject_tag"] == "#AAA"
    assert by_type["member_joined"]["subject_tag"] == "#AAA"
    assert by_type["week_finished"]["subject_tag"] is None


def test_wakes_split_by_job_and_by_tier(engine_conn):
    """One author per wake is the v4 lesson, and the unit of authorship is the
    JOB — a welcome and a farewell are different posts even though both are
    cheap-tier roster events.

    Before Phase 2 these two rode one wake, which was harmless only because
    `welcome` was the single registered job. With a job for each, that wake would
    have mapped to {welcome, farewell}, `job_for` would have refused to pick, and
    the whole thing would have fallen to the daily brain — silently, and exactly
    on the busy ticks that matter most. The tier split still keeps a cheap roster
    wake from being dragged to Sonnet prices by an unrelated war event.
    """
    _clan_event(engine_conn, "member_joined", tag="#AAA")
    _clan_event(engine_conn, "member_left_verified", tag="#BBB")
    _war_event(engine_conn, "week_finished")
    decision = wake.evaluate(now=NOW)

    from runtime.awareness import respond as respond_mod

    by_job = {respond_mod.job_for(w["events"]): w for w in decision["wakes"]}
    assert "welcome" in by_job and "farewell" in by_job, "each job gets its own wake"
    assert [e["event_type"] for e in by_job["welcome"]["events"]] == ["member_joined"]
    assert [e["event_type"] for e in by_job["farewell"]["events"]] == ["member_left_verified"]
    assert by_job["welcome"]["wake_model"] == "lightweight"

    # The war event has no job yet (Phase 3) and still separates on tier.
    chat = [w for w in decision["wakes"] if w["wake_model"] == "chat"]
    assert [e["event_type"] for e in chat[0]["events"]] == ["week_finished"]


def test_a_mixed_milestone_wake_maps_to_one_job(engine_conn):
    """Many event types -> one job is the mechanism behind the milestone batch.

    Four different milestone types share `milestone_batch`, so a wake carrying
    several of them collapses to a single job rather than being refused as
    ambiguous. This is what makes the batch class composable at all.
    """
    from runtime.awareness import respond as respond_mod

    events = [
        {"event_type": "arena_changed"},
        {"event_type": "legendary_badge_earned"},
        {"event_type": "champion_league_reached"},
    ]
    assert respond_mod.job_for(events) == "milestone_batch"

    # The safety property survives: two DIFFERENT jobs in one wake still refuse.
    assert (
        respond_mod.job_for([{"event_type": "member_joined"}, {"event_type": "role_changed"}])
        is None
    )
    # And an unmapped event still disqualifies the whole wake.
    assert (
        respond_mod.job_for([{"event_type": "arena_changed"}, {"event_type": "card_unlocked"}])
        is None
    )


def test_a_batch_class_coalesces_before_firing(engine_conn):
    """Three arena climbs in an hour are one post, not three."""
    _player_event(engine_conn, "arena_changed", tag="#AAA", minutes_ago=5)
    decision = wake.evaluate(now=NOW)
    assert decision["wakes"] == [], "still inside the coalesce window"
    assert decision["held"] and "coalescing" in decision["held"][0]["reason"]

    # A third event fires it early, without waiting out the window.
    _player_event(engine_conn, "arena_changed", tag="#BBB", minutes_ago=4)
    _player_event(engine_conn, "arena_changed", tag="#CCC", minutes_ago=3)
    decision = wake.evaluate(now=NOW)
    assert len(decision["wakes"]) == 1
    assert len(decision["wakes"][0]["events"]) == 3


def test_a_batch_fires_once_the_window_expires(engine_conn):
    _player_event(engine_conn, "arena_changed", minutes_ago=90)
    decision = wake.evaluate(now=NOW)
    assert len(decision["wakes"]) == 1
    assert decision["wakes"][0]["wake_class"] == "batch"


def test_the_badge_split_routes_grind_and_rare_differently(engine_conn):
    """The 2026-08-04 finding: one `badge_earned` type covered two populations.

    Over 20 days the clan earned 102 badges — ~40 Card Mastery grind, 4 one-off
    Legendaries — and the brain posted about the Legendaries only. A single wake
    class had to choose between waking 40 times for mastery or making the rare
    ones wait for the daily brain; splitting the type at the emitter is what
    lets both be right.
    """
    _player_event(engine_conn, "badge_earned", tag="#AAA")
    _player_event(engine_conn, "legendary_badge_earned", tag="#BBB")
    decision = wake.evaluate(now=NOW)

    assert len(decision["wakes"]) == 1, "the Legendary wakes; the mastery grind does not"
    fired = decision["wakes"][0]
    assert fired["wake_class"] == "immediate"
    assert [e["event_type"] for e in fired["events"]] == ["legendary_badge_earned"]
    assert decision["pending"] == 2, "the mastery badge still reaches the daily brain"


def test_reaching_the_top_of_ranked_wakes_but_a_league_bump_does_not(engine_conn):
    """Same split, using event types that already existed."""
    _player_event(engine_conn, "pol_promotion", tag="#AAA")
    _player_event(engine_conn, "ultimate_champion_reached", tag="#BBB")
    decision = wake.evaluate(now=NOW)
    fired_types = [e["event_type"] for w in decision["wakes"] for e in w["events"]]
    assert fired_types == ["ultimate_champion_reached"]


def test_high_water_bounds_a_failed_wake_to_one_attempt(engine_conn):
    """The join-trigger property: a wake whose turn fails must not re-fire every
    ten minutes forever. The daily deliberation is the backstop."""
    _clan_event(engine_conn, "member_joined")
    first = wake.evaluate(now=NOW)
    assert len(first["wakes"]) == 1
    wake.mark_fired(first["wakes"][0]["consumer_key"], first["wakes"][0]["high_water"])

    again = wake.evaluate(now=NOW)
    assert again["wakes"] == [], "already fired through this event"
    assert again["pending"] == 1, "the event is still pending for the daily brain"


def test_shadow_and_live_marks_are_separate_namespaces(engine_conn):
    """A shadow run must not arm Phase 1 to skip events it never posted."""
    _clan_event(engine_conn, "member_joined")
    shadow = wake.evaluate(mode="shadow", now=NOW)
    wake.mark_fired(shadow["wakes"][0]["consumer_key"], shadow["wakes"][0]["high_water"])

    live = wake.evaluate(mode="live", now=NOW)
    assert len(live["wakes"]) == 1, "live marks are untouched by the shadow run"
    assert wake.SHADOW_CURSOR_PREFIX in shadow["wakes"][0]["consumer_key"]
    assert shadow["wakes"][0]["consumer_key"] != live["wakes"][0]["consumer_key"]


def test_an_imminent_deliberation_suppresses_the_wake(engine_conn):
    _clan_event(engine_conn, "member_joined")
    decision = wake.evaluate(now=NOW, next_scheduled_at=NOW + timedelta(minutes=5))
    assert decision["wakes"] == []
    assert "scheduled deliberation" in decision["held"][0]["reason"]
    # ...and a distant one does not.
    decision = wake.evaluate(now=NOW, next_scheduled_at=NOW + timedelta(hours=3))
    assert len(decision["wakes"]) == 1


def test_a_held_wake_is_not_marked_and_re_evaluates(engine_conn):
    """Holding must never consume the event — that would be a silent drop."""
    _clan_event(engine_conn, "member_joined")
    held = wake.evaluate(now=NOW, next_scheduled_at=NOW + timedelta(minutes=5))
    wake.observe(held)
    later = wake.evaluate(now=NOW, next_scheduled_at=NOW + timedelta(hours=3))
    assert len(later["wakes"]) == 1, "a held wake must fire once the hold clears"


def test_the_daily_budget_degrades_to_digest_rather_than_spending(engine_conn, monkeypatch):
    monkeypatch.setenv("ELIXIR_WAKE_DAILY_BUDGET", "1")
    _clan_event(engine_conn, "member_joined")
    _war_event(engine_conn, "week_finished")
    decision = wake.evaluate(now=NOW)
    assert len(decision["wakes"]) == 1, "budget of 1 allows exactly one wake"
    assert decision["held"] and "budget" in decision["held"][0]["reason"]


def test_the_kill_switch_stops_everything(engine_conn, monkeypatch):
    monkeypatch.setenv("ELIXIR_WAKE_POLICY", "0")
    _clan_event(engine_conn, "member_joined")
    decision = wake.evaluate(now=NOW)
    assert decision["wakes"] == [] and decision["held"] == []


def test_observing_records_fires_and_holds_distinctly(engine_conn):
    """A held wake is the observation that tells us a policy is wrong; it is
    invisible if only fires are stored."""
    _clan_event(engine_conn, "member_joined")
    _player_event(engine_conn, "arena_changed", minutes_ago=1)
    decision = wake.evaluate(now=NOW)
    wake.observe(decision)

    rows = (
        telemetry.connect()
        .execute(
            "SELECT mode, wake_class, fired, event_count FROM wake_observations ORDER BY fired DESC"
        )
        .fetchall()
    )
    assert [r["fired"] for r in rows] == [1, 0]
    assert rows[0]["wake_class"] == "immediate" and rows[0]["mode"] == "shadow"
    assert rows[1]["wake_class"] == "batch"


def test_phase_0_composes_nothing(engine_conn, monkeypatch):
    """The whole safety claim of Phase 0. If this ever fails, shadow mode is
    posting."""
    import agent.core as core

    def _boom(*a, **k):
        raise AssertionError("Phase 0 must not call a model")

    monkeypatch.setattr(core, "_get_client", _boom)
    _clan_event(engine_conn, "member_joined")
    _war_event(engine_conn, "season_closed")
    wake.observe(wake.evaluate(now=NOW))


def test_every_hard_post_event_has_a_declared_wake_policy():
    """A hard-post with no wake policy is a coverage guarantee with no timing
    story. The registry must be explicit about both."""
    from engine.event_contracts import EVENT_CONTRACTS, WAKE_CLASSES

    for event_type, contract in EVENT_CONTRACTS.items():
        assert contract.wake in WAKE_CLASSES, event_type
        if contract.hard_post:
            assert contract.wake in ("immediate", "digest"), (
                f"{event_type} is a hard post; batching it delays a guaranteed post"
            )
