import json
from datetime import datetime, timezone

import db
from storage.game_modes import classify_battle_mode


def _battle_ts(time_part: str) -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d") + f"T{time_part}.000Z"


def _battle(ts, *, battle_type, game_mode_id, game_mode_name, team_size=1, event_tag=None, tournament_tag=None):
    team = [
        {
            "tag": "#ABC123",
            "name": "Alpha",
            "crowns": 1,
            "cards": [],
            "supportCards": [],
            "trophyChange": 30 if battle_type in {"PvP", "pathOfLegend"} else None,
            "startingTrophies": 1000 if battle_type in {"PvP", "pathOfLegend"} else None,
        }
        for _ in range(team_size)
    ]
    opponent = [
        {
            "tag": f"#OPP{idx}{ts[-8:-5]}",
            "name": f"Opp {idx}",
            "crowns": 0,
            "cards": [],
            "supportCards": [],
        }
        for idx in range(team_size)
    ]
    return {
        "type": battle_type,
        "battleTime": ts,
        "gameMode": {"id": game_mode_id, "name": game_mode_name},
        "deckSelection": "collection",
        "team": team,
        "opponent": opponent,
        **({"eventTag": event_tag} if event_tag else {}),
        **({"tournamentTag": tournament_tag} if tournament_tag else {}),
    }


def test_classify_battle_mode_uses_docs_taxonomy_order():
    assert classify_battle_mode(battle_type="pathOfLegend", game_mode_id=72000464, game_mode_name="Ranked1v1_NewArena2") == "ranked"
    assert classify_battle_mode(battle_type="riverRacePvP", game_mode_id=72000070, game_mode_name="RampUpElixir_Ladder") == "war"
    assert classify_battle_mode(battle_type="trail", game_mode_id=72000014, game_mode_name="TeamVsTeam", event_tag="#E") == "two_v_two"
    assert classify_battle_mode(battle_type="tournament", game_mode_id=72000194, game_mode_name="Draft_Competitive", tournament_tag="#T") == "tournament"
    assert classify_battle_mode(battle_type="friendly", game_mode_id=72000007, game_mode_name="Friendly") == "friendly"


def test_battle_rollups_split_new_mode_groups():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#ABC123", "name": "Alpha", "role": "member"}], conn=conn)
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                _battle(_battle_ts("100000"), battle_type="PvP", game_mode_id=72000006, game_mode_name="Ladder"),
                _battle(_battle_ts("100100"), battle_type="pathOfLegend", game_mode_id=72000464, game_mode_name="Ranked1v1_NewArena2"),
                _battle(_battle_ts("100200"), battle_type="trail", game_mode_id=72000014, game_mode_name="TeamVsTeam", team_size=2, event_tag="#E"),
                _battle(_battle_ts("100300"), battle_type="tournament", game_mode_id=72000194, game_mode_name="Draft_Competitive", tournament_tag="#T"),
                _battle(_battle_ts("100400"), battle_type="friendly", game_mode_id=72000007, game_mode_name="Friendly"),
            ],
            conn=conn,
        )
        rows = db.list_member_daily_battle_rollups("#ABC123", days=1, conn=conn)
        groups = {row["mode_group"] for row in rows}
    finally:
        conn.close()

    assert {"ladder", "ranked", "two_v_two", "tournament", "friendly"}.issubset(groups)


def test_ranked_and_clan_game_mode_query_helpers():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#ABC123", "name": "Alpha", "role": "member"}], conn=conn)
        db.snapshot_player_profile(
            {
                "tag": "#ABC123",
                "name": "Alpha",
                "currentPathOfLegendSeasonResult": {"leagueNumber": 6, "trophies": 1200, "rank": None},
                "lastPathOfLegendSeasonResult": {"leagueNumber": 5, "trophies": 1000, "rank": None},
                "bestPathOfLegendSeasonResult": {"leagueNumber": 7, "trophies": 1800, "rank": None},
                "progress": {"AutoChess_2026_Season_9": {"trophies": 2100, "bestTrophies": 2200}},
                "currentDeck": [],
                "cards": [],
            },
            conn=conn,
        )
        db.snapshot_player_battlelog(
            "#ABC123",
            [_battle(_battle_ts("100100"), battle_type="pathOfLegend", game_mode_id=72000464, game_mode_name="Ranked1v1_NewArena2")],
            conn=conn,
        )
        ranked = db.get_member_ranked_status("#ABC123", days=1, conn=conn)
        summary = db.get_clan_game_mode_summary(days=1, conn=conn)
    finally:
        conn.close()

    assert ranked["current"]["leagueNumber"] == 6
    assert ranked["recent_ranked"]["battles"] == 1
    assert summary["ranked_activity"][0]["tag"] == "#ABC123"
    assert summary["side_mode_progress"][0]["progress_key"] == "AutoChess_2026_Season_9"


