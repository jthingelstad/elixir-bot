"""Tests for the unified cr_api LLM tool (_execute_cr_api + filters + cap)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import cr_api


@pytest.fixture(autouse=True)
def _clear_cr_api_cache():
    cr_api._cache_clear()
    yield
    cr_api._cache_clear()


# ---------------------------------------------------------------------------
# _normalize_cr_tag
# ---------------------------------------------------------------------------

class TestNormalizeTag:
    def test_strips_hash_and_uppercases(self):
        assert cr_api._normalize_cr_tag("#j2rgcrvg") == "J2RGCRVG"
        assert cr_api._normalize_cr_tag("  J2RGCRVG  ") == "J2RGCRVG"
        assert cr_api._normalize_cr_tag("pylq2") == "PYLQ2"

    def test_rejects_invalid_chars(self):
        with pytest.raises(cr_api.InvalidTagError):
            cr_api._normalize_cr_tag("#BATTLE123")
        with pytest.raises(cr_api.InvalidTagError):
            cr_api._normalize_cr_tag("#!@$")

    def test_rejects_empty(self):
        with pytest.raises(cr_api.InvalidTagError):
            cr_api._normalize_cr_tag(None)
        with pytest.raises(cr_api.InvalidTagError):
            cr_api._normalize_cr_tag("")
        with pytest.raises(cr_api.InvalidTagError):
            cr_api._normalize_cr_tag("   #   ")

    def test_permissive_strip_tag_accepts_anything_nonempty(self):
        # _strip_tag is the internal API-fetcher helper; it must NOT validate.
        assert cr_api._strip_tag("#ABC123") == "ABC123"
        assert cr_api._strip_tag("xyz") == "XYZ"
        with pytest.raises(cr_api.InvalidTagError):
            cr_api._strip_tag("")


# ---------------------------------------------------------------------------
# TTL cache behavior
# ---------------------------------------------------------------------------

class TestTTLCache:
    def test_cache_hit_skips_second_fetch(self):
        with patch("cr_api._request_json", return_value={"tag": "#PYLQ2"}) as mock_req:
            first = cr_api.get_player("#PYLQ2")
            second = cr_api.get_player("#PYLQ2")
            assert first == second == {"tag": "#PYLQ2"}
            assert mock_req.call_count == 1

    def test_cache_expires_by_ttl(self):
        with patch("cr_api._request_json", return_value={"tag": "#PYLQ2"}) as mock_req:
            cr_api.get_player("#PYLQ2")
            # Expire by rewinding the stored expires_at.
            (endpoint, tag), (_, payload) = next(iter(cr_api._TTL_CACHE.items()))
            cr_api._TTL_CACHE[(endpoint, tag)] = (0.0, payload)
            cr_api.get_player("#PYLQ2")
            assert mock_req.call_count == 2

    def test_cache_keyed_by_endpoint_and_tag(self):
        with patch("cr_api._request_json", return_value={"ok": True}) as mock_req:
            cr_api.get_player("#PYLQ2")
            cr_api.get_clan_by_tag("#PYLQ2")  # different endpoint, same tag
            cr_api.get_player("#J2RGCRVG")    # same endpoint, different tag
            assert mock_req.call_count == 3


# ---------------------------------------------------------------------------
# _execute_cr_api dispatch + validation
# ---------------------------------------------------------------------------

@pytest.fixture
def execute_cr_api():
    """Import lazily — agent.tool_exec requires the full agent package init."""
    from agent import app  # noqa: F401 — triggers circular-import resolution
    from agent.tool_exec import _execute_cr_api
    return _execute_cr_api


class TestExecuteCrApiDispatch:
    def test_missing_aspect(self, execute_cr_api):
        assert execute_cr_api({"tag": "#PYLQ2"}) == {"error": "aspect is required"}

    def test_missing_tag(self, execute_cr_api):
        result = execute_cr_api({"aspect": "player"})
        assert result["error"] == "invalid_tag"

    def test_invalid_tag(self, execute_cr_api):
        result = execute_cr_api({"aspect": "player", "tag": "#BATTLE123"})
        assert result["error"] == "invalid_tag"

    def test_unknown_aspect(self, execute_cr_api):
        result = execute_cr_api({"aspect": "bogus", "tag": "#PYLQ2"})
        assert "Unknown aspect" in result["error"]

    def test_rejects_our_clan_for_clan_aspect(self, execute_cr_api):
        result = execute_cr_api({"aspect": "clan", "tag": f"#{cr_api.CLAN_TAG}"})
        assert result["error"] == "our_clan_use_local_tools"

    def test_rejects_our_clan_for_clan_members_aspect(self, execute_cr_api):
        result = execute_cr_api({"aspect": "clan_members", "tag": f"#{cr_api.CLAN_TAG}"})
        assert result["error"] == "our_clan_use_local_tools"

    def test_player_not_found_returns_structured_error(self, execute_cr_api):
        with patch("cr_api.get_player", return_value=None):
            result = execute_cr_api({"aspect": "player", "tag": "#PYLQ2"})
            assert result == {"error": "not_found_or_unavailable"}


# ---------------------------------------------------------------------------
# Filters — envelope size + field whitelisting
# ---------------------------------------------------------------------------

_ENVELOPE_BUDGET_BYTES = 8000


def _json_size(obj):
    return len(json.dumps(obj, default=str))


_FAKE_PLAYER = {
    "name": "King Levy", "tag": "#PYLQ2",
    "expLevel": 13, "trophies": 5432, "bestTrophies": 6000,
    "wins": 100, "losses": 50, "battleCount": 200, "threeCrownWins": 60,
    "donations": 80, "donationsReceived": 120, "role": "elder",
    "clan": {"tag": "#J2RGCRVG", "name": "POAP KINGS", "badgeId": 1},
    "arena": {"name": "Royal Crypt", "id": 54000000},
    "currentFavouriteCard": {"name": "Mega Knight", "id": 12345, "iconUrls": {"medium": "http://x"}},
    "currentDeck": [
        {"name": f"Card{i}", "level": 14, "maxLevel": 14, "elixirCost": 3, "iconUrls": {"medium": "http://x"}}
        for i in range(8)
    ],
    "achievements": [{"name": f"Ach{i}", "stars": 3, "progress": 999} for i in range(12)],
    "cards": [{"name": f"Collection{i}", "level": 14} for i in range(120)],  # should be filtered
    "badges": [{"name": f"Badge{i}"} for i in range(60)],  # filtered
}


class TestFilters:
    def test_player_filter_envelope_and_whitelist(self, execute_cr_api):
        with patch("cr_api.get_player", return_value=_FAKE_PLAYER):
            r = execute_cr_api({"aspect": "player", "tag": "#PYLQ2"})
        assert _json_size(r) < _ENVELOPE_BUDGET_BYTES
        assert r["name"] == "King Levy"
        assert r["tag"] == "#PYLQ2"
        assert r["clan"] == {"tag": "#J2RGCRVG", "name": "POAP KINGS"}
        assert r["currentFavouriteCard"] == "Mega Knight"
        assert "cards" not in r
        assert "badges" not in r
        # Deck trimmed to name/level/maxLevel/elixirCost only.
        assert set(r["currentDeck"][0].keys()) == {"name", "level", "maxLevel", "elixirCost"}

    def test_player_battles_filter_respects_limit(self, execute_cr_api):
        battles = [
            {
                "type": "PvP", "battleTime": f"2026040{i:02d}T000000.000Z",
                "gameMode": {"name": "Ladder"}, "arena": {"name": "Arena"},
                "team": [{"tag": "#T1", "name": "us", "crowns": 2, "trophyChange": 30, "clan": {"tag": "#C1", "name": "Us"}}],
                "opponent": [{"tag": "#O1", "name": "them", "crowns": 1, "clan": {"tag": "#CO", "name": "OppClan"}}],
                "deck": [{"name": "x"}] * 8,  # should be stripped
            }
            for i in range(25)
        ]
        with patch("cr_api.get_player_battle_log", return_value=battles):
            r = execute_cr_api({"aspect": "player_battles", "tag": "#PYLQ2", "limit": 5})
        assert r["count"] == 5
        assert len(r["battles"]) == 5
        assert "deck" not in r["battles"][0]
        assert r["battles"][0]["opponent"][0]["tag"] == "#O1"

    def test_player_battles_mode_filter(self, execute_cr_api):
        battles = [
            {"type": "PvP", "gameMode": {"name": "Ladder"}, "team": [], "opponent": []},
            {"type": "riverRacePvP", "gameMode": {"name": "War"}, "team": [], "opponent": []},
            {"type": "pathOfLegend", "gameMode": {"name": "PoL"}, "team": [], "opponent": []},
        ]
        with patch("cr_api.get_player_battle_log", return_value=battles):
            r = execute_cr_api({"aspect": "player_battles", "tag": "#PYLQ2", "mode": "war"})
        assert r["count"] == 1
        assert r["battles"][0]["type"] == "riverRacePvP"

    def test_clan_filter_truncates_description_and_summarizes(self, execute_cr_api):
        payload = {
            "name": "Some Clan", "tag": "#PYLQ2",
            "description": "x" * 800,
            "type": "open", "clanScore": 5000, "clanWarTrophies": 7000,
            "requiredTrophies": 6000, "members": 50,
            "location": {"name": "US"}, "badgeId": 42,
            "memberList": [
                {"role": "leader", "trophies": 9000, "donations": 200},
                {"role": "coLeader", "trophies": 8000, "donations": 100},
                {"role": "elder", "trophies": 7000, "donations": 50},
                {"role": "member", "trophies": 6000, "donations": 10},
            ],
        }
        with patch("cr_api.get_clan_by_tag", return_value=payload):
            r = execute_cr_api({"aspect": "clan", "tag": "#PYLQ2"})
        assert _json_size(r) < _ENVELOPE_BUDGET_BYTES
        assert len(r["description"]) <= 503  # 500 + "..."
        assert r["members_summary"]["total_members"] == 4
        assert r["members_summary"]["role_counts"]["leader"] == 1
        assert r["members_summary"]["total_donations_week"] == 360
        assert "memberList" not in r

    def test_clan_members_returns_top_n_by_trophies(self, execute_cr_api):
        payload = {
            "name": "Some Clan", "tag": "#PYLQ2",
            "memberList": [
                {"tag": f"#M{i}", "name": f"m{i}", "role": "member", "trophies": 1000 + i,
                 "expLevel": 50, "donations": 10, "donationsReceived": 5, "lastSeen": "2026",
                 "clanRank": 50 - i}
                for i in range(50)
            ],
        }
        with patch("cr_api.get_clan_by_tag", return_value=payload):
            r = execute_cr_api({"aspect": "clan_members", "tag": "#PYLQ2", "limit": 10})
        assert r["total_members"] == 50
        assert r["members_returned"] == 10
        # Sorted by trophies desc: #M49 first.
        assert r["members"][0]["tag"] == "#M49"

    def test_clan_war_summarizes_all_clans_and_top_participants(self, execute_cr_api):
        payload = {
            "state": "warDay", "sectionIndex": 1, "periodIndex": 3, "periodType": "warDay",
            "clans": [
                {"tag": f"#C{i}", "name": f"c{i}", "fame": 1000 * i, "repairPoints": 0,
                 "participants": [
                     {"tag": f"#P{i}{j}", "name": f"p{j}", "fame": 400 + j, "decksUsed": 4}
                     for j in range(30)
                 ]}
                for i in range(5)
            ],
        }
        with patch("cr_api.get_current_war", return_value=payload):
            r = execute_cr_api({"aspect": "clan_war", "tag": "#PYLQ2"})
        assert len(r["clans"]) == 5
        assert len(r["top_participants"]) == 5
        # Sorted desc by fame.
        fames = [p["fame"] for p in r["top_participants"]]
        assert fames == sorted(fames, reverse=True)

    def test_clan_war_log_extracts_focal_clan_rank(self, execute_cr_api):
        # We query #PYLQ2; each log entry has standings with 5 clans. Our rank
        # should be extracted from the entry where clan.tag == "#PYLQ2".
        payload = {
            "items": [
                {"seasonId": 42, "sectionIndex": 3, "createdDate": "2026",
                 "standings": [
                     {"rank": 2, "clan": {"tag": "#PYLQ2", "name": "them", "fame": 3000}},
                     {"rank": 1, "clan": {"tag": "#OTHER", "fame": 4000}},
                 ]},
            ],
        }
        with patch("cr_api.get_river_race_log", return_value=payload):
            r = execute_cr_api({"aspect": "clan_war_log", "tag": "#PYLQ2"})
        assert r["races"][0]["finishRank"] == 2
        assert r["races"][0]["fame"] == 3000

    def test_tournament_filter(self, execute_cr_api):
        payload = {
            "tag": "#PYLQ2", "name": "Test Cup", "description": "y" * 900,
            "type": "open", "status": "inProgress",
            "firstPlaceCardPrize": 25000, "maxCapacity": 1000, "levelCap": 11,
            "preparationDuration": 3600, "duration": 7200,
            "membersCount": 50,
            "membersList": [
                {"tag": f"#T{i}", "name": f"t{i}", "score": 100 - i, "rank": i + 1,
                 "previousRank": i + 2, "clan": {"tag": "#C1", "name": "Clan1"}}
                for i in range(40)
            ],
        }
        with patch("cr_api.get_tournament", return_value=payload):
            r = execute_cr_api({"aspect": "tournament", "tag": "#PYLQ2", "limit": 10})
        assert _json_size(r) < _ENVELOPE_BUDGET_BYTES
        assert r["members_returned"] == 10
        assert r["members"][0]["tag"] == "#T0"  # highest score
        assert len(r["description"]) <= 503


# ---------------------------------------------------------------------------
# Per-turn cap is enforced at the chat.py dispatch layer.
# Verified separately in test_chat_cap.
# ---------------------------------------------------------------------------

class TestPerTurnCap:
    def test_cap_constant(self):
        from agent import app  # noqa: F401
        from agent.chat import EXTERNAL_LOOKUP_CAP
        assert EXTERNAL_LOOKUP_CAP == 5

    def test_cr_api_in_external_lookup_set(self):
        from agent.tool_policy import EXTERNAL_LOOKUP_TOOL_NAMES
        assert "cr_api" in EXTERNAL_LOOKUP_TOOL_NAMES
