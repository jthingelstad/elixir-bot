"""Focused tests for V2 agent tools."""

import json
from unittest.mock import patch

import elixir_agent


def test_execute_tool_list_clan_members():
    with patch("elixir_agent.db") as mock_db:
        mock_db.list_members.return_value = [{"player_tag": "#ABC123", "member_name": "King Levy"}]
        result = json.loads(elixir_agent._execute_tool("list_clan_members", {}))
        assert result == [{"player_tag": "#ABC123", "member_name": "King Levy"}]
        mock_db.list_members.assert_called_once_with()


def test_execute_tool_resolve_member_uses_db_query():
    with patch("elixir_agent.db") as mock_db:
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_source": "current_name_exact"}]
        result = json.loads(elixir_agent._execute_tool("resolve_member", {"query": "King Levy"}))
        assert result[0]["player_tag"] == "#ABC123"
        mock_db.resolve_member.assert_called_once_with("King Levy", limit=5)


def test_execute_tool_get_member_profile_refreshes_member_cache():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_profile.return_value = {"player_tag": "#ABC123", "member_name": "King Levy"}

        result = json.loads(elixir_agent._execute_tool("get_member_profile", {"member_tag": "#ABC123"}))

        assert result == {"player_tag": "#ABC123", "member_name": "King Levy"}
        mock_db.snapshot_player_profile.assert_called_once()
        mock_db.snapshot_player_battlelog.assert_called_once()
        mock_db.get_member_profile.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_profile_resolves_handle_before_refresh():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 875}]
        mock_db.get_member_profile.return_value = {"player_tag": "#ABC123", "member_name": "King Levy"}

        result = json.loads(elixir_agent._execute_tool("get_member_profile", {"member_tag": "@jamie"}))

        assert result == {"player_tag": "#ABC123", "member_name": "King Levy"}
        mock_db.resolve_member.assert_called_once_with("@jamie", limit=5)
        mock_db.get_member_profile.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_overview_refreshes_member_cache():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_overview.return_value = {"player_tag": "#ABC123", "member_name": "King Levy"}
        result = json.loads(elixir_agent._execute_tool("get_member_overview", {"member_tag": "#ABC123"}))
        assert result == {"player_tag": "#ABC123", "member_name": "King Levy"}
        mock_db.get_member_overview.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_next_chests_uses_cr_api():
    with patch("elixir_agent.cr_api.get_player_chests", return_value=[{"name": "Silver Chest", "index": 1}]) as mock_chests:
        result = json.loads(elixir_agent._execute_tool("get_member_next_chests", {"member_tag": "#ABC123"}))
        assert result == [{"name": "Silver Chest", "index": 1}]
        mock_chests.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_next_chests_resolves_member_name():
    with (
        patch("elixir_agent.cr_api.get_player_chests", return_value=[{"name": "Silver Chest", "index": 1}]) as mock_chests,
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        result = json.loads(elixir_agent._execute_tool("get_member_next_chests", {"member_tag": "King Levy"}))
        assert result == [{"name": "Silver Chest", "index": 1}]
        mock_db.resolve_member.assert_called_once_with("King Levy", limit=5)
        mock_chests.assert_called_once_with("#ABC123")


def test_execute_tool_get_war_season_summary_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_season_summary.return_value = {"season_id": 129, "races": 4}
        result = json.loads(elixir_agent._execute_tool("get_war_season_summary", {}))
        assert result == {"season_id": 129, "races": 4}
        mock_db.get_war_season_summary.assert_called_once_with(season_id=None, top_n=5)


def test_execute_tool_get_trending_war_contributors_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_trending_war_contributors.return_value = {"season_id": 129, "members": []}
        result = json.loads(elixir_agent._execute_tool("get_trending_war_contributors", {}))
        assert result == {"season_id": 129, "members": []}
        mock_db.get_trending_war_contributors.assert_called_once_with(season_id=None, recent_races=2, limit=5)


def test_execute_tool_compare_member_war_to_clan_average_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.compare_member_war_to_clan_average.return_value = {"member": {"tag": "#ABC123"}}
        result = json.loads(
            elixir_agent._execute_tool(
                "compare_member_war_to_clan_average",
                {"member_tag": "#ABC123", "season_id": 129},
            )
        )
        assert result == {"member": {"tag": "#ABC123"}}
        mock_db.compare_member_war_to_clan_average.assert_called_once_with("#ABC123", season_id=129)


def test_resolve_member_tag_returns_clear_ambiguity_error():
    with patch("elixir_agent.db") as mock_db:
        mock_db.resolve_member.return_value = [
            {"player_tag": "#ABC123", "match_score": 650, "member_ref_with_handle": "King Levy (@jamie)"},
            {"player_tag": "#DEF456", "match_score": 625, "member_ref_with_handle": "King Levi (@levi)"},
        ]
        result = json.loads(elixir_agent._execute_tool("get_member_history", {"member_tag": "King Lev"}))
        assert "error" in result
        assert "Ambiguous member reference" in result["error"]


def test_execute_tool_get_members_at_risk_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_members_at_risk.return_value = {"members": []}
        result = json.loads(
            elixir_agent._execute_tool(
                "get_members_at_risk",
                {"require_war_participation": True, "season_id": 129},
            )
        )
        assert result == {"members": []}
        mock_db.get_members_at_risk.assert_called_once_with(
            inactivity_days=7,
            min_donations_week=20,
            require_war_participation=True,
            min_war_races=1,
            tenure_grace_days=14,
            season_id=129,
        )


def test_execute_tool_get_current_war_status_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_current_war_status.return_value = {"war_state": "full", "season_id": 129}
        result = json.loads(elixir_agent._execute_tool("get_current_war_status", {}))
        assert result == {"war_state": "full", "season_id": 129}
        mock_db.get_current_war_status.assert_called_once_with()


def test_execute_tool_get_member_war_attendance_resolves_member():
    with patch("elixir_agent.db") as mock_db:
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        mock_db.get_member_war_attendance.return_value = {"season": {"participation_rate": 1.0}}
        result = json.loads(elixir_agent._execute_tool("get_member_war_attendance", {"member_tag": "King Levy"}))
        assert result == {"season": {"participation_rate": 1.0}}
        mock_db.get_member_war_attendance.assert_called_once_with("#ABC123", season_id=None)


def test_execute_tool_get_war_battle_win_rates_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_battle_win_rates.return_value = {"season_id": 129, "members": []}
        result = json.loads(elixir_agent._execute_tool("get_war_battle_win_rates", {"season_id": 129, "limit": 5, "min_battles": 2}))
        assert result == {"season_id": 129, "members": []}
        mock_db.get_war_battle_win_rates.assert_called_once_with(season_id=129, limit=5, min_battles=2)


def test_execute_tool_get_recent_role_changes_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_recent_role_changes.return_value = [{"tag": "#ABC123", "old_role": "member", "new_role": "elder"}]
        result = json.loads(elixir_agent._execute_tool("get_recent_role_changes", {"days": 14}))
        assert result[0]["new_role"] == "elder"
        mock_db.get_recent_role_changes.assert_called_once_with(days=14)


def test_execute_tool_boat_battle_and_trend_queries_use_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_clan_boat_battle_record.return_value = {"wins": 2, "losses": 1}
        mock_db.get_war_score_trend.return_value = {"direction": "up", "score_change": 30}
        mock_db.compare_fame_per_member_to_previous_season.return_value = {"direction": "up", "delta": 120.0}

        boat = json.loads(elixir_agent._execute_tool("get_clan_boat_battle_record", {"wars": 3}))
        trend = json.loads(elixir_agent._execute_tool("get_war_score_trend", {"days": 30}))
        fame = json.loads(elixir_agent._execute_tool("compare_fame_per_member_to_previous_season", {"season_id": 129}))

        assert boat == {"wins": 2, "losses": 1}
        assert trend["direction"] == "up"
        assert fame["delta"] == 120.0
        mock_db.get_clan_boat_battle_record.assert_called_once_with(wars=3)
        mock_db.get_war_score_trend.assert_called_once_with(days=30)
        mock_db.compare_fame_per_member_to_previous_season.assert_called_once_with(season_id=129)


def test_execute_tool_get_member_missed_war_days_resolves_member():
    with patch("elixir_agent.db") as mock_db:
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        mock_db.get_member_missed_war_days.return_value = {"days_missed": 1}
        result = json.loads(elixir_agent._execute_tool("get_member_missed_war_days", {"member_tag": "@jamie", "season_id": 129}))
        assert result == {"days_missed": 1}
        mock_db.get_member_missed_war_days.assert_called_once_with("#ABC123", season_id=129)


def test_respond_in_channel_uses_interactive_read_only_workflow():
    with patch("elixir_agent._chat_with_tools", return_value={"event_type": "channel_response", "content": "hi"}) as mock_chat:
        result = elixir_agent.respond_in_channel(
            question="How am I doing?",
            author_name="Jamie",
            channel_name="#member-chat",
            workflow="interactive",
            clan_data={"memberList": []},
            war_data={},
            conversation_history=[],
            memory_context={},
            proactive=False,
        )
        assert result["event_type"] == "channel_response"
        assert mock_chat.call_args.kwargs["workflow"] == "interactive"
        assert mock_chat.call_args.kwargs["allowed_tools"] == elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"]


def test_respond_in_channel_uses_clanops_proactive_workflow():
    with patch("elixir_agent._chat_with_tools", return_value=None) as mock_chat:
        result = elixir_agent.respond_in_channel(
            question="We should review promotions this week.",
            author_name="Jamie",
            channel_name="#clan-ops",
            workflow="clanops",
            clan_data={"memberList": []},
            war_data={},
            conversation_history=[],
            memory_context={},
            proactive=True,
        )
        assert result is None
        assert mock_chat.call_args.kwargs["workflow"] == "clanops_proactive"
        assert mock_chat.call_args.kwargs["allowed_tools"] == elixir_agent.TOOLSETS_BY_WORKFLOW["clanops_proactive"]
