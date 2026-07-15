"""Shared member intelligence contract tests."""

from unittest.mock import patch

from capabilities.members import get_member_intelligence


class _Connection:
    def close(self):
        pass


class _MemberSource:
    def get_connection(self):
        return _Connection()

    def get_member_profile(self, tag):
        return {
            "player_tag": tag,
            "member_name": "Alpha",
            "observed_at": "2026-07-15T01:00:00Z",
        }

    def get_member_recent_form(self, tag, scope):
        return {
            "player_tag": tag,
            "scope": scope,
            "computed_at": "2026-07-15T01:05:00Z",
        }

    def get_member_war_status(self, tag, season_id):
        return {"player_tag": tag, "season_id": season_id, "observed_at": "2026-07-15T01:10:00Z"}

    def get_member_current_deck(self, tag):
        return {"player_tag": tag, "cards": [{"name": "Knight"}]}

    def get_member_signature_cards(self, tag, mode_scope):
        return {"player_tag": tag, "mode_scope": mode_scope, "cards": ["Knight"]}

    def get_member_recent_battles(self, tag, scope, limit):
        return {
            "player_tag": tag,
            "scope": scope,
            "limit": limit,
            "battles": [{"battle_time": "2026-07-15T01:11:00Z"}],
        }


def test_member_contract_combines_requested_local_facets_and_freshness():
    with patch(
        "capabilities.members.profile_engine.player_mode_profile",
        return_value={"identity": "ranked_regular", "total_battles": 42},
    ):
        result = get_member_intelligence(
            "abc123",
            facets=("profile", "form", "playstyle", "war", "loadout", "battles"),
            source=_MemberSource(),
        )

    assert result["capability"] == "member_intelligence"
    assert result["contract_version"] == 1
    assert result["player_tag"] == "#ABC123"
    assert result["profile"]["member_name"] == "Alpha"
    assert result["playstyle"]["identity"] == "ranked_regular"
    assert result["loadout"]["current_deck"]["cards"][0]["name"] == "Knight"
    assert result["freshness"] == {
        "profile_at": "2026-07-15T01:00:00Z",
        "form_at": "2026-07-15T01:05:00Z",
        "war_at": "2026-07-15T01:10:00Z",
        "latest_battle_at": "2026-07-15T01:11:00Z",
    }


def test_member_capability_does_not_fetch_unrequested_facets():
    result = get_member_intelligence(
        "#ABC123", facets=("profile",), source=_MemberSource()
    )

    assert result["requested_facets"] == ["profile"]
    assert "form" not in result
    assert "war" not in result
    assert "loadout" not in result
