"""Stage 4 live-wiring tests: incremental tick engine, intent consumer, cadence."""
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


def _conn():
    from event_core import db

    return db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))


def test_incremental_tick_detects_and_posts(world):
    """A new observation flows through one tick to a posted intent, incrementally."""
    from event_core.live.engine import advance, apply_payloads
    from event_core.live.tick import run_tick

    conn = _conn()
    # baseline: best_trophies just below a 100 boundary
    apply_payloads(
        world, conn,
        {"player_profiles": [{"tag": "#A", "name": "A", "trophies": 5000, "bestTrophies": 5950}]},
        "2026-06-21T00:00:00Z",
    )
    advance(world, conn)
    base_detections = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]

    # a tick where best_trophies crosses 6000
    posted = []
    poster = lambda intent: (posted.append(intent.dedup_key) or True)  # noqa: E731
    res = run_tick(
        world, conn,
        {"player_profiles": [{"tag": "#A", "name": "A", "trophies": 5000, "bestTrophies": 6050}]},
        "2026-06-22T00:00:00Z", poster,
    )

    after = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    assert after > base_detections  # best_trophies_peak detected incrementally
    assert res["posted"] >= 1 and posted  # intent posted via the consumer

    # idempotent: same observation again -> nothing new ingested/detected/posted
    posted.clear()
    res2 = run_tick(
        world, conn,
        {"player_profiles": [{"tag": "#A", "name": "A", "trophies": 5000, "bestTrophies": 6050}]},
        "2026-06-22T01:00:00Z", poster,
    )
    assert res2["ingested"]["profiles"] == 0
    assert res2["posted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == after
    conn.close()


def test_intent_consumer_posts_and_is_idempotent(world):
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.discord_consumer import IntentConsumer

    world.save(CommunicationIntent(
        dedup_key="i1", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={"detection_type": "best_trophies_peak"},
    ))
    conn = _conn()
    seen = []
    consumer = IntentConsumer(world, conn, poster=lambda i: (seen.append(i.dedup_key) or True))
    consumer.reset()
    assert consumer.run() == 1 and seen == ["i1"]

    from event_core.domain.communication_intent import intent_id
    assert world.repository.get(intent_id("i1")).status == "fulfilled"

    # re-run: already fulfilled -> not re-posted
    seen.clear()
    c2 = IntentConsumer(world, conn, poster=lambda i: (seen.append(i.dedup_key) or True))
    c2.reset()
    assert c2.run() == 0 and seen == []
    conn.close()


def test_intent_consumer_drops_on_poster_failure(world):
    from event_core.domain.communication_intent import CommunicationIntent, intent_id
    from event_core.live.discord_consumer import IntentConsumer

    world.save(CommunicationIntent(
        dedup_key="i2", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={},
    ))
    conn = _conn()
    consumer = IntentConsumer(world, conn, poster=lambda i: False)
    consumer.reset()
    consumer.run()
    assert consumer.dropped == 1
    assert world.repository.get(intent_id("i2")).status == "dropped"
    conn.close()


def test_cadence_reflection():
    from event_core.live.cadence import clan_activity_24h

    conn = _conn()
    conn.execute("CREATE TABLE battle_telemetry (player_tag TEXT, battle_time TEXT)")
    conn.executemany("INSERT INTO battle_telemetry VALUES(?,?)", [
        ("#A", "20260621T120000.000Z"), ("#A", "20260620T120000.000Z"), ("#B", "20260621T130000.000Z"),
    ])
    conn.execute("CREATE TABLE detections (detection_type TEXT, occurred_at TEXT)")
    conn.executemany("INSERT INTO detections VALUES(?,?)", [
        ("battle_hot_streak", "20260621T120000.000Z"), ("best_trophies_peak", "20260621T120000.000Z"),
    ])
    conn.commit()

    out = clan_activity_24h(conn, "20260621T000000.000Z")
    assert out["battles"] == 2 and out["active_players"] == 2
    assert out["detections"]["battle_hot_streak"] == 1
    conn.close()
