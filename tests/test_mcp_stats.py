"""Phase-1 Elixir MCP routing: builders shape MCP bodies into local
contracts, and every failure path degrades to None (callers fall back
to local tables — member Q&A must survive an Elixir MCP incident)."""

from datetime import datetime, timedelta, timezone

import capabilities.mcp_stats as mcp_stats


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def test_trend_block_matches_local_labels(monkeypatch):
    def fake_call(name, arguments=None):
        if name == "players_timeline":
            return {
                "series": [
                    {"date": _days_ago(13), "trophies": 12400},
                    {"date": _days_ago(8), "trophies": 12450},
                    {"date": _days_ago(6), "trophies": 12480},
                    {"date": _days_ago(0), "trophies": 12510},
                ],
                "meta": {"contract_version": "0.10.0"},
            }
        if name == "battles_performance":
            return {
                "before": {"battles": 20, "wins": 9, "losses": 10, "draws": 1, "net_trophies": -12},
                "after": {"battles": 30, "wins": 18, "losses": 11, "draws": 1, "net_trophies": 44},
                "meta": {"contract_version": "0.10.0"},
            }
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", fake_call)
    block = mcp_stats.trend_context_via_mcp("#20JJJ2CCRU", days=30, window_days=7)
    assert block is not None
    assert "=== MEMBER TREND SUMMARY ===" in block
    assert "current_7d_vs_previous_7d:" in block
    assert "record 18-11-1 vs 9-10-1" in block
    assert "battle_trophy_delta 44 vs -12" in block
    assert "trophies 30 vs 50" in block  # snapshot deltas, separated from battle deltas
    assert "source: elixir-mcp" in block


def test_trend_falls_to_none_when_any_call_fails(monkeypatch):
    monkeypatch.setattr(
        mcp_stats.elixir_mcp,
        "call_tool",
        lambda name, arguments=None: None,
    )
    assert mcp_stats.trend_context_via_mcp("#20JJJ2CCRU") is None


def test_war_attendance_shapes_and_semantics(monkeypatch):
    body = {
        "weeks": [
            {"season_id": 135, "section_index": 4},
            {"season_id": 135, "section_index": 3},
            {"season_id": 135, "section_index": 2},
            {"season_id": 134, "section_index": 4},
            {"season_id": 134, "section_index": 3},
        ],
        "member_weeks": [
            {"season_id": 135, "section_index": 4, "points": 900, "decks_used": 4},
            {"season_id": 135, "section_index": 3, "points": 0, "decks_used": 0},
            {"season_id": 134, "section_index": 4, "points": 1600, "decks_used": 16},
        ],
        "note": "n",
        "meta": {"contract_version": "0.10.0"},
    }
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    out = mcp_stats.war_attendance_via_mcp("#20JJJ2CCRU")
    assert out["season_id"] == 135
    # decks_used > 0 is the played bar — the 0-deck week does not count.
    assert out["season"]["races_played"] == 1
    assert out["season"]["total_races"] == 3
    assert out["season"]["races_missed"] == 2
    assert out["season"]["total_points"] == 900
    # last 4 recorded weeks include the s134 colosseum (played).
    assert out["last_4_weeks"]["races_played"] == 2
    assert out["last_4_weeks"]["total_races"] == 4
    assert out["source"] == "elixir-mcp"


def test_clan_standing_marks_asker_with_percentile(monkeypatch):
    body = {
        "clan_tag": "#J2RGCRVG",
        "window_days": 30,
        "basis": "b",
        "median_win_rate": 0.51,
        "ranked_members": 10,
        "members": [
            {"player_tag": "#AAA", "rank": 1, "win_rate": 0.7},
            {"player_tag": "#20JJJ2CCRU", "rank": 4, "win_rate": 0.55},
        ],
        "below_floor": [{"player_tag": "#Q"}],
        "note": "n",
        "meta": {"contract_version": "0.10.0"},
    }
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    out = mcp_stats.clan_standing_via_mcp("#20JJJ2CCRU")
    assert out["asker"]["rank"] == 4
    assert out["asker"]["percentile"] == 0.7
    assert out["ranked_members"] == 10
    assert out["below_floor_count"] == 1


def test_client_returns_none_without_token(monkeypatch):
    import elixir_mcp

    monkeypatch.delenv("ELIXIR_MCP_TOKEN", raising=False)
    assert elixir_mcp.call_tool("players_summary", {}) is None
