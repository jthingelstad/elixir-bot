"""Focused tests for agent query tools (consolidated tool layer)."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import elixir_agent


def test_execute_tool_get_clan_roster_list():
    with patch("elixir_agent.db") as mock_db:
        mock_db.list_members.return_value = [{"player_tag": "#ABC123", "member_name": "King Levy"}]
        result = json.loads(elixir_agent._execute_tool("get_clan_roster", {"aspect": "list"}))
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
        mock_db.get_member_recent_form.return_value = {"form": "hot"}

        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "#ABC123"}))

        assert result["profile"]["player_tag"] == "#ABC123"
        assert result["profile"]["member_name"] == "King Levy"
        assert result["profile"]["gold_known"] is False
        mock_db.snapshot_player_profile.assert_called_once()
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
        mock_db.get_member_recent_form.return_value = {"form": "hot"}

        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "#ABC123"}))

        assert result["profile"]["account_age_summary"] == (
            "Derived Clash Royale account age from Years Played badge data: 4 years / 1,474 days"
        )
        assert result["profile"]["recent_activity_summary"] == (
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
        mock_db.get_member_recent_form.return_value = {"form": "hot"}

        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "#ABC123"}))

        assert result["profile"]["current_role_summary"] == "King Thing is currently the clan leader."


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
                "get_member",
                {"member_tag": "#ABC123", "include": ["cards"], "min_level": 14},
            )
        )

        assert result["card_collection"]["summary"]["highest_level"] == 16
        assert result["card_collection"]["gold_known"] is False
        assert "Current gold is not available" in result["card_collection"]["gold_note"]
        mock_db.snapshot_player_profile.assert_called_once()
        mock_db.get_member_card_collection.assert_called_once_with(
            "#ABC123",
            limit=100,
            min_level=14,
            include_support=True,
            rarity=None,
        )


def test_build_tool_result_envelope_strips_card_image_fields_from_context():
    raw = json.dumps(
        {
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

    envelope = json.loads(elixir_agent._build_tool_result_envelope("get_member", raw))

    assert "iconUrls" not in envelope["data"]["current_deck"]["cards"][0]
    assert "icon_url" not in envelope["data"]["signature_cards"]["cards"][0]


def test_build_tool_result_envelope_drops_oversized_lists_instead_of_slicing():
    """Oversized payloads must drop large arrays cleanly, not mid-token slice
    them. The model must still see ok=true (truncation is not a tool failure)
    and a structured marker explaining what was dropped."""
    bulky_card = {"name": "X" * 200, "level": 13, "extra": "Y" * 200}
    raw = json.dumps({
        "summary": {"total_cards": 100, "max_level": 47},
        "card_collection": {
            "cards": [dict(bulky_card, name=f"Card{i:03d}") for i in range(120)],
        },
    })

    envelope = json.loads(elixir_agent._build_tool_result_envelope("get_member", raw))

    # Truncation is a state, not an error.
    assert envelope["ok"] is True
    assert envelope["error"] is None
    assert envelope["truncated"] is True
    # Structured marker replaced the array; data is still a parseable dict.
    cards_field = envelope["data"]["card_collection"]["cards"]
    assert isinstance(cards_field, dict)
    assert cards_field["dropped"] is True
    assert cards_field["original_count"] == 120
    assert "context_size" in cards_field["reason"]
    # Summary is preserved — the model can still answer "how many total cards".
    assert envelope["data"]["summary"]["total_cards"] == 100
    # Meta records what was dropped.
    assert "card_collection.cards" in envelope["meta"]["dropped_fields"]
    assert envelope["meta"]["original_size"] > envelope["meta"]["char_limit"]


def test_build_tool_result_envelope_under_limit_unchanged():
    """Small payloads should pass through untouched."""
    raw = json.dumps({"summary": {"total_cards": 10}, "cards": [{"name": "Knight"}]})
    envelope = json.loads(elixir_agent._build_tool_result_envelope("get_member", raw))
    assert envelope["ok"] is True
    assert envelope["error"] is None
    assert envelope["truncated"] is False
    assert envelope["data"]["cards"] == [{"name": "Knight"}]


def test_interactive_workflow_exposes_all_read_tools():
    interactive_names = {
        tool["name"] for tool in elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"]
    }

    # With consolidated tools, all read tools are visible in interactive;
    # sensitive aspects (at_risk, promotion_candidates) are gated at execution time.
    assert "get_clan_health" in interactive_names
    assert "get_member" in interactive_names


def test_execute_tool_get_member_resolves_handle_before_refresh():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 875}]
        mock_db.get_member_profile.return_value = {"player_tag": "#ABC123", "member_name": "King Levy"}
        mock_db.get_member_recent_form.return_value = {"form": "hot"}

        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "@jamie"}))

        assert result["profile"]["player_tag"] == "#ABC123"
        assert result["profile"]["member_name"] == "King Levy"
        assert result["profile"]["gold_known"] is False
        mock_db.resolve_member.assert_called_once_with("@jamie", limit=5)
        mock_db.get_member_profile.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_cards_accepts_bare_player_tag():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#20JJJ2CCRU", "name": "King Thing"}),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_card_collection.return_value = {"summary": {"highest_level": 16}, "cards": []}

        result = json.loads(elixir_agent._execute_tool(
            "get_member",
            {"member_tag": "20JJJ2CCRU", "include": ["cards"]},
        ))

        assert result["card_collection"]["summary"]["highest_level"] == 16
        mock_db.resolve_member.assert_not_called()
        mock_db.get_member_card_collection.assert_called_once_with(
            "#20JJJ2CCRU",
            limit=100,
            min_level=None,
            include_support=True,
            rarity=None,
        )


def test_execute_tool_get_member_cards_passes_rarity_filter():
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
                "get_member",
                {"member_tag": "20JJJ2CCRU", "include": ["cards"], "rarity": "legendary"},
            )
        )

        assert result["card_collection"]["summary"]["rarity_counts"]["legendary"] == 5
        assert "Royal Ghost" in result["card_collection"]["cards_by_rarity"]["legendary"]
        mock_db.get_member_card_collection.assert_called_once_with(
            "#20JJJ2CCRU",
            limit=100,
            min_level=None,
            include_support=True,
            rarity="legendary",
        )


def test_execute_tool_get_member_chests_uses_cr_api():
    with (
        patch("elixir_agent.cr_api.get_player_chests", return_value=[{"name": "Silver Chest", "index": 1}]) as mock_chests,
        patch("elixir_agent.cr_api.get_player", return_value=None),
        patch("elixir_agent.db") as mock_db,
    ):
        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "#ABC123", "include": ["chests"]}))
        assert result["chests"] == [{"name": "Silver Chest", "index": 1}]
        mock_chests.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_chests_resolves_member_name():
    with (
        patch("elixir_agent.cr_api.get_player_chests", return_value=[{"name": "Silver Chest", "index": 1}]) as mock_chests,
        patch("elixir_agent.cr_api.get_player", return_value=None),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "King Levy", "include": ["chests"]}))
        assert result["chests"] == [{"name": "Silver Chest", "index": 1}]
        mock_db.resolve_member.assert_called_once_with("King Levy", limit=5)
        mock_chests.assert_called_once_with("#ABC123")


def test_execute_tool_get_member_battles_returns_recent_battle_list():
    with (
        patch("elixir_agent.cr_api.get_player", return_value={"tag": "#ABC123", "name": "King Levy"}),
        patch("elixir_agent.cr_api.get_player_battle_log", return_value=[{"type": "PvP"}]),
        patch("elixir_agent.db") as mock_db,
    ):
        mock_db.get_member_recent_battles.return_value = {
            "member_tag": "#ABC123",
            "member_name": "King Levy",
            "scope": "overall_10",
            "count": 1,
            "battles": [
                {
                    "battle_time": "2026-04-24T00:55:02.000Z",
                    "battle_type": "PvP",
                    "game_mode_name": "Ladder",
                    "outcome": "W",
                    "crowns_for": 3,
                    "crowns_against": 0,
                    "opponent_name": "Foo",
                    "opponent_tag": "#XYZ",
                }
            ],
        }
        result = json.loads(elixir_agent._execute_tool(
            "get_member",
            {"member_tag": "#ABC123", "include": ["battles"], "battles_limit": 3},
        ))
        assert result["battles"]["count"] == 1
        assert result["battles"]["battles"][0]["outcome"] == "W"
        mock_db.get_member_recent_battles.assert_called_once_with(
            "#ABC123", scope="overall_10", limit=3,
        )
        # battles include must trigger battlelog cache refresh
        mock_db.snapshot_player_battlelog.assert_called_once()


def test_execute_tool_get_war_season_summary_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_season_summary.return_value = {"season_id": 129, "races": 4}
        result = json.loads(elixir_agent._execute_tool("get_war_season", {"aspect": "summary"}))
        assert result == {"season_id": 129, "races": 4}
        mock_db.get_war_season_summary.assert_called_once_with(season_id=None, top_n=10)


def test_execute_tool_get_clan_roster_max_cards_and_clan_health_hot_streaks():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_members_with_most_level_16_cards.return_value = [{"tag": "#ABC123", "level_16_count": 42}]
        mock_db.get_members_on_hot_streak.return_value = [{"tag": "#ABC123", "current_streak": 6}]

        elite = json.loads(elixir_agent._execute_tool("get_clan_roster", {"aspect": "max_cards", "limit": 5}))
        hot = json.loads(elixir_agent._execute_tool("get_clan_health", {"aspect": "hot_streaks", "min_streak": 5}))

        assert elite == [{"tag": "#ABC123", "level_16_count": 42}]
        assert hot == [{"tag": "#ABC123", "current_streak": 6}]
        mock_db.get_members_with_most_level_16_cards.assert_called_once_with(limit=5)
        mock_db.get_members_on_hot_streak.assert_called_once_with(min_streak=5, scope="ladder_ranked_10")


def test_execute_tool_get_clan_trends_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.compare_clan_trend_windows.return_value = {"clan": {"clan_tag": "#J2RGCRVG"}, "window_days": 14}
        mock_db.build_clan_trend_summary_context.return_value = "=== CLAN TREND SUMMARY ==="

        result = json.loads(elixir_agent._execute_tool("get_clan_trends", {"window_days": 14, "days": 21}))

        assert result["clan"]["clan_tag"] == "#J2RGCRVG"
        assert result["window_days"] == 14
        assert result["trend_summary"] == "=== CLAN TREND SUMMARY ==="
        mock_db.compare_clan_trend_windows.assert_called_once_with(window_days=14)
        mock_db.build_clan_trend_summary_context.assert_called_once_with(days=21, window_days=14)


def test_execute_tool_get_war_season_trending_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_trending_war_contributors.return_value = {"season_id": 129, "members": []}
        result = json.loads(elixir_agent._execute_tool("get_war_season", {"aspect": "trending"}))
        assert result == {"season_id": 129, "members": []}
        mock_db.get_trending_war_contributors.assert_called_once_with(season_id=None, recent_races=2, limit=10)


def _mock_war_player_type_conn():
    """Create a mock connection that satisfies _war_player_type queries."""
    mock_conn = Mock()
    # member_id lookup
    member_row = {"member_id": 1}
    # _war_player_type query result
    war_type_row = {"total_races": 10, "races_played": 8}
    mock_conn.execute.return_value.fetchone.side_effect = [member_row, war_type_row]
    return mock_conn


def test_execute_tool_get_member_war_detail_vs_clan_avg():
    with (
        patch("elixir_agent.db") as mock_db,
        patch("db.get_connection") as mock_conn_fn,
    ):
        mock_db.compare_member_war_to_clan_average.return_value = {"member": {"tag": "#ABC123"}}
        mock_conn_fn.return_value = _mock_war_player_type_conn()

        result = json.loads(
            elixir_agent._execute_tool(
                "get_member_war_detail",
                {"member_tag": "#ABC123", "aspect": "vs_clan_avg"},
            )
        )
        assert result["member"]["tag"] == "#ABC123"
        assert result["war_player_type"] == "regular"
        mock_db.compare_member_war_to_clan_average.assert_called_once_with("#ABC123", season_id=None)


def test_resolve_member_tag_returns_clear_ambiguity_error():
    with patch("elixir_agent.db") as mock_db:
        mock_db.resolve_member.return_value = [
            {"player_tag": "#ABC123", "match_score": 650, "member_ref_with_handle": "King Levy (@jamie)"},
            {"player_tag": "#DEF456", "match_score": 625, "member_ref_with_handle": "King Levi (@levi)"},
        ]
        mock_db.get_member_history.return_value = {"history": []}
        result = json.loads(elixir_agent._execute_tool("get_member", {"member_tag": "King Lev", "include": ["history"]}))
        assert "error" in result
        assert "Ambiguous member reference" in result["error"]


def test_execute_tool_get_clan_health_at_risk_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_members_at_risk.return_value = {"members": []}
        result = json.loads(
            elixir_agent._execute_tool(
                "get_clan_health",
                {"aspect": "at_risk", "season_id": 129},
                workflow="clanops",
            )
        )
        assert result == {"members": []}
        mock_db.get_members_at_risk.assert_called_once_with(
            inactivity_days=7,
            min_donations_week=20,
            require_war_participation=False,
            min_war_races=1,
            season_id=129,
        )


def test_execute_tool_get_clan_health_sensitive_aspect_blocked_in_interactive():
    result = json.loads(
        elixir_agent._execute_tool(
            "get_clan_health",
            {"aspect": "at_risk"},
            workflow="interactive",
        )
    )
    assert "error" in result
    assert "leadership channels" in result["error"]


def test_execute_tool_get_river_race_standings():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_current_war_status.return_value = {
            "war_state": "full",
            "season_id": 129,
            "race_rank": 1,
            "race_standings": [{"rank": 1, "clan_name": "POAP KINGS", "fame": 5000, "is_us": True}],
            "season_week_label": "Season 129 Week 1",
            "period_type": "warDay",
            "trophy_change": -20,
            "trophy_stakes_known": True,
            "final_battle_day_active": False,
            "final_practice_day_active": False,
            "trophy_stakes_text": "20 trophies",
        }
        mock_db.is_colosseum_week_confirmed.return_value = False
        result = json.loads(elixir_agent._execute_tool("get_river_race", {}))
        assert result["race_standings"][0]["clan_name"] == "POAP KINGS"
        assert result["race_rank"] == 1
        assert result["trophy_stakes_text"] == "20 trophies"
        assert result["is_colosseum_week"] is False
        assert result["is_final_battle_day"] is False
        assert result["is_final_practice_day"] is False
        mock_db.get_current_war_status.assert_called_once()


def test_execute_tool_get_river_race_engagement():
    with patch("elixir_agent.db") as mock_db:
        mock_db.build_war_now_context.return_value = (
            {
                "season_id": 129,
                "week": 1,
                "phase": "battle",
                "phase_display": "Battle Day 1",
                "day_number": 1,
                "day_total": 4,
                "period_type": "warDay",
                "time_left_seconds": 12000,
                "time_left_text": "3h 20m",
                "period_started_at": "2026-03-05T10:00:00Z",
                "period_ends_at": "2026-03-06T10:00:00Z",
                "is_colosseum_week": False,
                "is_final_battle_day": False,
                "is_final_practice_day": False,
                "race_standings": [],
                "now_text": "=== RIVER RACE — CURRENT MOMENT ===\nSeason 129 · Week 1 · Battle Day 1 of 4\nPeriod ends in 3h 20m",
            },
            "=== RIVER RACE — CURRENT MOMENT ===\nSeason 129 · Week 1 · Battle Day 1 of 4\nPeriod ends in 3h 20m",
        )
        mock_db.get_current_war_day_state.return_value = {
            "war_day_key": "s00129-w01-p010",
            "clan_fame": 5000,
            "total_participants": 40,
            "engaged_count": 30,
            "finished_count": 20,
            "untouched_count": 10,
        }
        result = json.loads(elixir_agent._execute_tool("get_river_race", {"aspect": "engagement"}))
        assert result["phase_display"] == "Battle Day 1"
        assert result["time_left_text"] == "3h 20m"
        assert result["engaged_count"] == 30
        assert result["untouched_count"] == 10
        assert "RIVER RACE — CURRENT MOMENT" in result["now_text"]
        mock_db.build_war_now_context.assert_called_once()


def test_execute_tool_get_member_war_detail_attendance_resolves_member():
    with (
        patch("elixir_agent.db") as mock_db,
        patch("db.get_connection") as mock_conn_fn,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        mock_db.get_member_war_attendance.return_value = {"season": {"participation_rate": 1.0}}
        mock_conn_fn.return_value = _mock_war_player_type_conn()

        result = json.loads(elixir_agent._execute_tool(
            "get_member_war_detail",
            {"member_tag": "King Levy", "aspect": "attendance"},
        ))
        assert result["season"]["participation_rate"] == 1.0
        assert result["war_player_type"] == "regular"
        mock_db.get_member_war_attendance.assert_called_once_with("#ABC123", season_id=None)


def test_execute_tool_get_war_season_win_rates_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_war_battle_win_rates.return_value = {"season_id": 129, "members": []}
        result = json.loads(elixir_agent._execute_tool("get_war_season", {"aspect": "win_rates", "season_id": 129, "limit": 5}))
        assert result == {"season_id": 129, "members": []}
        mock_db.get_war_battle_win_rates.assert_called_once_with(season_id=129, limit=5, min_battles=1)


def test_execute_tool_get_clan_roster_role_changes_uses_db():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_recent_role_changes.return_value = [{"tag": "#ABC123", "old_role": "member", "new_role": "elder"}]
        result = json.loads(elixir_agent._execute_tool("get_clan_roster", {"aspect": "role_changes", "days": 14}))
        assert result[0]["new_role"] == "elder"
        mock_db.get_recent_role_changes.assert_called_once_with(days=14)


def test_execute_tool_get_war_season_boat_battles_and_trends():
    with patch("elixir_agent.db") as mock_db:
        mock_db.get_clan_boat_battle_record.return_value = {"wins": 2, "losses": 1}
        mock_db.get_war_score_trend.return_value = {"direction": "up", "score_change": 30}
        mock_db.compare_fame_per_member_to_previous_season.return_value = {"direction": "up", "delta": 120.0}

        boat = json.loads(elixir_agent._execute_tool("get_war_season", {"aspect": "boat_battles"}))
        trend = json.loads(elixir_agent._execute_tool("get_war_season", {"aspect": "score_trend"}))
        fame = json.loads(elixir_agent._execute_tool("get_war_season", {"aspect": "season_comparison", "season_id": 129}))

        assert boat == {"wins": 2, "losses": 1}
        assert trend["direction"] == "up"
        assert fame["delta"] == 120.0
        mock_db.get_clan_boat_battle_record.assert_called_once_with(wars=3)
        mock_db.get_war_score_trend.assert_called_once_with(days=30)
        mock_db.compare_fame_per_member_to_previous_season.assert_called_once_with(season_id=129)


def test_execute_tool_get_member_war_detail_missed_days():
    with (
        patch("elixir_agent.db") as mock_db,
        patch("db.get_connection") as mock_conn_fn,
    ):
        mock_db.resolve_member.return_value = [{"player_tag": "#ABC123", "match_score": 950}]
        mock_db.get_member_missed_war_days.return_value = {"days_missed": 1}
        mock_conn_fn.return_value = _mock_war_player_type_conn()

        result = json.loads(elixir_agent._execute_tool(
            "get_member_war_detail",
            {"member_tag": "@jamie", "aspect": "missed_days"},
        ))
        assert result["days_missed"] == 1
        assert result["war_player_type"] == "regular"
        mock_db.get_member_missed_war_days.assert_called_once_with("#ABC123", season_id=None)


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
    with (
        patch("elixir_agent._chat_with_tools", return_value=None) as mock_chat,
        patch("agent.workflows.db.build_war_now_context", return_value=(None, "")),
    ):
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


def test_respond_in_channel_injects_war_context_for_war_talk():
    """War context should be injected for #war-talk channel."""
    with (
        patch("elixir_agent._chat_with_tools", return_value={"event_type": "channel_response", "content": "ok"}) as mock_chat,
        patch("agent.workflows.db.build_clan_trend_summary_context", return_value="trends"),
        patch("agent.workflows.db.build_war_now_context", return_value=(
            {
                "season_id": 129,
                "week": 3,
                "phase": "battle",
                "phase_display": "Battle Day 2",
                "day_number": 2,
                "day_total": 4,
                "time_left_text": "12h 30m",
                "is_colosseum_week": False,
                "is_final_battle_day": False,
                "is_final_practice_day": False,
                "race_standings": [
                    {"rank": 1, "clan_name": "POAP KINGS", "fame": 12000, "is_us": True},
                    {"rank": 2, "clan_name": "Dragon Riders", "fame": 11000, "is_us": False},
                ],
            },
            "=== RIVER RACE — CURRENT MOMENT ===\n"
            "Season 129 · Week 3 · Battle Day 2 of 4\n"
            "Period ends in 12h 30m\n"
            "Race standings:\n"
            "  1. POAP KINGS (us) | 12,000 fame\n"
            "  2. Dragon Riders | 11,000 fame",
        )),
    ):
        elixir_agent.respond_in_channel(
            question="How's the race going?",
            author_name="Jamie",
            channel_name="#war-talk",
            workflow="interactive",
            clan_data={"memberList": []},
            war_data={"state": "warDay"},
            conversation_history=[],
            memory_context={},
        )
        user_msg = mock_chat.call_args.args[1]
        assert "RIVER RACE — CURRENT MOMENT" in user_msg
        assert "POAP KINGS" in user_msg
        assert "Dragon Riders" in user_msg


