"""Guard for the collection_level_milestone signal (CR 2026 progression).

This signal replaces the deprecated exp_level `level_up` celebration: Collection
Level is the live account-progression metric, and we celebrate every 100 levels.
It had ZERO production events for its first two days (Collection Level climbs
slowly, so no member crossed a x100 boundary), so these golden-pair tests exist
to prove the emitter actually fires — the seam is otherwise only exercised in
production. Spec: events.md §3, memory cr-progression-model-2026.
"""

from __future__ import annotations

from engine.emitters import emit
from engine.emitters.player import project_player_aspects

NOW = "2026-07-01T12:00:00Z"
LATER = "2026-07-01T13:00:00Z"
LATEST = "2026-07-01T14:00:00Z"
TAG = "#CL111"


def _cards_payload(collection_level: int, knight_level: int = 14):
    """Minimal player payload; Collection Level rides the CollectionLevel badge
    progress (the four-digit CR 2026 metric), which projects into the `cards`
    aspect where emit_cards reads it."""
    return {
        "tag": TAG,
        "name": "Coll",
        "cards": [
            {
                "id": 1,
                "name": "Knight",
                "rarity": "common",
                "level": knight_level,
                "maxLevel": 14,
            }
        ],
        "badges": [{"name": "CollectionLevel", "level": 8, "progress": collection_level}],
    }


def _emit_cards(conn, collection_level, at, knight_level=14):
    return emit(
        conn,
        "player",
        TAG,
        "cards",
        project_player_aspects(_cards_payload(collection_level, knight_level))["cards"],
        at,
    )


def _milestone_events(conn):
    return [
        r["payload_json"]
        for r in conn.execute(
            "SELECT payload_json FROM player_events "
            "WHERE event_type='collection_level_milestone' ORDER BY event_id"
        )
    ]


def test_projector_puts_collection_level_in_cards_aspect(engine_conn):
    """Regression: the badge must land in the aspect emit_cards reads. If this
    key ever moves back to the profile aspect (or drops), the signal goes dark
    silently — which is exactly what a plain 'no errors' check would miss."""
    aspects = project_player_aspects(_cards_payload(1673))
    assert aspects["cards"]["collection_level"]["progress"] == 1673


def test_first_sight_emits_no_milestone(engine_conn):
    assert _emit_cards(engine_conn, 1673, NOW) == 0
    assert _milestone_events(engine_conn) == []


def test_crosses_one_century_boundary(engine_conn):
    _emit_cards(engine_conn, 1673, NOW)  # first sight — baseline only
    _emit_cards(engine_conn, 1712, LATER)  # 1673 -> 1712 crosses 1700
    events = _milestone_events(engine_conn)
    assert len(events) == 1
    assert '"milestone":1700' in events[0].replace(" ", "")


def test_crosses_two_boundaries_at_once(engine_conn):
    _emit_cards(engine_conn, 1712, NOW)  # first sight
    _emit_cards(engine_conn, 1905, LATER)  # 1712 -> 1905 crosses 1800 and 1900
    events = _milestone_events(engine_conn)
    milestones = {
        m for e in events for m in (1800, 1900) if f'"milestone":{m}' in e.replace(" ", "")
    }
    assert milestones == {1800, 1900}


def test_no_boundary_no_milestone(engine_conn):
    _emit_cards(engine_conn, 1712, NOW)  # first sight
    _emit_cards(engine_conn, 1755, LATER)  # 1712 -> 1755 crosses nothing
    assert _milestone_events(engine_conn) == []