def test_game_mode_contexts_capture_events_and_leaderboards():
    conn = db.get_connection(":memory:")
    try:
        db.upsert_game_mode_contexts_from_events(
            [{"eventTag": "#E", "title": "Princess Gambit", "gameMode": {"id": 72000469, "name": "DraftMode_Princess"}}],
            conn=conn,
        )
        db.upsert_game_mode_contexts_from_leaderboards(
            {"items": [{"id": 170000019, "name": "Merge Tactics"}]},
            conn=conn,
        )
        events = db.list_game_mode_contexts("event", conn=conn)
        boards = db.list_game_mode_contexts("leaderboard", conn=conn)
    finally:
        conn.close()

    assert events[0]["display_name"] == "Princess Gambit"
    assert boards[0]["display_name"] == "Merge Tactics"
    assert json.dumps(events + boards)


def test_anarchy_badges_do_not_attach_to_princess_battle_activity():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#ABC123", "name": "Alpha", "role": "member"}], conn=conn)
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                _battle(_battle_ts("100100"), battle_type="trail", game_mode_id=72000501, game_mode_name="All_Random_Princess", event_tag="#PRINCESS"),
                _battle(_battle_ts("100200"), battle_type="trail", game_mode_id=72000501, game_mode_name="All_Random_Princess", event_tag="#PRINCESS"),
            ],
            conn=conn,
        )
        db.record_signal_events(
            [{
                "type": "badge_earned",
                "tag": "#ABC123",
                "name": "Alpha",
                "badge_name": "AnarchyLeagueCompletion",
                "badge_label": "Anarchy League Completion",
                "badge_category": "event",
            }],
            source_system="test",
            source_detector="player_profile",
            conn=conn,
        )
        summary = db.get_clan_game_mode_summary(days=1, mode_group="special_event", limit=5, conn=conn)
    finally:
        conn.close()

    assert summary["by_game_mode"][0]["game_mode_name"] == "All_Random_Princess"
    assert "event_name" not in summary["by_game_mode"][0]
    assert summary["event_participation"][0]["tag"] == "#ABC123"
    assert summary["event_participation"][0]["event_battles"] == 2
    assert "event_name" not in summary["event_participation"][0]
    assert "badge_completions" not in summary["event_participation"][0]
    assert summary["event_badge_completions"][0]["badge_name"] == "AnarchyLeagueCompletion"
    assert summary["event_badge_completions"][0]["event_name"] == "Anarchy League"
    assert "event_game_mode_name" not in summary["event_badge_completions"][0]


def test_member_highlight_context_keeps_anarchy_badge_separate_from_princess_activity(monkeypatch):
    from runtime.signals.context import _build_outcome_context

    monkeypatch.setattr("runtime.signals.context._build_player_insight_context", lambda tag: [])

    context = _build_outcome_context(
        {"target_channel_key": "member-highlights", "intent": "member_highlights"},
        [{
            "type": "badge_earned",
            "tag": "#ABC123",
            "name": "Alpha",
            "badge_name": "AnarchyLeagueCompletion",
            "badge_label": "Anarchy League Completion",
            "badge_level": 1,
            "progress": 1,
            "target": 2,
        }],
        clan={},
        war={},
    )

    assert "CURRENT EVENT CONTEXT" in context
    assert "current_event: Anarchy League" in context
    assert "badge_progress: 1/2" in context
    assert "All_Random_Princess" not in context
    assert "evidence_boundary: No battle-mode source is encoded for this badge" in context
