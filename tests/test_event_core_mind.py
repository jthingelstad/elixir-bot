"""Mind-layer tests: granular events, Detection aggregate, Followers."""
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


def test_milestones_helper():
    from event_core.mind.detectors import _milestones

    assert _milestones(9, 12, 5) == [10]
    assert _milestones(8, 23, 5) == [10, 15, 20]
    assert _milestones(10, 10, 5) == []
    assert _milestones(None, 12, 5) == []


def test_granular_level_change_emitted_after_baseline(world):
    from event_core.domain.player import player_id

    # baseline observation: no granular events
    world.observe_player_profile("#LVL", {"exp_level": 9, "name": "x"}, "t0", "h0")
    # level jump 9 -> 12 should emit PlayerLevelChanged
    world.observe_player_profile("#LVL", {"exp_level": 12, "name": "x"}, "t1", "h1")

    p = world.repository.get(player_id("#LVL"))
    assert p.profile["exp_level"] == 12
    topics = [
        n.topic.rsplit(".", 1)[-1]
        for n in world.recorder.select_notifications(start=1, limit=100)
    ]
    assert "PlayerLevelChanged" in topics


def test_detector_emits_and_is_idempotent(world):
    from event_core import db
    from event_core.mind.detectors import PlayerLevelUpDetector

    world.observe_player_profile("#D", {"exp_level": 9}, "t0", "h0")
    world.observe_player_profile("#D", {"exp_level": 12}, "2026-06-21T00:00:00Z", "h1")

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    det = PlayerLevelUpDetector(world, conn)
    det.reset()
    emitted = det.run()
    assert emitted == 1  # crossed level 10

    # idempotent: a fresh detector resuming sees nothing new; even a full reset
    # re-run emits 0 because the Detection id is deterministic (get-or-create)
    det2 = PlayerLevelUpDetector(world, conn)
    det2.reset()
    assert det2.run() == 0
    conn.close()


def test_detection_id_deterministic():
    from event_core.domain.detection import detection_id

    assert detection_id("player_level_up:#A:10") == detection_id("player_level_up:#A:10")
    assert detection_id("a") != detection_id("b")


legacy_missing = not os.path.exists(config.LEGACY_DB)


@pytest.mark.skipif(legacy_missing, reason="frozen legacy DB not present")
def test_mind_build_against_legacy():
    from event_core.mind.build import build_and_validate

    res = build_and_validate()
    # best_trophies detector fires in the archive window and overlaps legacy dates
    bt = res["validation"]["by_type"]["best_trophies_peak"]
    assert res["detector_emitted"]["detector:best_trophies_peak"] > 0
    assert bt["overlap"] > 0
