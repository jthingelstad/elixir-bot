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


# --- scan pipeline: current roster -> detection -> recommendation + case ---
def test_inactivity_pipeline(world):
    from datetime import datetime, timedelta, timezone

    from event_core import db
    from event_core.domain.decision_case import case_id
    from event_core.domain.recommendation import recommendation_id
    from event_core.mind.leadership import InactivityRiskDetector, LeadershipGenerator

    now = datetime.now(timezone.utc)

    def cr(dt):  # CR-compact last_seen stamp
        return dt.strftime("%Y%m%dT%H%M%S.000Z")

    # The scan reads the current-roster projection directly, NOT roster events — an
    # idle member is the *absence* of events, so seed member_current_state as the live
    # tick would. Current roster = rows sharing the most recent observed_at.
    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    conn.executescript(
        "CREATE TABLE members (member_id INTEGER PRIMARY KEY, player_tag TEXT, current_name TEXT);"
        "CREATE TABLE member_current_state "
        "(member_id INTEGER PRIMARY KEY, observed_at TEXT, last_seen_api TEXT);"
    )
    latest = now.isoformat()
    left_batch = (now - timedelta(days=10)).isoformat()
    conn.executemany(
        "INSERT INTO members VALUES (?,?,?)",
        [(1, "#INA", "Idle Guy"), (2, "#ACTIVE", "Active Guy"), (3, "#LEFT", "Departed Guy")],
    )
    conn.executemany(
        "INSERT INTO member_current_state VALUES (?,?,?)",
        [
            (1, latest, cr(now - timedelta(days=20))),      # idle 20d, current batch -> flagged
            (2, latest, cr(now - timedelta(days=1))),       # active 1d, current batch -> skipped
            (3, left_batch, cr(now - timedelta(days=30))),  # idle but not current (left) -> ignored
        ],
    )
    conn.commit()

    det = InactivityRiskDetector(world, conn)
    assert det.run() == 1  # only #INA — active member and departed member excluded

    gen = LeadershipGenerator(world, conn)
    gen.reset()
    assert gen.run() == 1  # opened a recommendation + case

    # the recommendation + case exist with leadership scope + evidence
    rec = world.repository.get(recommendation_id("kick:#INA"))
    assert rec.recommendation_type == "kick" and rec.scope == "leadership"
    assert rec.reason_codes == ["inactivity"] and rec.caused_by
    case = world.repository.get(case_id("inactivity_review:#INA"))
    assert case.case_type == "inactivity_review" and case.status == "open"

    # idempotent: re-scanning the same (tag, last_seen) emits nothing new
    assert InactivityRiskDetector(world, conn).run() == 0
    gen2 = LeadershipGenerator(world, conn)
    gen2.reset()
    assert gen2.run() == 0
    conn.close()
