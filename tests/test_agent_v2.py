"""Focused tests for agent query tools."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

        assert result["player_tag"] == "#ABC123"
        assert result["member_name"] == "King Levy"
        assert result["gold_known"] is False
        mock_db.snapshot_player_profile.assert_called_once()
        mock_db.snapshot_player_battlelog.assert_called_once()
        mock_db.get_member_profile.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_profile_includes_account_age_and_activity_summaries():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_profile.return_value = {
            "player_tag": "#ABC123",
            "member_name": "King Levy",
            "cr_account_age_years": 4,
            "cr_account_age_days": 1474,
            "cr_games_per_day": 3.64,
            "cr_games_per_day_window_days": 14,
        }

        result = json.loads(elixir_agent._execute_tool("get_member_profile", {"member_tag": "#ABC123"}))

        assert result["account_age_summary"] == (
            "Derived Clash Royale account age from Years Played badge data: 4 years / 1,474 days"
        )
        assert result["recent_activity_summary"] == (
            "Recent activity: 3.64 games played per day over the last 14 days"
        )


def test_execute_tool_get_member_profile_includes_current_role_summary():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Thing"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_profile.return_value = {
            "player_tag": "#ABC123",
            "member_name": "King Thing",
            "role": "leader",
        }

        result = json.loads(elixir_agent._execute_tool("get_member_profile", {"member_tag": "#ABC123"}))

        assert result["current_role_summary"] == "King Thing is currently the clan leader."


def test_execute_tool_get_member_card_collection_uses_db():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_card_collection.return_value = {
            "summary": {"highest_level": 16},
            "cards": [{"name": "Knight", "level": 16}],
        }

        result = json.loads(
            elixir_agent._execute_tool(
                "get_member_card_collection",
                {"member_tag": "#ABC123", "limit": 25, "min_level": 14},
            )
        )

        assert result["summary"]["highest_level"] == 16
        assert result["gold_known"] is False
        assert "Current gold is not available" in result["gold_note"]
        mock_db.snapshot_player_profile.assert_called_once()
        mock_db.get_member_card_collection.assert_called_once_with(
            "#ABC123",
            limit=25,
            min_level=14,
            include_support=True,
            rarity=None,
        )


def test_build_tool_result_envelope_strips_card_image_fields_from_context():
    raw = json.dumps(
        {
            "player_tag": "#ABC123",
            "profile_url": "https://example.com/profile",
            "current_deck": {
                "cards": [
                    {
                        "name": "Hog Rider",
                        "iconUrls": {"medium": "https://cdn.example/hog.png"},
                    }
                ]
            },
            "signature_cards": {
                "cards": [
                    {
                        "name": "Fireball",
                        "icon_url": "https://cdn.example/fireball.png",
                        "usage_pct": 64,
                    }
                ]
            },
        }
    )

    envelope = json.loads(elixir_agent._build_tool_result_envelope("get_member_overview", raw))

    assert envelope["data"]["profile_url"] == "https://example.com/profile"
    assert "iconUrls" not in envelope["data"]["current_deck"]["cards"][0]
    assert "icon_url" not in envelope["data"]["signature_cards"]["cards"][0]


def test_interactive_workflow_does_not_expose_sensitive_leadership_read_tools():
    interactive_names = {
        tool["name"] for tool in elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"]
    }

    assert "get_promotion_candidates" not in interactive_names
    assert "get_members_at_risk" not in interactive_names
    assert "get_member_profile" in interactive_names


def test_execute_tool_get_member_profile_resolves_handle_before_refresh():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 875}]
        mock_db.get_member_profile.return_value = {"player_tag": "#ABC123", "member_name": "King Levy"}

        result = json.loads(elixir_agent._execute_tool("get_member_profile", {"member_tag": "@jamie"}))

        assert result["player_tag"] == "#ABC123"
        assert result["member_name"] == "King Levy"
        assert result["gold_known"] is False
        mock_db.resolve_member.assert_called_once_with("@jamie", limit=5)
        mock_db.get_member_profile.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_card_collection_accepts_bare_player_tag():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#20JJJ2CCRU", "name": "King Thing"}),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_card_collection.return_value = {"summary": {"highest_level": 16}, "cards": []}

        result = json.loads(elixir_agent._execute_tool("get_member_card_collection", {"member_tag": "20JJJ2CCRU"}))

        assert result["summary"]["highest_level"] == 16
        mock_db.resolve_member.assert_not_called()
        mock_db.get_member_card_collection.assert_called_once_with(
            "#20JJJ2CCRU",
            limit=60,
            min_level=None,
            include_support=True,
            rarity=None,
        )


def test_execute_tool_get_member_card_collection_passes_rarity_filter():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#20JJJ2CCRU", "name": "King Thing"}),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_card_collection.return_value = {
            "summary": {"rarity_counts": {"legendary": 5}},
            "cards_by_rarity": {"legendary": ["Royal Ghost", "Princess"]},
            "cards": [],
            "support_cards": [],
        }

        result = json.loads(
            elixir_agent._execute_tool(
                "get_member_card_collection",
                {"member_tag": "20JJJ2CCRU", "rarity": "legendary"},
            )
        )

        assert result["summary"]["rarity_counts"]["legendary"] == 5
        assert "Royal Ghost" in result["cards_by_rarity"]["legendary"]
        mock_db.get_member_card_collection.assert_called_once_with(
            "#20JJJ2CCRU",
            limit=60,
            min_level=None,
            include_support=True,
            rarity="legendary",
        )


def test_execute_tool_get_member_overview_refreshes_member_cache():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_overview.return_value = {"player_tag": "#ABC123", "member_name": "King Levy"}
        result = json.loads(elixir_agent._execute_tool("get_member_overview", {"member_tag": "#ABC123"}))
        assert result["player_tag"] == "#ABC123"
        assert result["member_name"] == "King Levy"
        assert result["gold_known"] is False
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


def test_execute_tool_level_16_and_hot_streak_queries_use_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_members_with_most_level_16_cards.return_value = [{"tag": "#ABC123", "level_16_count": 42}]
        mock_db.get_members_on_hot_streak.return_value = [{"tag": "#ABC123", "current_streak": 6}]

        elite = json.loads(elixir_agent._execute_tool("get_members_with_most_level_16_cards", {"limit": 5}))
        hot = json.loads(elixir_agent._execute_tool("get_members_on_hot_streak", {"min_streak": 5}))

        assert elite == [{"tag": "#ABC123", "level_16_count": 42}]
        assert hot == [{"tag": "#ABC123", "current_streak": 6}]
        mock_db.get_members_with_most_level_16_cards.assert_called_once_with(limit=5)
        mock_db.get_members_on_hot_streak.assert_called_once_with(min_streak=5, scope="ladder_ranked_10")


def test_execute_tool_trend_queries_use_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        mock_db.compare_member_trend_windows.return_value = {"member": {"tag": "#ABC123"}, "window_days": 14}
        mock_db.build_member_trend_summary_context.return_value = "=== MEMBER TREND SUMMARY ==="
        mock_db.compare_clan_trend_windows.return_value = {"clan": {"clan_tag": "#J2RGCRVG"}, "window_days": 14}
        mock_db.build_clan_trend_summary_context.return_value = "=== CLAN TREND SUMMARY ==="

        member_cmp = json.loads(elixir_agent._execute_tool("compare_member_trend_windows", {"member_tag": "King Levy", "window_days": 14}))
        member_summary = json.loads(elixir_agent._execute_tool("get_member_trend_summary", {"member_tag": "King Levy", "days": 21, "window_days": 14}))
        clan_cmp = json.loads(elixir_agent._execute_tool("compare_clan_trend_windows", {"window_days": 14}))
        clan_summary = json.loads(elixir_agent._execute_tool("get_clan_trend_summary", {"days": 21, "window_days": 14}))

        assert member_cmp["window_days"] == 14
        assert member_summary == "=== MEMBER TREND SUMMARY ==="
        assert clan_cmp["window_days"] == 14
        assert clan_summary == "=== CLAN TREND SUMMARY ==="
        mock_db.compare_member_trend_windows.assert_called_once_with("#ABC123", window_days=14)
        mock_db.build_member_trend_summary_context.assert_called_once_with("#ABC123", days=21, window_days=14)
        mock_db.compare_clan_trend_windows.assert_called_once_with(window_days=14)
        mock_db.build_clan_trend_summary_context.assert_called_once_with(days=21, window_days=14)


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


def test_execute_tool_get_current_war_day_state_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_current_war_day_state.return_value = {"war_day_key": "s00129-w01-p010", "phase_display": "Battle Day 1"}
        result = json.loads(elixir_agent._execute_tool("get_current_war_day_state", {}))
        assert result == {"war_day_key": "s00129-w01-p010", "phase_display": "Battle Day 1"}
        mock_db.get_current_war_day_state.assert_called_once_with()


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
    with (
        patch("elixir_agent._chat_with_tools", return_value={"event_type": "channel_response", "content": "hi"}) as mock_chat,
        patch("agent.workflows.db.build_clan_trend_summary_context", return_value="=== CLAN TREND SUMMARY ===\nclan: POAP KINGS"),
    ):
        result = elixir_agent.respond_in_channel(
            question="How am I doing?",
            author_name="Jamie",
            channel_name="#member-chat",
            workflow="interactive",
            clan_data={"memberList": []},
            war_data={},
            conversation_history=[],
            memory_context={},
        )
        assert result["event_type"] == "channel_response"
        assert mock_chat.call_args.kwargs["workflow"] == "interactive"
        assert mock_chat.call_args.kwargs["allowed_tools"] == elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"]
        assert "=== CLAN TREND SUMMARY ===" in mock_chat.call_args.args[1]


def test_respond_in_channel_keeps_ask_elixir_lightweight_followups_focused():
    with patch("elixir_agent._chat_with_tools", return_value={"event_type": "channel_response", "content": "Appreciated."}) as mock_chat:
        result = elixir_agent.respond_in_channel(
            question="much smarter response",
            author_name="Jamie",
            channel_name="#ask-elixir",
            workflow="interactive",
            clan_data={"memberList": [{"name": "Alpha"}]},
            war_data={"season_id": 130},
            conversation_history=[],
            memory_context={},
        )

        assert result["event_type"] == "channel_response"
        user_msg = mock_chat.call_args.args[1]
        assert "lightweight conversational follow-up" in user_msg
        assert "Latest message from 'Jamie' in #ask-elixir: much smarter response" in user_msg
        assert "=== CLAN TREND SUMMARY ===" not in user_msg
        assert "POAP KINGS" not in user_msg


def test_respond_in_channel_uses_clanops_workflow():
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
        )
        assert result is None
        assert mock_chat.call_args.kwargs["workflow"] == "clanops"
        assert mock_chat.call_args.kwargs["allowed_tools"] == elixir_agent.TOOLSETS_BY_WORKFLOW["clanops"]


def _mock_anthropic_response(text="ok", input_tokens=10, output_tokens=20):
    """Build a mock Anthropic Messages response."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def test_create_chat_completion_records_llm_telemetry():
    response = _mock_anthropic_response()
    create = Mock(return_value=response)
    mock_client = SimpleNamespace(
        messages=SimpleNamespace(create=create)
    )
    with (
        patch("agent.core._get_client", return_value=mock_client),
        patch("elixir_agent.runtime_status.record_llm_call") as mock_record,
    ):
        result = elixir_agent._create_chat_completion(
            workflow="interactive",
            messages=[{"role": "user", "content": "status"}],
        )

    assert result.choices[0].message.content == "ok"
    mock_record.assert_called_once()
    assert mock_record.call_args.args[0] == "interactive"
    assert mock_record.call_args.kwargs["ok"] is True
    assert mock_record.call_args.kwargs["total_tokens"] == 30
    assert create.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_create_chat_completion_uses_content_model_for_site_workflows():
    response = _mock_anthropic_response()
    create = Mock(return_value=response)
    mock_client = SimpleNamespace(
        messages=SimpleNamespace(create=create)
    )
    with (
        patch("agent.core._get_client", return_value=mock_client),
        patch("elixir_agent.runtime_status.record_llm_call"),
    ):
        elixir_agent._create_chat_completion(
            workflow="site_home_message",
            messages=[{"role": "user", "content": "status"}],
        )

    assert create.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_create_chat_completion_respects_model_env_overrides():
    response = _mock_anthropic_response()
    create = Mock(return_value=response)
    mock_client = SimpleNamespace(
        messages=SimpleNamespace(create=create)
    )
    with (
        patch("agent.core._get_client", return_value=mock_client),
        patch("elixir_agent.runtime_status.record_llm_call"),
        patch.dict(os.environ, {"ELIXIR_CHAT_MODEL": "claude-test-chat", "ELIXIR_CONTENT_MODEL": "claude-test-content"}),
    ):
        elixir_agent._create_chat_completion(
            workflow="clanops",
            messages=[{"role": "user", "content": "status"}],
        )
        assert create.call_args.kwargs["model"] == "claude-test-chat"

        elixir_agent._create_chat_completion(
            workflow="site_members_message",
            messages=[{"role": "user", "content": "status"}],
        )
        assert create.call_args.kwargs["model"] == "claude-test-content"


