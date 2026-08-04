"""The game-level stream: detect Clash Royale changes (new cards, events, event
badges) into `game_events`.

Covers the card-catalog diff and the sentinel emitter, which are live. The
recognizer/delivery half of this file was removed with the deterministic
proactive stack (#207) — announcing these changes is the awareness loop's job
now.

Covers: card-catalog diff → game_events, and the sentinel emitter (Mastery
skipped, novel badge attributed, go-live cursor seed).
"""

from __future__ import annotations

import json

import db
from engine.db import cursor_set
from engine.emitters.game import emit_game_from_sentinel
from storage import game_events as ge
from storage.card_catalog import sync_card_catalog


def _mem():
    conn = db.get_connection(":memory:")
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
                sample_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sentinel_type,
            "player.badges" if sentinel_type == "badge_name" else "events",
            name,
            "player",
            entity,
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
    # Attribution rides first_entity_key, set once on insert. Real case: Chaos_S2
    # first seen on Aaqib, later re-observed on Vijay.
    #
    # Before 2026-08-04 the sentinel touched every row on every sighting and
    # entity_key drifted to the latest observer, which is why attribution had to
    # use first_entity_key. The sentinel now writes on novelty only, so the
    # second sighting is a no-op and nothing drifts — but first_entity_key is
    # still what the emitter reads, and this pins that.
    from storage.api_sentinel import (
        _ensure_first_entity_key,
        _insert_observation_if_new,
        reset_known_keys,
    )

    reset_known_keys()  # module-level novelty cache must not leak between tests

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
    _insert_observation_if_new(
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
    _insert_observation_if_new(conn, {**base, "entity_key": "AAA"}, "2026-07-06T21:30:43")
    _insert_observation_if_new(conn, {**base, "entity_key": "BBB"}, "2026-07-07T02:00:00")
    row = conn.execute(
        "SELECT observation_id, entity_key, first_entity_key "
        "FROM api_sentinel_observations WHERE name='Chaos_S2'"
    ).fetchone()
    # Both are AAA now: novelty-only writes mean the second sighting never
    # touched the row, so entity_key cannot drift. first_entity_key is still the
    # column the emitter attributes from, which is what matters here.
    assert row["entity_key"] == "AAA" and row["first_entity_key"] == "AAA"
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
