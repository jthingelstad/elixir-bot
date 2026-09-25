"""Phase-1 Elixir MCP routing: builders shape MCP bodies into local
contracts, and every failure path degrades to None (callers fall back
to local tables — member Q&A must survive an Elixir MCP incident)."""

from datetime import datetime, timedelta, timezone

import pytest

import capabilities.mcp_stats as mcp_stats


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def test_trend_block_matches_local_labels(monkeypatch):
    def fake_call(name, arguments=None):
        if name == "players_timeline":
            # 4.0.0 wire: `series`, points keyed `day`, the metrics asked
            # for on the row.
            assert arguments["metrics"] == ["trophies", "best_trophies"]
            return {
                "series": [
                    {
                        "day": _days_ago(13),
                        "trophies": 12400,
                        "best_trophies": 12600,
                    },
                    {
                        "day": _days_ago(8),
                        "trophies": 12450,
                        "best_trophies": 12600,
                    },
                    {
                        "day": _days_ago(6),
                        "trophies": 12480,
                        "best_trophies": 12600,
                    },
                    {
                        "day": _days_ago(0),
                        "trophies": 12510,
                        "best_trophies": 12622,
                    },
                ],
                "meta": {"contract_version": "4.0.0"},
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
    assert "best_trophies 12622" in block
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
        "notes": ["points are per-member contributions; fame belongs to the boat (the clan)."],
        "meta": {"contract_version": "1.0.0"},
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
    # 1.0.0: caveats arrive as notes[]; the old `note` string is gone.
    assert out["notes"] == body["notes"]
    assert "note" not in out


_WAR_WEEKS = [
    {"season_id": 135, "section_index": 4},
    {"season_id": 135, "section_index": 3},
    {"season_id": 135, "section_index": 2},
    {"season_id": 134, "section_index": 4},
    {"season_id": 134, "section_index": 3},
]
_ABSENT = object()


@pytest.mark.parametrize(
    ("member_weeks", "field"),
    [
        (_ABSENT, "member_weeks"),
        (None, "member_weeks"),
        # A current-season week without decks_used, absent or null.
        ([{"season_id": 135, "section_index": 4, "points": 900}], "member_weeks[].decks_used"),
        (
            [{"season_id": 135, "section_index": 4, "points": 900, "decks_used": None}],
            "member_weeks[].decks_used",
        ),
        # A last-four week from the previous season is counted too.
        ([{"season_id": 134, "section_index": 4, "points": 1600}], "member_weeks[].decks_used"),
        # A played week without points.
        ([{"season_id": 135, "section_index": 4, "decks_used": 4}], "member_weeks[].points"),
        (
            [{"season_id": 135, "section_index": 4, "decks_used": 4, "points": None}],
            "member_weeks[].points",
        ),
        ([{"section_index": 4, "points": 900, "decks_used": 4}], "member_weeks[].season_id"),
    ],
)
def test_war_attendance_absent_field_is_unavailable_not_zero(
    monkeypatch, caplog, member_weeks, field
):
    """Jamie 2026-09-25: a missing field reads as UNAVAILABLE, not 0. The
    hub removes an unreliable field as a patch, and `or 0` would have made
    a withdrawn decks_used into a member who played no war decks. None
    sends the caller to the local tables."""
    import logging

    body = {"weeks": _WAR_WEEKS, "notes": [], "meta": {"contract_version": "9.0.1"}}
    if member_weeks is not _ABSENT:
        body["member_weeks"] = member_weeks
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    with caplog.at_level(logging.WARNING, logger="elixir.mcp_stats"):
        assert mcp_stats.war_attendance_via_mcp("#20JJJ2CCRU") is None
    assert f"war_history {field}" in caplog.text
    assert "absent or null for #20JJJ2CCRU" in caplog.text


def test_war_attendance_checks_only_the_rows_it_counts(monkeypatch):
    """An EMPTY member_weeks is a real answer (no race rows: zero played),
    and a null on a week the counts never read (outside the current season
    and the latest four) does not withdraw the answer."""
    body = {"weeks": _WAR_WEEKS, "member_weeks": [], "notes": []}
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    out = mcp_stats.war_attendance_via_mcp("#20JJJ2CCRU")
    assert out["season"]["races_played"] == 0
    assert out["season"]["total_races"] == 3
    assert out["last_4_weeks"]["races_played"] == 0

    body["member_weeks"] = [
        {"season_id": 135, "section_index": 4, "points": 900, "decks_used": 4},
        {"season_id": 134, "section_index": 3, "points": None, "decks_used": None},
    ]
    out = mcp_stats.war_attendance_via_mcp("#20JJJ2CCRU")
    assert out["season"]["races_played"] == 1
    assert out["season"]["total_points"] == 900
    assert out["season"]["total_decks_used"] == 4


def test_attendance_tool_falls_back_to_local_when_decks_used_is_withdrawn(monkeypatch):
    """End to end through the tool: the MCP answer without decks_used is
    unavailable, so get_member_war_detail answers from the local tables
    rather than reporting a member who played nothing."""
    from unittest.mock import patch

    from agent import tool_exec

    body = {
        "weeks": _WAR_WEEKS,
        "member_weeks": [{"season_id": 135, "section_index": 4, "points": 900}],
        "notes": [],
    }
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    local = {"season": {"races_played": 1, "total_races": 3}, "source": "local"}
    with (
        patch.object(tool_exec.db, "get_member_war_attendance", return_value=local) as local_read,
        patch.object(tool_exec, "_enrich_war_player_type"),
        patch.object(tool_exec, "_annotate_roster_status"),
    ):
        out = tool_exec._execute_get_member_war_detail(
            {"member_tag": "#20JJJ2CCRU", "aspect": "attendance"}
        )
    assert out is local
    local_read.assert_called_once_with("#20JJJ2CCRU", season_id=None)


def test_clan_standing_absent_counts_are_unavailable_not_zero(monkeypatch):
    body = {
        "clan_tag": "#J2RGCRVG",
        "members": [{"player_tag": "#20JJJ2CCRU", "rank": 4, "win_rate": 0.55}],
        "notes": [],
    }
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    out = mcp_stats.clan_standing_via_mcp("#20JJJ2CCRU")
    assert out["ranked_members"] is None
    assert out["below_floor_count"] is None
    # No percentile without the denominator.
    assert "percentile" not in out["asker"]


def test_trend_record_does_not_invent_zero_draws(monkeypatch):
    def fake_call(name, arguments=None):
        if name == "players_timeline":
            return {"series": []}
        return {"before": {"wins": 9, "losses": 10}, "after": {"wins": 18, "losses": 11}}

    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", fake_call)
    block = mcp_stats.trend_context_via_mcp("#20JJJ2CCRU")
    assert "record 18-11-None vs 9-10-None" in block
    assert "18-11-0" not in block


def test_clan_standing_marks_asker_with_percentile(monkeypatch):
    body = {
        "clan_tag": "#J2RGCRVG",
        "applied": {
            "clan_tag": "#J2RGCRVG",
            "window": {"from": "2026-08-11T00:00:00.000Z", "to": None, "source": "argument"},
            "days": 30,
            "min_battles": 20,
        },
        "median_win_rate": 0.51,
        "ranked_members": 10,
        "members": [
            {"player_tag": "#AAA", "rank": 1, "win_rate": 0.7},
            {"player_tag": "#20JJJ2CCRU", "rank": 4, "win_rate": 0.55},
        ],
        "below_floor": [{"player_tag": "#Q"}],
        "notes": ["Covers RECORDED battles only."],
        "meta": {"contract_version": "1.0.0"},
    }
    monkeypatch.setattr(mcp_stats.elixir_mcp, "call_tool", lambda name, arguments=None: body)
    out = mcp_stats.clan_standing_via_mcp("#20JJJ2CCRU")
    assert out["asker"]["rank"] == 4
    assert out["asker"]["percentile"] == 0.7
    assert out["ranked_members"] == 10
    assert out["below_floor_count"] == 1
    # 1.0.0: window_days/basis are gone; the echo is applied{} and notes[].
    assert out["window_days"] == 30
    assert out["window"] == {"from": "2026-08-11T00:00:00.000Z", "to": None, "source": "argument"}
    assert out["notes"] == ["Covers RECORDED battles only."]
    assert "basis" not in out and "note" not in out


def test_client_returns_none_without_token(monkeypatch):
    import elixir_mcp

    monkeypatch.delenv("ELIXIR_MCP_TOKEN", raising=False)
    assert elixir_mcp.call_tool("players_summary", {}) is None


def test_served_contract_is_information_never_drift(monkeypatch, caplog):
    """No major pin (hub DECISIONS 2026-09-25: removing an unreliable field
    is a patch, so the major cannot tell this program a field it reads
    went away). Pinned at 6, every process warned "contract drift" once
    the hub moved past 6.x. The version is logged at INFO once per
    distinct value, and no version, major or not, is a warning."""
    import logging

    import elixir_mcp

    monkeypatch.setattr(elixir_mcp, "_contract_seen", None)
    with caplog.at_level(logging.DEBUG, logger="elixir.mcp"):
        for version in ("6.0.0", "9.0.1", "9.0.1", "10.0.0"):
            elixir_mcp._note_contract({"meta": {"contract_version": version}})
        elixir_mcp._note_contract({"meta": {}})
    assert "drift" not in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert caplog.text.count("server contract 9.0.1") == 1
    assert "server contract 6.0.0" in caplog.text
    assert "server contract 10.0.0" in caplog.text


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_tool_error_is_logged_with_code_message_and_argument_keys(monkeypatch, caplog):
    """A refusal must be visible CLIENT-side: the server saw 32 invalid_tag
    refusals from this surface in a week and the bot's own log held none.
    The line carries the code, the message, the argument KEYS and the tag -
    never other values."""
    import json
    import logging

    import elixir_mcp

    body = {
        "error": {"code": "invalid_tag", "message": "Not a Clash Royale tag."},
        "meta": {"contract_version": "1.0.0"},
    }
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(body)}], "isError": True},
    }
    monkeypatch.setenv("ELIXIR_MCP_TOKEN", "svt_test")
    monkeypatch.setattr(elixir_mcp.requests, "post", lambda *a, **k: _FakeResponse(envelope))
    with caplog.at_level(logging.WARNING, logger="elixir.mcp"):
        out = elixir_mcp.call_tool(
            "war_history", {"player_tag": "#OOPS", "seasons": 2, "on_behalf_of": "discord:1"}
        )
    assert out is None
    line = caplog.text
    assert "war_history tool error invalid_tag: Not a Clash Royale tag." in line
    assert "args=['on_behalf_of', 'player_tag', 'seasons']" in line
    assert "player_tag='#OOPS'" in line
    assert "discord:1" not in line


