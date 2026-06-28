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
    # baseline: career wins just below a durable 1,000-win boundary
    apply_payloads(
        world, conn,
        {"player_profiles": [{"tag": "#A", "name": "A", "trophies": 5000, "bestTrophies": 5950, "wins": 990}]},
        "2026-06-21T00:00:00Z",
    )
    advance(world, conn)
    base_detections = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]

    # a tick where career wins crosses 1000
    posted = []
    poster = lambda intent: (posted.append(intent.dedup_key) or True)  # noqa: E731
    res = run_tick(
        world, conn,
        {"player_profiles": [{"tag": "#A", "name": "A", "trophies": 5000, "bestTrophies": 5950, "wins": 1002}]},
        "2026-06-22T00:00:00Z", poster,
    )

    after = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    assert after > base_detections  # career_wins_milestone detected incrementally
    assert res["posted"] >= 1 and posted  # intent posted via the consumer

    # idempotent: same observation again -> nothing new ingested/detected/posted
    posted.clear()
    res2 = run_tick(
        world, conn,
        {"player_profiles": [{"tag": "#A", "name": "A", "trophies": 5000, "bestTrophies": 5950, "wins": 1002}]},
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


def test_consumer_fast_forward_drains_backlog(world):
    """Cutover safety: fast_forward skips the historical intent backlog (Stage 5
    finding) so go-live doesn't flood Discord."""
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.discord_consumer import IntentConsumer

    for i in range(3):
        world.save(CommunicationIntent(
            dedup_key=f"b{i}", intent_type="celebrate:x", subject_tag="#A", scope="public",
            priority=1, caused_by=[], summary={},
        ))
    conn = _conn()
    consumer = IntentConsumer(world, conn, poster=lambda i: True)
    consumer.reset()
    consumer.fast_forward()  # drain backlog without posting
    assert consumer.run() == 0  # backlog skipped
    conn.close()


def test_intent_consumer_leaves_raised_on_failure_and_retries(world):
    """At-least-once: a failed post leaves the intent raised (not dropped) and the
    consumer stops without advancing, so a later tick retries and delivers it."""
    from event_core.domain.communication_intent import CommunicationIntent, intent_id
    from event_core.live.discord_consumer import IntentConsumer

    world.save(CommunicationIntent(
        dedup_key="i2", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={},
    ))
    conn = _conn()
    # poster fails this tick
    c1 = IntentConsumer(world, conn, poster=lambda i: False)
    c1.reset()
    assert c1.run() == 0 and c1.failed == 1
    assert world.repository.get(intent_id("i2")).status == "raised"  # NOT dropped

    # next tick, poster works -> the same intent is retried and delivered
    seen = []
    c2 = IntentConsumer(world, conn, poster=lambda i: (seen.append(i.dedup_key) or True))
    assert c2.run() == 1 and seen == ["i2"]
    assert world.repository.get(intent_id("i2")).status == "fulfilled"
    conn.close()


def test_render_intent_and_dry_run_poster(world):
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.discord import DryRunPoster, render_intent

    ci = CommunicationIntent(
        dedup_key="x", intent_type="celebrate:best_trophies_peak", subject_tag="#A",
        scope="public", priority=1, caused_by=[],
        summary={"detection_type": "best_trophies_peak", "peak": 6000},
    )
    text = render_intent(ci)
    assert "6000" in text and "#A" not in text

    wins = CommunicationIntent(
        dedup_key="w", intent_type="celebrate:career_wins_milestone", subject_tag="#A",
        scope="public", priority=1, caused_by=[],
        summary={"detection_type": "career_wins_milestone", "milestone": 1000},
    )
    assert "1000 career wins" in render_intent(wins)
    collection = CommunicationIntent(
        dedup_key="c", intent_type="celebrate:collection_level_milestone", subject_tag="#A",
        scope="public", priority=1, caused_by=[],
        summary={"detection_type": "collection_level_milestone", "milestone": 1700},
    )
    assert "collection level 1700" in render_intent(collection)

    poster = DryRunPoster()
    assert poster(ci) is True
    assert poster.posts == [("public", text)]


def test_render_intent_fallback_never_leaks_raw_public_payloads():
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.discord import render_intent

    cohort = CommunicationIntent(
        dedup_key="intent:detection:cohort_wave:badge_earned:2026-06-28",
        intent_type="cohort:cohort_wave",
        subject_tag="",
        scope="public",
        priority=1,
        caused_by=["cohort:badge_earned:2026-06-28"],
        summary={
            "detection_type": "cohort_wave",
            "wave_type": "badge_earned",
            "day": "2026-06-28",
            "member_count": 3,
            "occurred_at": "2026-06-28T12:00:00Z",
        },
    )
    cohort_text = render_intent(cohort)
    assert "3 POAP KINGS members" in cohort_text
    assert "[public]" not in cohort_text
    assert "cohort:cohort_wave" not in cohort_text
    assert "{'" not in cohort_text

    unlocked = CommunicationIntent(
        dedup_key="intent:detection:new_card_unlocked:#20G9RY299P:26000077",
        intent_type="celebrate:new_card_unlocked",
        subject_tag="#20G9RY299P",
        scope="public",
        priority=1,
        caused_by=["new_card_unlocked:#20G9RY299P:26000077"],
        summary={
            "detection_type": "new_card_unlocked",
            "card_name": "Monk",
            "rarity": "champion",
        },
    )
    unlocked_text = render_intent(unlocked)
    assert "Monk" in unlocked_text
    assert "#20G9RY299P" not in unlocked_text
    assert "{'" not in unlocked_text


def test_agent_poster_passes_delivery_metadata(monkeypatch):
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live import runtime

    monkeypatch.setattr(runtime, "compose_copy", lambda intent: "TDuck hit a new peak.")
    seen = []

    def send(channel_id, text, scope, metadata):
        seen.append((channel_id, text, scope, metadata))
        return True

    ci = CommunicationIntent(
        dedup_key="intent:detection:best_trophies_peak:#A:6000",
        intent_type="celebrate:best_trophies_peak",
        subject_tag="#A",
        scope="public",
        priority=2,
        caused_by=["best_trophies_peak:#A:6000"],
        summary={"detection_type": "best_trophies_peak", "peak": 6000},
    )

    assert runtime.make_agent_poster(send)(ci) is True

    channel_id, text, scope, metadata = seen[0]
    assert channel_id == runtime.PUBLIC_HIGHLIGHTS["channel_id"]
    assert text == "TDuck hit a new peak."
    assert scope == "public"
    assert metadata["event_core_intent_id"] == str(ci.id)
    assert metadata["event_core_dedup_key"] == "intent:detection:best_trophies_peak:#A:6000"
    assert metadata["intent_type"] == "celebrate:best_trophies_peak"
    assert metadata["target_channel_key"] == "player-highlights"
    assert metadata["source_signal_key"] == "best_trophies_peak:#A:6000"
    assert metadata["source_signal_type"] == "best_trophies_peak"


def test_agent_poster_leaves_leadership_intents_for_action_card_scan(monkeypatch):
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live import runtime

    monkeypatch.setattr(
        runtime,
        "compose_copy",
        lambda intent: pytest.fail("leadership intents should not become plain text"),
    )
    seen = []

    def send(channel_id, text, scope, metadata):
        seen.append((channel_id, text, scope, metadata))
        return True

    ci = CommunicationIntent(
        dedup_key="intent:recommendation:kick:#B",
        intent_type="leadership:kick",
        subject_tag="#B",
        scope="leadership",
        priority=2,
        caused_by=["recommendation:kick:#B"],
        summary={"recommendation_type": "kick", "reason_codes": ["inactive"]},
    )

    assert runtime.make_agent_poster(send)(ci) is True
    assert seen == []


def test_route_intent_and_go_live_drain(world):
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live.discord_consumer import IntentConsumer
    from event_core.live.runtime import go_live_drain, route_intent

    pub = CommunicationIntent(
        dedup_key="p", intent_type="celebrate:best_trophies_peak", subject_tag="#A",
        scope="public", priority=1, caused_by=[], summary={},
    )
    lead = CommunicationIntent(
        dedup_key="l", intent_type="leadership:kick", subject_tag="#B",
        scope="leadership", priority=2, caused_by=[], summary={},
    )
    assert route_intent(pub)["channel_name"] == "player-highlights"
    assert route_intent(lead)["channel_name"] == "leader-actions"

    # v5 restored-coverage prefixes route to their channels.
    def _intent(dedup, itype, scope="public"):
        return CommunicationIntent(
            dedup_key=dedup, intent_type=itype, subject_tag="#C",
            scope=scope, priority=1, caused_by=[], summary={},
        )

    assert route_intent(_intent("w", "clan:member_joined"))["channel_name"] == "clan-events"
    assert route_intent(_intent("r", "war:war_update"))["channel_name"] == "river-race"
    assert route_intent(_intent("c", "cohort:cohort_wave"))["channel_name"] == "clan-events"
    assert route_intent(_intent("lv", "clan:member_left"))["channel_name"] == "clan-events"
    assert route_intent(_intent("pr", "clan:member_promoted"))["channel_name"] == "clan-events"
    # fail-closed: unknown prefix routes to the private leadership channel
    assert route_intent(_intent("u", "mystery:thing"))["channel_name"] == "leader-actions"
    # leadership scope always wins, even with a public-looking prefix
    assert route_intent(_intent("x", "celebrate:foo", scope="leadership"))["channel_name"] == "leader-actions"

    world.save(pub)
    world.save(lead)
    conn = _conn()
    assert go_live_drain(world, conn) >= 1  # drained to head, posted nothing
    # downtime backlog is not re-posted
    assert IntentConsumer(world, conn, poster=lambda i: True).run() == 0
    conn.close()


def test_battle_telemetry_dedups_null_identity():
    """Boat/PvE battles with no opponent tag/crowns must still dedup (NULL-in-PK
    would otherwise re-insert every poll)."""
    from event_core import db
    from event_core.ingest.battles import write_battle_telemetry

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    boat = [{"battleTime": "20260621T120000.000Z", "type": "boatBattle",
             "team": [{"tag": "#A"}], "opponent": [{}], "gameMode": {"id": 1}}]
    assert write_battle_telemetry(conn, "#A", boat, "t0") == 1
    assert write_battle_telemetry(conn, "#A", boat, "t1") == 0  # deduped, not re-inserted
    assert conn.execute("SELECT COUNT(*) FROM battle_telemetry").fetchone()[0] == 1
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
        ("battle_trophy_push", "20260621T120000.000Z"), ("best_trophies_peak", "20260621T120000.000Z"),
    ])
    conn.commit()

    out = clan_activity_24h(conn, "20260621T000000.000Z")
    assert out["battles"] == 2 and out["active_players"] == 2
    assert out["detections"]["battle_trophy_push"] == 1
    conn.close()


