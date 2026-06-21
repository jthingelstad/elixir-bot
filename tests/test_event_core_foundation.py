"""Foundation-slice tests for the v5 Event Core.

Unit tests run on a synthetic in-memory event store. The integration test
(exact parity / determinism / idempotency vs the frozen legacy DB) is skipped
automatically when elixir.db.legacy is absent.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from event_core import config


@pytest.fixture()
def world():
    """A fresh ObservedWorld backed by a throwaway sqlite event store."""
    d = tempfile.mkdtemp()
    config.configure_eventstore_env(os.path.join(d, "events.db"))
    from event_core.application import ObservedWorld

    return ObservedWorld()


def test_player_id_is_canonical_and_deterministic():
    from event_core.domain.player import player_id

    assert player_id("c920yglc2") == player_id("#C920YGLC2")
    assert player_id("#ABC") != player_id("#DEF")


def test_observe_profile_dedup_and_fold(world):
    from event_core.domain.player import player_id

    tag = "#TESTER"
    assert world.observe_player_profile(tag, {"trophies": 6000}, "t0", "h0") is True
    assert world.observe_player_profile(tag, {"trophies": 6000}, "t1", "h0") is False  # dedup
    assert world.observe_player_profile(tag, {"trophies": 6100}, "t2", "h1") is True

    p = world.repository.get(player_id(tag))
    assert p.profile["trophies"] == 6100
    assert p.last_observed_at == "t2"


def test_notification_log_orders_events(world):
    world.observe_player_profile("#A", {"trophies": 1}, "t0", "h0")
    world.observe_player_profile("#A", {"trophies": 2}, "t1", "h1")
    notifs = world.recorder.select_notifications(start=1, limit=100)
    topics = [n.topic.rsplit(".", 1)[-1] for n in notifs]
    assert topics == ["Registered", "ProfileObserved", "ProfileObserved"]


def test_projection_reflects_latest(world):
    from event_core import db
    from event_core.projections.player_state import PlayerCurrentProfile

    world.observe_player_profile("#PROJ", {"trophies": 100, "exp_level": 50}, "t0", "h0")
    world.observe_player_profile("#PROJ", {"trophies": 200, "exp_level": 51}, "t1", "h1")

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    proj = PlayerCurrentProfile(world, conn)
    proj.setup()
    proj.run()
    row = conn.execute(
        "SELECT trophies, exp_level FROM player_current_profile WHERE player_tag='#PROJ'"
    ).fetchone()
    assert row["trophies"] == 200
    assert row["exp_level"] == 51


legacy_missing = not os.path.exists(config.LEGACY_DB)


@pytest.mark.skipif(legacy_missing, reason="frozen legacy DB not present")
def test_foundation_parity_determinism_idempotency():
    import hashlib
    import sqlite3

    from event_core import build_foundation
    from event_core.application import ObservedWorld
    from event_core.backfill import backfill_players

    def fingerprint():
        c = sqlite3.connect(config.PROJECTIONS_DB)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM player_current_profile ORDER BY player_tag"
        ).fetchall()
        c.close()
        blob = "\n".join(
            "|".join(str(r[k]) for k in r.keys() if k != "observed_at") for r in rows
        )
        return hashlib.sha256(blob.encode()).hexdigest()

    r1 = build_foundation.build()
    fp1 = fingerprint()
    r2 = build_foundation.build()
    fp2 = fingerprint()

    # exact parity: every reproducible member matches, none mismatched/missing
    parity = r1["parity"]
    assert parity["mismatched"] == 0
    assert parity["missing_projection"] == 0
    assert parity["matched"] == parity["reproducible_members"] > 0

    # replay determinism: two from-zero rebuilds are byte-identical
    assert fp1 == fp2

    # idempotency: re-ingest into the existing store emits nothing
    config.configure_eventstore_env(config.EVENTS_DB)
    app = ObservedWorld()
    before = app.recorder.max_notification_id()
    again = backfill_players(app)
    assert again["events_emitted"] == 0
    assert app.recorder.max_notification_id() == before
