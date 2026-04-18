"""Tests for tournament signal generation during polling."""

import db
from runtime.jobs._tournament import _build_battle_played_signal
from storage.tournament import poll_tournament, register_tournament, store_tournament_battle


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


def _battle_payload(tournament_tag="#2QG9Y9UR", team_tag="#ABC123", opp_tag="#DEF456",
                     team_crowns=3, opp_crowns=1, team_name="King Thing", opp_name="King Levy"):
    return {
        "battleTime": "20260418T141500.000Z",
        "tournamentTag": tournament_tag,
        "type": "challenge",
        "deckSelection": "draft",
        "gameMode": {"id": 72000001, "name": "CW_Duel_1v1"},
        "arena": {"name": "Legendary Arena"},
        "team": [{
            "tag": team_tag, "name": team_name, "crowns": team_crowns,
            "cards": [{"name": "Hog Rider", "id": 1, "level": 14, "maxLevel": 15},
                       {"name": "Ice Spirit", "id": 2, "level": 14, "maxLevel": 15}],
        }],
        "opponent": [{
            "tag": opp_tag, "name": opp_name, "crowns": opp_crowns,
            "cards": [{"name": "Golem", "id": 3, "level": 14, "maxLevel": 15}],
        }],
    }


def test_store_tournament_battle_returns_signal_ready_dict_on_insert():
    conn = db.get_connection(":memory:")
    try:
        register_tournament("#2QG9Y9UR", _api_payload(members=[
            {"tag": "#ABC123", "name": "King Thing"},
            {"tag": "#DEF456", "name": "King Levy"},
        ]), conn=conn)
        tid = conn.execute("SELECT tournament_id FROM tournaments").fetchone()["tournament_id"]
        info = store_tournament_battle(tid, _battle_payload(), conn=conn)
        assert info is not None
        # Canonical order is lex-smallest tag first: #ABC123 < #DEF456
        assert info["player1_tag"] == "#ABC123"
        assert info["player2_tag"] == "#DEF456"
        assert info["winner_tag"] == "#ABC123"
        assert info["player1_crowns"] == 3
        assert info["player2_crowns"] == 1
        assert "Hog Rider" in info["player1_deck"]
        assert info["game_mode_name"] == "CW_Duel_1v1"
    finally:
        conn.close()


def test_store_tournament_battle_returns_none_on_duplicate():
    conn = db.get_connection(":memory:")
    try:
        register_tournament("#2QG9Y9UR", _api_payload(members=[
            {"tag": "#ABC123", "name": "King Thing"},
            {"tag": "#DEF456", "name": "King Levy"},
        ]), conn=conn)
        tid = conn.execute("SELECT tournament_id FROM tournaments").fetchone()["tournament_id"]
        first = store_tournament_battle(tid, _battle_payload(), conn=conn)
        assert first is not None
        # Same battle fetched from the other player's log — should dedup.
        second = store_tournament_battle(tid, _battle_payload(), conn=conn)
        assert second is None
    finally:
        conn.close()


def test_generate_tournament_update_uses_dedicated_prompt_and_tools():
    """The tournament update path must use cr_api, the tournament schema, and
    feed only the signal list into the user message (no war state injection).
    """
    import elixir_agent
    from unittest.mock import patch

    captured = {}

    def fake_chat_with_tools(system_prompt, user_message, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_message"] = user_message
        captured["kwargs"] = kwargs
        return {"event_type": "tournament_update", "summary": "test", "content": "A match played."}

    signals = [{
        "type": "tournament_battle_played",
        "signal_key": "tournament_battle_played|#2QG9Y9UR|20260418T141500.000Z|#ABC|#DEF",
        "tournament_tag": "#2QG9Y9UR",
        "audience": "clan_internal",
    }]
    with patch("agent.workflows._chat_with_tools", side_effect=fake_chat_with_tools):
        result = elixir_agent.generate_tournament_update(signals)

    assert result["content"] == "A match played."
    assert captured["kwargs"]["workflow"] == "tournament_update"
    tool_names = {t["name"] for t in captured["kwargs"]["allowed_tools"]}
    assert tool_names == {"cr_api"}
    # The user message must carry the signal JSON and not contain war-state
    # fragments that the main channel_update path layers in.
    assert "tournament_battle_played" in captured["user_message"]
    # The data injected into the user message must not contain actual war
    # state values — the old path leaked TIME / PHASE blocks with hours
    # remaining and day numbers. Those blocks should be entirely absent.
    assert "TIME / PHASE" not in captured["user_message"]
    assert "hours_remaining_in_day" not in captured["user_message"]
    assert "RACE STANDINGS" not in captured["user_message"]


def test_tournament_system_prompt_excludes_clan_events_prose():
    """The tournament system prompt loads its own subagent file — it must not
    drag in clan-events.md content (that's the channel whose prompt caused
    the war-context confusion we're fixing)."""
    from agent.prompts import _tournament_update_system

    prompt = _tournament_update_system()
    # Positive signal: tournament-specific content is present.
    assert "Tournament Lane" in prompt or "tournament" in prompt.lower()
    # Negative signal: clan-events voice prose should not be in here.
    assert "Roster lifecycle" not in prompt
    assert "Communal. Proud." not in prompt


def test_build_battle_played_signal_audience_classification():
    base_info = {
        "battle_time": "20260418T141500.000Z",
        "player1_tag": "#ABC123", "player1_name": "King Thing", "player1_crowns": 3,
        "player1_deck": ["Hog Rider", "Ice Spirit"],
        "player2_tag": "#DEF456", "player2_name": "Rival", "player2_crowns": 1,
        "player2_deck": ["Golem"],
        "winner_tag": "#ABC123",
        "deck_selection": "draft",
        "game_mode_name": "CW_Duel_1v1",
        "arena_name": "Legendary Arena",
    }

    both = _build_battle_played_signal("#2QG9Y9UR", "PK Cup",
        {**base_info, "player1_is_clan_member": True, "player2_is_clan_member": True})
    assert both["audience"] == "clan_internal"
    assert both["winner_name"] == "King Thing"
    assert both["loser_name"] == "Rival"
    assert both["signal_key"] == "tournament_battle_played|#2QG9Y9UR|20260418T141500.000Z|#ABC123|#DEF456"

    one = _build_battle_played_signal("#2QG9Y9UR", "PK Cup",
        {**base_info, "player1_is_clan_member": True, "player2_is_clan_member": False})
    assert one["audience"] == "clan_one_side"

    neither = _build_battle_played_signal("#2QG9Y9UR", "PK Cup",
        {**base_info, "player1_is_clan_member": False, "player2_is_clan_member": False})
    assert neither["audience"] == "external_observed"