def test_respond_in_channel_omits_war_context_for_non_war_question():
    """War context should NOT be injected for non-war questions in #ask-elixir."""
    with (
        patch("elixir_agent._chat_with_tools", return_value={"event_type": "channel_response", "content": "ok"}) as mock_chat,
        patch("agent.workflows.db.build_clan_trend_summary_context", return_value="trends"),
    ):
        elixir_agent.respond_in_channel(
            question="What's a good Hog Rider deck?",
            author_name="Jamie",
            channel_name="#ask-elixir",
            workflow="interactive",
            clan_data={"memberList": []},
            war_data={"state": "warDay"},
            conversation_history=[],
            memory_context={},
        )
        user_msg = mock_chat.call_args.args[1]
        assert "RIVER RACE — CURRENT MOMENT" not in user_msg
        assert "=== WAR DECKS TODAY ===" not in user_msg


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
    assert create.call_args.kwargs["model"] == "claude-haiku-4-5-20251001"


def test_create_chat_completion_uses_sonnet_for_long_form_workflows():
    response = _mock_anthropic_response()
    create = Mock(return_value=response)
    mock_client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with (
        patch("agent.core._get_client", return_value=mock_client),
        patch("elixir_agent.runtime_status.record_llm_call"),
    ):
        for workflow in ("weekly_digest", "tournament_recap", "intel_report", "memory_synthesis"):
            elixir_agent._create_chat_completion(
                workflow=workflow,
                messages=[{"role": "user", "content": "status"}],
            )
            assert create.call_args.kwargs["model"] == "claude-sonnet-4-6", workflow