def test_consumer_drops_stale_backlog_instead_of_posting(world):
    """F1: a long-outage backlog is bounded — intents older than MAX_INTENT_AGE_HOURS
    are dropped (auditably) rather than posted, while fresh ones still post."""
    from event_core.domain.communication_intent import CommunicationIntent, intent_id
    from event_core.live.discord_consumer import IntentConsumer

    world.save(CommunicationIntent(
        dedup_key="stale1", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=["e"], summary={},
    ))
    world.save(CommunicationIntent(
        dedup_key="fresh1", intent_type="celebrate:x", subject_tag="#B", scope="public",
        priority=1, caused_by=["e"], summary={},
    ))
    conn = _conn()
    seen = []
    consumer = IntentConsumer(world, conn, poster=lambda i: (seen.append(i.dedup_key) or True))
    consumer.reset()
    # mark only the first intent stale
    consumer._is_stale = lambda intent: intent.dedup_key == "stale1"
    consumer.run()

    assert seen == ["fresh1"]  # stale not posted, fresh posted
    assert consumer.dropped == 1 and consumer.posted == 1
    assert world.repository.get(intent_id("stale1")).status == "dropped"
    assert world.repository.get(intent_id("fresh1")).status == "fulfilled"
    conn.close()


def test_catch_up_drains_once_then_skips_on_restart(monkeypatch, world):
    """F1: catch_up drains+marks at first go-live, then is a no-op on restart so it
    can't silently fast-forward past unposted events."""
    from event_core import db as _db
    from event_core.live import service

    # catch_up opens AND closes its own conn each call, so hand it a fresh conn to
    # the same temp file (the marker persists in the file across calls). It imports
    # config/db/ObservedWorld inside the function, so patch the source modules.
    path = os.path.join(tempfile.mkdtemp(), "proj.db")
    real_connect = _db.connect
    monkeypatch.setattr("event_core.config.configure_eventstore_env", lambda *a, **k: None)
    monkeypatch.setattr("event_core.application.ObservedWorld", lambda: world)
    monkeypatch.setattr("event_core.db.connect", lambda *a, **k: real_connect(path))
    monkeypatch.setattr("event_core.live.tick.fetch_payloads", lambda: {})
    monkeypatch.setattr("event_core.live.engine.apply_payloads", lambda *a, **k: {})
    monkeypatch.setattr("event_core.live.engine.advance", lambda *a, **k: {})
    drains = []
    monkeypatch.setattr("event_core.live.runtime.go_live_drain", lambda *a, **k: drains.append(1) or 7)

    first = service.catch_up()
    assert "drained_to_position" in first and len(drains) == 1  # drained once
    vconn = real_connect(path)
    assert service._cutover_done(vconn)  # marker set
    vconn.close()

    second = service.catch_up()
    assert "skipped" in second and len(drains) == 1  # NOT drained again on restart

    forced = service.catch_up(force=True)
    assert "drained_to_position" in forced and len(drains) == 2  # force overrides


