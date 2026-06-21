"""Phase 3 reactive-layer tests: communication policy + agent read tools."""
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


def test_intent_lifecycle_invariant():
    from event_core.domain.communication_intent import CommunicationIntent, InvalidTransition

    ci = CommunicationIntent(
        dedup_key="i1", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={},
    )
    ci.drop("not_noteworthy")
    assert ci.status == "dropped"
    with pytest.raises(InvalidTransition):
        ci.fulfil()


def test_policy_emits_scoped_intents_idempotently(world):
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.domain.recommendation import Recommendation
    from event_core.mind.communication import CommunicationPolicy

    world.save(Detection(
        dedup_key="best_trophies_peak:#A:6000", detection_type="best_trophies_peak",
        detector="t", subject_tag="#A", occurred_at="2026-06-21T00:00:00Z",
        caused_by=["e1"], payload={"peak": 6000},
    ))
    world.save(Recommendation(
        dedup_key="kick:#B", recommendation_type="kick", player_tag="#B",
        reason_codes=["inactivity"], policy_version="v", severity="medium", caused_by=["e2"],
    ))

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 2  # one public (detection) + one leadership (recommendation)

    pub = world.repository.get(intent_id("intent:detection:best_trophies_peak:#A:6000"))
    assert pub.scope == "public" and pub.subject_tag == "#A"
    lead = world.repository.get(intent_id("intent:recommendation:kick:#B"))
    assert lead.scope == "leadership"

    pol2 = CommunicationPolicy(world, conn)
    pol2.reset()
    assert pol2.run() == 0  # idempotent
    conn.close()


def test_policy_maps_restored_coverage_detection_types(world):
    """v5 restored-coverage detections get the right intent_type prefix (which
    route_intent uses to pick the channel); non-public detections are filtered."""
    from event_core import db
    from event_core.domain.communication_intent import intent_id
    from event_core.domain.detection import Detection
    from event_core.mind.communication import CommunicationPolicy

    cases = {
        "member_joined:#J:t0": ("member_joined", "welcome"),
        "member_left:#J:t0": ("member_left", "clan"),
        "member_promoted:#J:elder:t0": ("member_promoted", "clan"),
        "war_update:#CLAN:5:warDay": ("war_update", "war"),
        "cohort_wave:badge_earned:2026-06-21": ("cohort_wave", "cohort"),
    }
    for dedup, (dtype, _prefix) in cases.items():
        world.save(Detection(
            dedup_key=dedup, detection_type=dtype, detector="t", subject_tag="#J",
            occurred_at="2026-06-21T00:00:00Z", caused_by=["e"], payload={},
        ))
    # a detection that should NOT post (drives recommendations, not Discord)
    world.save(Detection(
        dedup_key="inactive_member_risk:#Z", detection_type="inactive_member_risk",
        detector="t", subject_tag="#Z", occurred_at="2026-06-21T00:00:00Z",
        caused_by=["e"], payload={},
    ))

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    pol = CommunicationPolicy(world, conn)
    pol.reset()
    assert pol.run() == 5  # only the public restored-coverage detections

    for dedup, (_dtype, prefix) in cases.items():
        intent = world.repository.get(intent_id(f"intent:detection:{dedup}"))
        assert intent.intent_type.split(":", 1)[0] == prefix
    conn.close()


def test_agent_tools_resolve_evidence_and_scope():
    from event_core import db
    from event_core.read import tools

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    conn.execute(
        "CREATE TABLE battle_telemetry (player_tag TEXT, battle_time TEXT, battle_type TEXT, "
        "mode_group TEXT, outcome TEXT, crowns_for INT, crowns_against INT, opponent_tag TEXT, "
        "trophy_change INT, is_competitive INT)"
    )
    conn.executemany(
        "INSERT INTO battle_telemetry VALUES(?,?,?,?,?,?,?,?,?,1)",
        [
            ("#A", "20260621T100000.000Z", "PvP", "ladder", "W", 3, 1, "#OPP1", 30),
            ("#A", "20260621T110000.000Z", "PvP", "ladder", "W", 2, 0, "#OPP2", 30),
        ],
    )
    conn.execute(
        "CREATE TABLE detections (dedup_key TEXT PRIMARY KEY, detection_type TEXT, detector TEXT, "
        "subject_tag TEXT, occurred_at TEXT, scope TEXT, payload_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO detections VALUES(?,?,?,?,?,?,?)",
        [
            ("d1", "battle_hot_streak", "x", "#A", "20260621T120000.000Z", "public", "{}"),
            ("d2", "inactive_member_risk", "x", "#A", "20260621T120000.000Z", "leadership", "{}"),
        ],
    )
    conn.commit()

    ev = tools.resolve_evidence(conn, {"subject_tag": "#A", "occurred_at": "20260621T120000.000Z"})
    assert len(ev) == 2 and ev[0]["opponent_tag"] in ("#OPP1", "#OPP2")

    # scope gating: public caller does not see the leadership detection
    assert len(tools.get_player_detections(conn, "#A", scope="public")) == 1
    assert len(tools.get_player_detections(conn, "#A", scope="leadership")) == 2
    conn.close()