def test_generate_tournament_recap_uses_agent_loop_with_cr_api():
    captured = {}

    def fake_chat_with_tools(system_prompt, user_message, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_message"] = user_message
        captured["kwargs"] = kwargs
        return {"content": "The winner stood alone.  "}

    with patch("agent.workflows._chat_with_tools", side_effect=fake_chat_with_tools):
        text = elixir_agent.generate_tournament_recap("tournament context goes here")

    assert text == "The winner stood alone."
    assert captured["kwargs"]["workflow"] == "tournament_recap"
    tool_names = {t["name"] for t in captured["kwargs"]["allowed_tools"]}
    assert tool_names == {"cr_api"}
    assert captured["kwargs"]["response_schema"] == {"required": ["content"]}
    assert captured["kwargs"]["strict_json"] is True
    assert "tournament context goes here" in captured["user_message"]


def test_generate_tournament_recap_returns_none_on_empty_parse():
    with patch("agent.workflows._chat_with_tools", return_value=None):
        assert elixir_agent.generate_tournament_recap("ctx") is None
    with patch("agent.workflows._chat_with_tools", return_value={"content": ""}):
        assert elixir_agent.generate_tournament_recap("ctx") is None
    with patch("agent.workflows._chat_with_tools", return_value={"content": 123}):
        assert elixir_agent.generate_tournament_recap("ctx") is None


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

    assert create.call_args.kwargs["model"] == "claude-haiku-4-5-20251001"


def test_create_chat_completion_respects_model_env_overrides():
    response = _mock_anthropic_response()
    create = Mock(return_value=response)
    mock_client = SimpleNamespace(
        messages=SimpleNamespace(create=create)
    )
    with (
        patch("agent.core._get_client", return_value=mock_client),
        patch("elixir_agent.runtime_status.record_llm_call"),
        patch.dict(os.environ, {"ELIXIR_CHAT_MODEL": "claude-test-chat", "ELIXIR_LIGHTWEIGHT_MODEL": "claude-test-lightweight"}),
    ):
        elixir_agent._create_chat_completion(
            workflow="weekly_digest",
            messages=[{"role": "user", "content": "status"}],
        )
        assert create.call_args.kwargs["model"] == "claude-test-chat"

        elixir_agent._create_chat_completion(
            workflow="clanops",
            messages=[{"role": "user", "content": "status"}],
        )
        assert create.call_args.kwargs["model"] == "claude-test-lightweight"


def test_chat_with_tools_normalizes_tool_call_messages_for_followup_rounds():
    tool_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="get_river_race", arguments="{}"),
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


