"""The game-level stream: detect Clash Royale changes (new cards, events, event
badges) and announce them clan-wide to #announcements — with the card/badge art,
grouped so one real change posts once, badges attributed to the first member.

Covers the seam end to end: card-catalog diff → game_events; sentinel emitter
(Mastery skipped, novel badge attributed, go-live cursor seed); game_recognizer
(group by change_key, idempotent, backfilled never posts); the #announcements
route + deterministic fallbacks; and the image-embed delivery gate.
"""

from __future__ import annotations

import json

import db
from engine import delivery
from engine.db import cursor_set
from engine.emitters.game import emit_game_from_sentinel
from engine.recognition import compose
from engine.recognition import recognizers as R
from storage import game_events as ge
from storage.card_catalog import sync_card_catalog


def _mem():
    conn = db.get_connection(":memory:")
    from engine.legacy_proactive import prepare_queue

    prepare_queue(conn)
    ge.ensure_schema(conn)
    return conn


def _card(cid, name, rarity="common", elixir=3, icon="http://x/c.png"):
    return {
        "id": cid,
        "name": name,
        "rarity": rarity,
        "elixirCost": elixir,
        "maxLevel": 14,
        "iconUrls": {"medium": icon},
    }


def _obs(
    conn,
    sentinel_type,
    name,
    *,
    entity="2G2RPVPP",
    first_seen="2026-07-07T14:00:00",
    sample=None,
):
    conn.execute(
        """INSERT INTO api_sentinel_observations
               (sentinel_type, scope, name, endpoint, entity_key, first_seen_at,
                last_seen_at, sample_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sentinel_type,
            "player.badges" if sentinel_type == "badge_name" else "events",
            name,
            "player",
            entity,
            first_seen,
            first_seen,
            json.dumps(sample or {}),
            first_seen,
            first_seen,
        ),
    )


# ------------------------------------------------------------------ cards


def test_card_sync_detects_new_card_not_bootstrap_and_is_idempotent():
    conn = _mem()
    # bootstrap population emits nothing (a full catalog is the baseline, not news)
    sync_card_catalog({"items": [_card(26000000, "Knight")]}, conn=conn)
    assert ge.new_events_since(conn, 0) == []
    # a genuinely-new card (Ronin) → one card_added with the image
    sync_card_catalog(
        {"items": [_card(26000000, "Knight"), _card(28000018, "Ronin", "legendary")]},
        conn=conn,
    )
    rows = ge.new_events_since(conn, 0)
    assert len(rows) == 1
    p = json.loads(rows[0]["payload_json"])
    assert rows[0]["change_key"] == "card:28000018"
    assert p["name"] == "Ronin" and p["icon_url"] == "http://x/c.png"
    # re-sync the same set → no duplicate event
    sync_card_catalog({"items": [_card(26000000, "Knight"), _card(28000018, "Ronin")]}, conn=conn)
    assert len(ge.new_events_since(conn, 0)) == 1
    conn.close()


# ---------------------------------------------------------------- emitter


def test_emitter_skips_mastery_attributes_novel_badge_and_reads_events():
    conn = _mem()
    # seed a member so attribution resolves to a clean name
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, first_seen_at, "
        "last_seen_at) VALUES ('#2G2RPVPP','Aaqib Javed','2026-03-01','2026-07-07')"
    )
    # baseline observation exists BEFORE go-live → seeded behind the cursor
    _obs(
        conn,
        "badge_name",
        "OldBadge",
        first_seen="2026-06-13T11:46:39",
        sample={"badge": {"name": "OldBadge", "iconUrls": {"large": "http://x/old.png"}}},
    )
    conn.commit()
    # first pass seeds the cursor to the tip and emits nothing (go-live)
    assert emit_game_from_sentinel(conn, "2026-07-07T14:00:00Z") == 0
    # now genuinely-new observations arrive
    _obs(
        conn,
        "badge_name",
        "Chaos_S2",
        first_seen="2026-07-07T15:00:00",
        sample={"badge": {"name": "Chaos_S2", "iconUrls": {"large": "http://x/c.png"}}},
    )
    _obs(conn, "badge_name", "MasteryRonin", first_seen="2026-07-07T15:01:00")
    _obs(
        conn,
        "event",
        "#E9",
        entity="global",
        first_seen="2026-07-07T15:02:00",
        sample={"eventTag": "#E9", "title": "Mega Chaos", "description": "2v2 chaos"},
    )
    conn.commit()
    assert (
        emit_game_from_sentinel(conn, "2026-07-07T15:05:00Z") == 2
    )  # badge + event; Mastery skipped
    kinds = {r["event_type"]: json.loads(r["payload_json"]) for r in ge.new_events_since(conn, 0)}
    assert "MasteryRonin" not in json.dumps(kinds)
    assert kinds["event_badge_earned"]["badge_label"] == "Chaos S2"
    assert kinds["event_badge_earned"]["member_name"] == "Aaqib Javed"
    assert kinds["event_badge_earned"]["image_url"] == "http://x/c.png"
    assert kinds["event_started"]["title"] == "Mega Chaos"
    conn.close()


def test_badge_attributed_to_first_observer_not_latest():
    # The sentinel overwrites entity_key with the LATEST observer on every touch,
    # so attribution must ride first_entity_key (preserved on insert). Real case:
    # Chaos_S2 first seen on Aaqib, later touched by Vijay.
    from storage.api_sentinel import (
        _ensure_first_entity_key,
        _insert_or_touch_observation,
    )

    conn = _mem()
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag,current_name,first_seen_at,"
        "last_seen_at) VALUES ('#AAA','Aaqib','2026-03-01','2026-07-07')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag,current_name,first_seen_at,"
        "last_seen_at) VALUES ('#BBB','Vijay','2026-03-01','2026-07-07')"
    )
    _ensure_first_entity_key(conn)
    # filler so Chaos_S2 lands at observation_id >= 2 (a 0 cursor hits the
    # go-live seed path instead of emitting)
    _insert_or_touch_observation(
        conn,
        {
            "sentinel_type": "badge_name",
            "scope": "player.badges",
            "name": "Filler",
            "endpoint": "player",
            "entity_key": "AAA",
            "sample": {},
        },
        "2026-06-01T00:00:00",
    )
    badge = {"name": "Chaos_S2", "iconUrls": {"large": "http://x/c.png"}}
    base = {
        "sentinel_type": "badge_name",
        "scope": "player.badges",
        "name": "Chaos_S2",
        "endpoint": "player",
        "sample": {"badge": badge},
    }
    _insert_or_touch_observation(conn, {**base, "entity_key": "AAA"}, "2026-07-06T21:30:43")
    _insert_or_touch_observation(conn, {**base, "entity_key": "BBB"}, "2026-07-07T02:00:00")
    row = conn.execute(
        "SELECT observation_id, entity_key, first_entity_key "
        "FROM api_sentinel_observations WHERE name='Chaos_S2'"
    ).fetchone()
    assert row["entity_key"] == "BBB" and row["first_entity_key"] == "AAA"  # drift vs preserved
    conn.commit()
    # emit the (existing) obs by placing the cursor just behind it
    cursor_set(conn, "emit:game", row["observation_id"] - 1)
    emit_game_from_sentinel(conn, "2026-07-07T03:00:00Z")
    ev = [r for r in ge.new_events_since(conn, 0) if r["event_type"] == "event_badge_earned"][0]
    assert json.loads(ev["payload_json"])["member_name"] == "Aaqib"  # first, not latest
    conn.close()


def test_emitter_unattributed_when_member_unresolvable():
    conn = _mem()
    cursor_set(conn, "emit:game", 0)  # allow the seed path to run
    _obs(
        conn,
        "badge_name",
        "GhostBadge",
        entity="ZZZUNKNOWN",
        first_seen="2026-06-01",
        sample={"badge": {"name": "GhostBadge"}},
    )
    conn.commit()
    emit_game_from_sentinel(conn, "2026-07-07T14:00:00Z")  # seed pass
    _obs(
        conn,
        "badge_name",
        "LoneBadge",
        entity="ZZZUNKNOWN",
        first_seen="2026-07-07T15:00:00",
        sample={"badge": {"name": "LoneBadge"}},
    )
    conn.commit()
    emit_game_from_sentinel(conn, "2026-07-07T15:05:00Z")
    row = [r for r in ge.new_events_since(conn, 0) if r["event_type"] == "event_badge_earned"][0]
    assert json.loads(row["payload_json"])["member_name"] is None
    conn.close()


# --------------------------------------------------------------- recognizer


def test_recognizer_groups_by_change_key_and_is_idempotent():
    conn = _mem()
    # two detections sharing ONE change_key must post once
    ge.insert_game_event(
        conn,
        dedup_key="event_started:#E1",
        event_type="event_started",
        change_key="event:#E1",
        observed_at="2026-07-07T00:00:00Z",
        payload={"event_type": "event_started", "title": "Chaos"},
    )
    ge.insert_game_event(
        conn,
        dedup_key="event_started:#E1:mode",
        event_type="event_started",
        change_key="event:#E1",
        observed_at="2026-07-07T00:00:01Z",
        payload={"event_type": "event_started", "title": "Chaos (mode)"},
    )
    conn.commit()
    c = R.game_recognizer(conn, "2026-07-07T00:05:00Z")
    assert c["game_posted"] == 1  # grouped
    intents = conn.execute(
        "SELECT * FROM communication_intents WHERE lane='announcements'"
    ).fetchall()
    assert len(intents) == 1
    # re-run across a later tick → the ledger claim blocks a repeat
    assert R.game_recognizer(conn, "2026-07-07T00:10:00Z")["game_posted"] == 0
    conn.close()


def test_recognizer_never_posts_backfilled_rows():
    conn = _mem()
    ge.insert_game_event(
        conn,
        dedup_key="card_added:1",
        event_type="card_added",
        change_key="card:1",
        observed_at="2026-01-01T00:00:00Z",
        payload={"event_type": "card_added", "name": "OldCard"},
        backfilled=True,
    )
    conn.commit()
    assert R.game_recognizer(conn, "2026-07-07T00:05:00Z")["game_posted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM communication_intents").fetchone()[0] == 0
    conn.close()


# ----------------------------------------------------------- route + fallback


def test_game_intents_route_to_announcements():
    assert compose.route("game:card_added", "public") == "announcements"
    assert compose.route("game:event_badge_earned", "public") == "announcements"


def _render(intent_type, payload):
    return compose.render_intent(
        {
            "intent_type": intent_type,
            "scope": "public",
            "payload_json": json.dumps(payload),
        }
    )


def test_fallbacks_read_cleanly_for_each_game_type():
    card = _render(
        "game:card_added",
        {
            "event_type": "card_added",
            "name": "Ronin",
            "rarity": "legendary",
            "elixir_cost": 3,
        },
    )
    assert "Ronin" in card and "Legendary" in card and "3 elixir" in card
    ev = _render(
        "game:event_started",
        {
            "event_type": "event_started",
            "title": "Mega Chaos",
            "description": "2v2 chaos",
        },
    )
    assert "Mega Chaos" in ev
    badge = _render(
        "game:event_badge_earned",
        {
            "event_type": "event_badge_earned",
            "badge_label": "Chaos S2",
            "member_name": "Aaqib Javed",
        },
    )
    assert "Chaos S2" in badge and "Aaqib Javed" in badge
    anon = _render(
        "game:event_badge_earned",
        {
            "event_type": "event_badge_earned",
            "badge_label": "Chaos S2",
            "member_name": None,
        },
    )
    assert "Chaos S2" in anon and "Aaqib" not in anon


# ------------------------------------------------------------- image delivery


def test_card_intent_delivers_with_image_but_text_lanes_do_not():
    conn = _mem()
    sync_card_catalog({"items": [_card(1, "Knight")]}, conn=conn)  # bootstrap
    sync_card_catalog(
        {
            "items": [
                _card(1, "Knight"),
                _card(2, "Ronin", "legendary", icon="http://x/ronin.png"),
            ]
        },
        conn=conn,
    )
    conn.commit()
    R.game_recognizer(conn, "2026-07-07T00:05:00Z")

    seen = {}

    def send_img(lane, copy, thread_id=None, image_url=None):
        seen["image_url"] = image_url
        return 1

    delivery.consume(conn, send_img, lambda i: None, "2026-07-07T00:06:00Z")
    assert seen["image_url"] == "http://x/ronin.png"
    conn.close()


def test_text_only_send_fn_never_sees_image_kwarg():
    conn = _mem()
    ge.insert_game_event(
        conn,
        dedup_key="card_added:9",
        event_type="card_added",
        change_key="card:9",
        observed_at="2026-07-07T00:00:00Z",
        payload={
            "event_type": "card_added",
            "name": "X",
            "image_url": "http://x/x.png",
        },
    )
    conn.commit()
    R.game_recognizer(conn, "2026-07-07T00:05:00Z")
    calls = {}

    def send_text(lane, copy):  # 2-arg stub (offline/tests) — must still work
        calls["lane"] = lane
        return 1

    out = delivery.consume(conn, send_text, lambda i: None, "2026-07-07T00:06:00Z")
    assert out["delivered"] == 1 and calls["lane"] == "announcements"
    conn.close()


# ------------------------------------------------------ Chaos_S2 end-to-end


def test_chaos_s2_replay_end_to_end():
    # The real motivating case: Aaqib Javed earns the new Chaos_S2 badge.
    conn = _mem()
    conn.execute(
        "INSERT OR IGNORE INTO players (player_tag, current_name, first_seen_at, "
        "last_seen_at) VALUES ('#2G2RPVPP','Aaqib Javed','2026-03-01','2026-07-07')"
    )
    cursor_set(conn, "emit:game", 0)
    _obs(
        conn,
        "badge_name",
        "BaselineBadge",
        first_seen="2026-06-13T11:46:39",
        sample={"badge": {"name": "BaselineBadge"}},
    )
    conn.commit()
    emit_game_from_sentinel(conn, "2026-07-06T00:00:00Z")  # go-live seed → nothing
    _obs(
        conn,
        "badge_name",
        "Chaos_S2",
        first_seen="2026-07-06T21:30:43",
        sample={"badge": {"name": "Chaos_S2", "iconUrls": {"large": "http://x/chaos.png"}}},
    )
    conn.commit()
    emit_game_from_sentinel(conn, "2026-07-06T21:35:00Z")
    R.game_recognizer(conn, "2026-07-06T21:36:00Z")

    captured = {}

    def send_img(lane, copy, thread_id=None, image_url=None):
        captured.update(lane=lane, copy=copy, image_url=image_url)
        return 1

    delivery.consume(conn, send_img, lambda i: None, "2026-07-06T21:37:00Z")
    assert captured["lane"] == "announcements"
    assert captured["image_url"] == "http://x/chaos.png"
    assert "Chaos S2" in captured["copy"] and "Aaqib Javed" in captured["copy"]
    assert "Chaos_S2" not in captured["copy"]  # never the raw key
    conn.close()