def test_catch_up_seeds_missing_post_cutover_followers():
    from event_core import db
    from event_core.live import service

    conn = db.connect(os.path.join(tempfile.mkdtemp(), "proj.db"))
    try:
        seeded = service._fast_forward_missing_post_cutover_followers(conn, 123)
        assert seeded == ["detector:collection_level_milestone"]
        row = conn.execute(
            "SELECT last_global_position FROM projection_tracking WHERE projection_name=?",
            ("detector:collection_level_milestone",),
        ).fetchone()
        assert row["last_global_position"] == 123

        assert service._fast_forward_missing_post_cutover_followers(conn, 456) == []
        row = conn.execute(
            "SELECT last_global_position FROM projection_tracking WHERE projection_name=?",
            ("detector:collection_level_milestone",),
        ).fetchone()
        assert row["last_global_position"] == 123
    finally:
        conn.close()


def test_health_splits_deliverable_pending_from_drained(world, monkeypatch):
    """F4: health reports deliverable backlog (Raised after the consumer cursor) vs
    drained-historical, and excludes scan-style detectors from follower lag."""
    from event_core.domain.communication_intent import CommunicationIntent
    from event_core.live import health

    # two intents: the consumer will be parked between them
    world.save(CommunicationIntent(
        dedup_key="old", intent_type="celebrate:x", subject_tag="#A", scope="public",
        priority=1, caused_by=[], summary={},
    ))
    world.save(CommunicationIntent(
        dedup_key="new", intent_type="celebrate:x", subject_tag="#B", scope="public",
        priority=1, caused_by=[], summary={},
    ))
    conn = _conn()  # temp proj.db with projection_tracking
    # consumer parked at position 1: 'old' (id 1) is behind it (drained), 'new' (id 2) ahead (deliverable)
    conn.execute("INSERT OR REPLACE INTO projection_tracking VALUES ('consumer:discord', 1, 't')")
    # a stale scan-detector row that must NOT count as follower lag
    conn.execute("INSERT OR REPLACE INTO projection_tracking VALUES ('detector:war_update', 0, 't')")
    conn.execute("INSERT OR REPLACE INTO projection_tracking VALUES ('detections_proj', 2, 't')")
    conn.commit()
    # health_snapshot imports db/ObservedWorld inside the function — patch sources.
    monkeypatch.setattr("event_core.config.configure_eventstore_env", lambda *a, **k: None)
    monkeypatch.setattr("event_core.application.ObservedWorld", lambda: world)
    monkeypatch.setattr("event_core.db.connect", lambda *a, **k: conn)

    snap = health.health_snapshot()
    assert snap["intents"]["deliverable_pending"] == 1  # only 'new' (ahead of cursor)
    assert snap["intents"]["drained_historical"] == 1   # 'old' (behind cursor, unfulfilled)
    assert "detector:war_update" not in snap["follower_lag"]  # scan detector excluded
    assert "detector:war_update" in snap["scan_detectors"]
    conn.close()
