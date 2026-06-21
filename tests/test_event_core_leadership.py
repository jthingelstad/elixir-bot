"""Leadership decision-layer tests: aggregate invariants + the generator pipeline.

Unit tests on synthetic temp stores (no shared DB), safe to run concurrently.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from event_core import config


@pytest.fixture()
def world():
    d = tempfile.mkdtemp()
    config.configure_eventstore_env(os.path.join(d, "events.db"))
    from event_core.application import ObservedWorld

    return ObservedWorld()


# --- aggregate lifecycle invariants ---
def test_recommendation_terminal_blocks_refresh():
    from event_core.domain.recommendation import InvalidTransition, Recommendation

    r = Recommendation(
        dedup_key="kick:#A", recommendation_type="kick", player_tag="#A",
        reason_codes=["inactivity"], policy_version="v", severity="medium", caused_by=[],
    )
    r.refresh(["inactivity", "low_war"], [])
    assert r.status == "refreshed"
    r.suppress("below_threshold")
    assert r.status == "suppressed"
    with pytest.raises(InvalidTransition):
        r.refresh([], [])


def test_decision_case_terminal_blocks_transitions():
    from event_core.domain.decision_case import DecisionCase, InvalidTransition

    c = DecisionCase(
        dedup_key="inactivity_review:#A", case_type="inactivity_review",
        player_tag="#A", priority=1, due_at=None, caused_by=[],
    )
    c.defer("2026-07-01T00:00:00Z")
    assert c.status == "deferred"
    c.resolve("kicked")
    assert c.status == "resolved" and c.resolution == "kicked"
    with pytest.raises(InvalidTransition):
        c.accept()


def test_days_inactive():
    from event_core.mind.leadership import days_inactive

    d = days_inactive("20260601T000000.000Z", "2026-06-21T00:00:00Z")
    assert 19.9 < d < 20.1
    assert days_inactive(None, "2026-06-21T00:00:00Z") is None


# --- generator pipeline: roster -> detection -> recommendation + case ---
def test_inactivity_pipeline(world):
    from event_core import db
    from event_core.domain.decision_case import case_id
    from event_core.domain.recommendation import recommendation_id
    from event_core.mind.leadership import InactivityRiskDetector, LeadershipGenerator

    # an obviously inactive member (last seen 20 days before observation)
    world.observe_member_roster(
        "#INA", {"last_seen_api": "20260601T000000.000Z", "role": "member", "trophies": 5000},
        "2026-06-21T00:00:00Z", "h0",
    )
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))

    det = InactivityRiskDetector(world, conn)
    det.reset()
    assert det.run() == 1  # one inactive_member_risk detection

    gen = LeadershipGenerator(world, conn)
    gen.reset()
    assert gen.run() == 1  # opened a recommendation + case

    # the recommendation + case exist with leadership scope + evidence
    rec = world.repository.get(recommendation_id("kick:#INA"))
    assert rec.recommendation_type == "kick" and rec.scope == "leadership"
    assert rec.reason_codes == ["inactivity"] and rec.caused_by
    case = world.repository.get(case_id("inactivity_review:#INA"))
    assert case.case_type == "inactivity_review" and case.status == "open"

    # idempotent: re-running emits nothing new
    gen2 = LeadershipGenerator(world, conn)
    gen2.reset()
    assert gen2.run() == 0
    conn.close()