def test_rpc_level_error_is_logged_not_malformed(monkeypatch, caplog):
    import logging

    import elixir_mcp

    envelope = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32029, "message": "Rate limited"}}
    monkeypatch.setenv("ELIXIR_MCP_TOKEN", "svt_test")
    monkeypatch.setattr(elixir_mcp.requests, "post", lambda *a, **k: _FakeResponse(envelope))
    with caplog.at_level(logging.WARNING, logger="elixir.mcp"):
        assert elixir_mcp.call_tool("clans_standings", {"days": 30}) is None
    assert "rpc error -32029: Rate limited" in caplog.text
    assert "malformed" not in caplog.text


def test_hash_prefixed_non_tag_is_resolved_as_a_name_not_sent_as_a_tag():
    """'#King Thing' used to pass straight through _resolve_member_tag and
    reach war_history as player_tag (invalid_tag). Only the CR alphabet is a
    tag; anything else is a name to resolve."""
    from unittest.mock import patch

    from agent import tool_exec

    with patch.object(
        tool_exec.db,
        "resolve_member",
        return_value=[{"player_tag": "#20JJJ2CCRU", "match_score": 1000}],
    ) as resolve:
        assert tool_exec._resolve_member_tag("#King Thing") == "#20JJJ2CCRU"
    resolve.assert_called_once_with("King Thing", limit=5)
    # Real tags still short-circuit, canonicalised, without a lookup.
    with patch.object(tool_exec.db, "resolve_member") as resolve:
        assert tool_exec._resolve_member_tag("#20jjj2ccru") == "#20JJJ2CCRU"
        assert tool_exec._resolve_member_tag("20JJJ2CCRU") == "#20JJJ2CCRU"
    resolve.assert_not_called()