def test_chat_with_tools_normalizes_tool_call_messages_for_followup_rounds():
    tool_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="get_current_war_status", arguments="{}"),
    )
    first_message = SimpleNamespace(role="assistant", content=None, tool_calls=[tool_call])
    second_message = SimpleNamespace(
        role="assistant",
        content=json.dumps(
            {
                "event_type": "channel_response",
                "summary": "war answer",
                "content": "We are in war day.",
            }
        ),
        tool_calls=None,
    )
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=first_message)]),
        SimpleNamespace(choices=[SimpleNamespace(message=second_message)]),
    ]

    def fake_create_chat_completion(**kwargs):
        return responses.pop(0)

    with (
        patch("agent.chat._create_chat_completion", side_effect=fake_create_chat_completion),
        patch("agent.chat._execute_tool", return_value=json.dumps({"war_state": "warDay"})),
    ):
        result = elixir_agent._chat_with_tools(
            "system",
            "user",
            workflow="clanops",
            allowed_tools=elixir_agent.TOOLSETS_BY_WORKFLOW["clanops"],
            response_schema=elixir_agent.RESPONSE_SCHEMAS_BY_WORKFLOW["clanops"],
            strict_json=True,
        )

    assert result["event_type"] == "channel_response"
    assert result["content"] == "We are in war day."


def test_chat_with_tools_returns_error_payload_for_invalid_final_json():
    bad_message = SimpleNamespace(role="assistant", content='{"event_type":"channel_response"', tool_calls=None)
    repair_message = SimpleNamespace(role="assistant", content='{"event_type":"channel_response"', tool_calls=None)
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=bad_message)]),
        SimpleNamespace(choices=[SimpleNamespace(message=repair_message)]),
    ]

    def fake_create_chat_completion(**kwargs):
        return responses.pop(0)

    with patch("agent.chat._create_chat_completion", side_effect=fake_create_chat_completion):
        result = elixir_agent._chat_with_tools(
            "system",
            "user",
            workflow="interactive",
            allowed_tools=elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"],
            response_schema=elixir_agent.RESPONSE_SCHEMAS_BY_WORKFLOW["interactive"],
            strict_json=True,
            return_errors=True,
        )

    assert result["_error"]["kind"] == "parse_error"
    assert result["_error"]["phase"] == "repair_response"
    assert "{\"event_type\":\"channel_response\"" in result["_error"]["result_preview"]
