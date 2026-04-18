"""Tests for tournament signal generation during polling."""

import db
from storage.tournament import poll_tournament, register_tournament


def _api_payload(*, name="PK Clan Tourney", status="inPreparation", members):
    return {
        "name": name,
        "description": "Clan tournament",
        "type": "passwordProtected",
        "status": status,
        "creatorTag": "#ABC123",
        "gameMode": {"id": 12000000, "name": "Draft"},
        "levelCap": 15,
        "maxCapacity": 50,
        "duration": 3600,
        "preparationDuration": 3600,
        "createdTime": "20260418T130000.000Z",
        "startedTime": None,
        "endedTime": None,
        "membersList": members,
    }


def test_poll_tournament_emits_no_joins_on_first_poll_after_registration():
    conn = db.get_connection(":memory:")
    try:
        seed_members = [
            {"tag": "#ABC123", "name": "King Thing", "score": 0, "rank": 1},
            {"tag": "#DEF456", "name": "King Levy", "score": 0, "rank": 1},
        ]
        payload = _api_payload(members=seed_members)
        register_tournament("#2QG9Y9UR", payload, conn=conn)
        # Same members on the first poll — nothing is new.
        result = poll_tournament("#2QG9Y9UR", payload, conn=conn)
        types = [s["type"] for s in result["live_signals"]]
        assert "tournament_participant_joined" not in types
    finally:
        conn.close()


def test_poll_tournament_emits_join_signal_for_each_new_participant():
    conn = db.get_connection(":memory:")
    try:
        seed = [{"tag": "#ABC123", "name": "King Thing", "score": 0, "rank": 1}]
        register_tournament("#2QG9Y9UR", _api_payload(members=seed), conn=conn)
        # First poll: still just the seed roster.
        poll_tournament("#2QG9Y9UR", _api_payload(members=seed), conn=conn)
        # Second poll: two new joiners appear.
        expanded = seed + [
            {"tag": "#DEF456", "name": "King Levy", "score": 0, "rank": 1,
             "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS"}},
            {"tag": "#GHI789", "name": "Ditika", "score": 0, "rank": 1},
        ]
        result = poll_tournament("#2QG9Y9UR", _api_payload(members=expanded), conn=conn)
        join_signals = [s for s in result["live_signals"] if s["type"] == "tournament_participant_joined"]
        assert len(join_signals) == 2
        by_tag = {s["player_tag"]: s for s in join_signals}
        assert by_tag["#DEF456"]["player_name"] == "King Levy"
        assert by_tag["#DEF456"]["clan_name"] == "POAP KINGS"
        assert by_tag["#GHI789"]["player_name"] == "Ditika"
        # signal_key should dedupe per (tournament, player) so the awareness
        # pipeline never double-posts the same join.
        assert by_tag["#DEF456"]["signal_key"] == "tournament_participant_joined|#2QG9Y9UR|#DEF456"
    finally:
        conn.close()


def test_poll_tournament_does_not_resignal_known_participants():
    """A returning participant across polls must not generate a new join signal."""
    conn = db.get_connection(":memory:")
    try:
        roster = [
            {"tag": "#ABC123", "name": "King Thing", "score": 0, "rank": 1},
            {"tag": "#DEF456", "name": "King Levy", "score": 100, "rank": 1},
        ]
        register_tournament("#2QG9Y9UR", _api_payload(members=roster), conn=conn)
        poll_tournament("#2QG9Y9UR", _api_payload(members=roster), conn=conn)
        # Same roster, updated scores.
        for m in roster:
            m["score"] = (m["score"] or 0) + 50
        result = poll_tournament("#2QG9Y9UR", _api_payload(members=roster), conn=conn)
        types = [s["type"] for s in result["live_signals"]]
        assert "tournament_participant_joined" not in types
    finally:
        conn.close()