def test_chat_with_tools_returns_truncation_when_initial_response_hits_max_tokens(caplog):
    truncated_message = SimpleNamespace(
        role="assistant",
        content='{"event_type":"channel_response","summary":"deck","content":"This deck has',
        tool_calls=None,
    )
    truncated_choice = SimpleNamespace(message=truncated_message, stop_reason="max_tokens")
    responses = [SimpleNamespace(choices=[truncated_choice])]

    def fake_create_chat_completion(**kwargs):
        return responses.pop(0)

    with patch("agent.chat._create_chat_completion", side_effect=fake_create_chat_completion):
        with caplog.at_level("WARNING", logger="elixir_agent"):
            result = elixir_agent._chat_with_tools(
                "system",
                "user",
                workflow="interactive",
                allowed_tools=elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"],
                response_schema=elixir_agent.RESPONSE_SCHEMAS_BY_WORKFLOW["interactive"],
                strict_json=True,
                return_errors=True,
            )

    assert result["_error"]["kind"] == "truncation"
    assert result["_error"]["phase"] == "initial_response"
    assert "max_tokens" in result["_error"]["detail"]
    assert any("llm_truncated" in rec.message for rec in caplog.records)


def test_chat_with_tools_returns_empty_response_after_max_tool_rounds(caplog):
    tool_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="get_river_race", arguments="{}"),
    )
    tool_message = SimpleNamespace(role="assistant", content=None, tool_calls=[tool_call])
    tool_choice = SimpleNamespace(message=tool_message, stop_reason="tool_use")
    # interactive max_tool_rounds=4 → loop runs 5 iterations of tool calls, then 1 final call
    tool_responses = [SimpleNamespace(choices=[tool_choice]) for _ in range(5)]
    empty_message = SimpleNamespace(role="assistant", content="", tool_calls=None)
    empty_choice = SimpleNamespace(message=empty_message, stop_reason="end_turn")
    responses = tool_responses + [SimpleNamespace(choices=[empty_choice])]

    captured_messages = []

    def fake_create_chat_completion(**kwargs):
        captured_messages.append([dict(m) for m in kwargs["messages"]])
        return responses.pop(0)

    with (
        patch("agent.chat._create_chat_completion", side_effect=fake_create_chat_completion),
        patch("agent.chat._execute_tool", return_value=json.dumps({"war_state": "warDay"})),
    ):
        with caplog.at_level("WARNING", logger="elixir_agent"):
            result = elixir_agent._chat_with_tools(
                "system",
                "user",
                workflow="interactive",
                allowed_tools=elixir_agent.TOOLSETS_BY_WORKFLOW["interactive"],
                response_schema=elixir_agent.RESPONSE_SCHEMAS_BY_WORKFLOW["interactive"],
                strict_json=True,
                return_errors=True,
            )

    assert result["_error"]["kind"] == "empty_response"
    assert result["_error"]["phase"] == "final_response"
    assert any("empty_final_response" in rec.message for rec in caplog.records)
    # Verify the explicit nudge was appended before the final call
    final_call_messages = captured_messages[-1]
    assert any(
        m["role"] == "user" and "Do not request any more tools" in m.get("content", "")
        for m in final_call_messages
    )


def test_execute_tool_update_member_birthday():
    with patch("elixir_agent.db") as mock_db:
        result = json.loads(
            elixir_agent._execute_tool(
                "update_member",
                {"member_tag": "#ABC123", "field": "birthday", "value": {"month": 3, "day": 15}},
            )
        )
        assert result["success"] is True
        assert result["field"] == "birthday"
        mock_db.set_member_birthday.assert_called_once_with("#ABC123", name=None, month=3, day=15)


def test_execute_tool_update_member_note():
    with patch("elixir_agent.db") as mock_db:
        result = json.loads(
            elixir_agent._execute_tool(
                "update_member",
                {"member_tag": "#ABC123", "field": "note", "value": "War Machine"},
            )
        )
        assert result["success"] is True
        mock_db.set_member_note.assert_called_once_with("#ABC123", name=None, note="War Machine")
