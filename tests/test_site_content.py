"""Tests for the POAP KINGS site integration."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

import db
from integrations.poap_kings import site as site_content


@pytest.fixture
def conn():
    """In-memory SQLite DB with schema."""
    c = db.get_connection(":memory:")
    yield c
    c.close()


@pytest.fixture
def tmp_repo(tmp_path):
    """Temporary repo structure with schemas."""
    data_dir = tmp_path / "src" / "_data"
    schema_dir = data_dir / "schemas"
    schema_dir.mkdir(parents=True)

    # Copy schemas from the real poapkings.com
    real_schema_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "poapkings.com", "src", "_data", "schemas"
    )
    if os.path.exists(real_schema_dir):
        for f in os.listdir(real_schema_dir):
            if f.endswith(".schema.json"):
                with open(os.path.join(real_schema_dir, f)) as src:
                    (schema_dir / f).write_text(src.read())

    return tmp_path


# ── write_content / load_current ──────────────────────────────────────────

def test_write_and_load_content(tmp_repo, monkeypatch):
    """Write content and load it back."""
    monkeypatch.setattr(site_content, "DATA_DIR", str(tmp_repo / "src" / "_data"))
    monkeypatch.setattr(site_content, "SCHEMA_DIR", str(tmp_repo / "src" / "_data" / "schemas"))

    data = {
        "memberCount": 18,
        "clanScore": 57519,
        "clanWarTrophies": 140,
        "donationsPerWeek": 212,
        "totalTrophies": 132730,
        "avgLevel": 42.5,
        "minTrophies": 2000,
        "clanLeague": "Bronze",
        "clanStatus": "Open",
    }
    assert site_content.write_content("clan", data) is True
    loaded = site_content.load_current("clan")
    assert loaded["memberCount"] == 18
    assert loaded["clanScore"] == 57519


def test_write_content_invalid_schema(tmp_repo, monkeypatch):
    """Invalid data fails schema validation and is not written."""
    monkeypatch.setattr(site_content, "DATA_DIR", str(tmp_repo / "src" / "_data"))
    monkeypatch.setattr(site_content, "SCHEMA_DIR", str(tmp_repo / "src" / "_data" / "schemas"))
    schema_path = tmp_repo / "src" / "_data" / "schemas" / "elixirClan.schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["memberCount"],
                "properties": {
                    "memberCount": {"type": "integer"},
                },
            }
        )
    )

    data = {"memberCount": "not_an_int"}  # Missing required fields, wrong type
    assert site_content.write_content("clan", data) is False


def test_write_content_unknown_type():
    """Unknown content type raises ValueError."""
    with pytest.raises(ValueError, match="Unknown content type"):
        site_content.write_content("unknown", {})


def test_load_current_missing(tmp_repo, monkeypatch):
    """Returns None for non-existent file."""
    monkeypatch.setattr(site_content, "DATA_DIR", str(tmp_repo / "src" / "_data"))
    assert site_content.load_current("home") is None


def test_load_current_unknown_type():
    """Unknown content type returns None."""
    assert site_content.load_current("unknown") is None


def test_publish_site_content_returns_structured_result_when_commit_created(monkeypatch):
    payloads = {"home": {"message": "Hello from Elixir"}}
    github_calls = []

    def fake_github_request(method, path, *, payload=None, expected=(200,), token=None):
        github_calls.append((method, path, payload))
        responses = {
            ("POST", "/git/blobs"): {"sha": "blobsha123"},
            ("GET", "/git/ref/heads/main"): {"object": {"sha": "parentcommit123"}},
            ("GET", "/git/commits/parentcommit123"): {"tree": {"sha": "basetree123"}},
            ("POST", "/git/trees"): {"sha": "treesha123"},
            ("POST", "/git/commits"): {"sha": "commitsha1234567890"},
            ("PATCH", "/git/refs/heads/main"): None,
        }
        return responses[(method, path)]

    monkeypatch.setattr(site_content, "site_enabled", lambda: True)
    monkeypatch.setattr(site_content, "_site_repo", lambda: "poap/test-site")
    monkeypatch.setattr(site_content, "_site_branch", lambda: "main")
    monkeypatch.setattr(site_content, "load_published", lambda content_type, branch=None: None)
    monkeypatch.setattr(site_content, "_github_request", fake_github_request)

    result = site_content.publish_site_content(payloads, "Test publish")

    assert result == {
        "changed": True,
        "commit_sha": "commitsha1234567890",
        "commit_url": "https://github.com/poap/test-site/commit/commitsha1234567890",
        "repo": "poap/test-site",
        "branch": "main",
        "changed_content_types": ["home"],
        "changed_paths": ["src/_data/elixirHome.json"],
    }
    assert github_calls[0][0:2] == ("POST", "/git/blobs")
    assert github_calls[-1][0:2] == ("PATCH", "/git/refs/heads/main")


def test_publish_site_content_returns_structured_no_change_result(monkeypatch):
    payloads = {"home": {"message": "Hello from Elixir"}}

    monkeypatch.setattr(site_content, "site_enabled", lambda: True)
    monkeypatch.setattr(site_content, "_site_repo", lambda: "poap/test-site")
    monkeypatch.setattr(site_content, "_site_branch", lambda: "main")
    monkeypatch.setattr(site_content, "load_published", lambda content_type, branch=None: {"message": "Hello from Elixir"})

    with patch("integrations.poap_kings.site._github_request") as mock_github:
        result = site_content.publish_site_content(payloads, "Test publish")

    assert result == {
        "changed": False,
        "commit_sha": None,
        "commit_url": None,
        "repo": "poap/test-site",
        "branch": "main",
        "changed_content_types": [],
        "changed_paths": [],
    }
    mock_github.assert_not_called()


# ── build_clan_data ──────────────────────────────────────────────────────────

def test_build_clan_data():
    """Extracts clan stats correctly."""
    clan_data = {
        "members": 18,
        "clanScore": 57519,
        "clanWarTrophies": 140,
        "donationsPerWeek": 212,
        "requiredTrophies": 2000,
        "type": "open",
        "warLeague": {"name": "Bronze"},
        "location": {"id": 57000000, "name": "International", "isCountry": False},
        "memberList": [
            {"trophies": 10000, "expLevel": 50},
            {"trophies": 8000, "expLevel": 40},
        ],
    }
    result = site_content.build_clan_data(clan_data)
    assert result["memberCount"] == 18
    assert result["totalTrophies"] == 18000
    assert result["avgLevel"] == 45.0
    assert result["clanLeague"] == "Bronze"
    assert result["clanStatus"] == "Open"
    assert result["clanRegion"] == "International"


def test_build_clan_data_empty():
    """Handles empty member list."""
    result = site_content.build_clan_data({"memberList": []})
    assert result["memberCount"] == 0
    assert result["avgLevel"] == 0
    assert result["clanRegion"] == "Not Set"


# ── build_roster_data ────────────────────────────────────────────────────────

def test_build_roster_data(conn):
    """Builds roster from API data and stored member metadata."""
    db.set_member_join_date("ABC123", "King Levy", "2026-02-04T04:00:00Z", conn=conn)
    db.set_member_note("ABC123", "King Levy", "Founder", conn=conn)
    db.set_member_profile_url("ABC123", "King Levy", "https://example.com", conn=conn)
    db.set_member_generated_profile(
        "ABC123",
        "King Levy",
        "King Levy is one of the clan's war leaders and a steady ladder force.",
        "war",
        conn=conn,
    )
    db.snapshot_player_profile(
        {
            "tag": "#ABC123",
            "name": "King Levy",
            "currentDeck": [],
            "cards": [],
            "badges": [
                {"name": "YearsPlayed", "level": 4, "maxLevel": 11, "progress": 1473, "target": 1825, "iconUrls": {"large": "https://cdn/years.png"}},
                {"name": "BattleWins", "level": 5, "maxLevel": 8, "progress": 2079, "target": 2500, "iconUrls": {"large": "https://cdn/wins.png"}},
                {"name": "MasteryGoblinBarrel", "level": 3, "maxLevel": 10, "progress": 180, "target": 200, "iconUrls": {"large": "https://cdn/mastery-gb.png"}},
            ],
            "achievements": [
                {"name": "Team Player", "stars": 3, "value": 1, "target": 1, "info": "Join a Clan", "completionInfo": None},
                {"name": "Friend in Need", "stars": 2, "value": 1200, "target": 2500, "info": "Donate 2500 cards", "completionInfo": None},
            ],
        },
        conn=conn,
    )
    with patch("storage.player.chicago_today", return_value="2026-03-14"):
        db.snapshot_player_battlelog(
            "#ABC123",
            [
                {
                    "type": "PvP",
                    "battleTime": "20260314T100000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 2, "trophyChange": 30, "startingTrophies": 7000, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP1", "name": "Opp 1", "crowns": 1, "cards": [], "supportCards": []}],
                },
                {
                    "type": "PvP",
                    "battleTime": "20260310T110000.000Z",
                    "gameMode": {"id": 72000006, "name": "Ladder"},
                    "team": [{"tag": "#ABC123", "name": "King Levy", "crowns": 3, "trophyChange": 31, "startingTrophies": 6970, "cards": [], "supportCards": []}],
                    "opponent": [{"tag": "#OPP2", "name": "Opp 2", "crowns": 0, "cards": [], "supportCards": []}],
                },
            ],
            conn=conn,
        )

    clan_data = {
        "memberList": [
            {
                "tag": "#ABC123",
                "name": "King Levy",
                "role": "coLeader",
                "expLevel": 66,
                "trophies": 11313,
                "arena": {"name": "Musketeer Street"},
                "clanRank": 3,
                "donations": 30,
                "donationsReceived": 32,
                "lastSeen": "20260303T034007.000Z",
            },
            {
                "tag": "#DEF456",
                "name": "Newbie",
                "role": "member",
                "expLevel": 10,
                "trophies": 3000,
                "arena": {"name": "Arena 5"},
                "clanRank": 10,
                "donations": 0,
                "donationsReceived": 0,
                "lastSeen": "20260303T120000.000Z",
            },
        ],
    }
    result = site_content.build_roster_data(clan_data, conn=conn)
    assert "updated" in result
    assert len(result["members"]) == 2

    # King Levy has extras from DB
    levy = result["members"][0]
    assert levy["name"] == "King Levy"
    assert levy["note"] == "Founder"
    assert levy["profile_url"] == "https://example.com"
    assert levy["date_joined"] == "2026-02-04T04:00:00Z"
    assert levy["cr_account_age_days"] == 1473
    assert levy["cr_account_age_years"] == 4
    assert levy["cr_account_age_updated_at"] is not None
    assert levy["cr_games_per_day"] == 0.14
    assert levy["cr_games_per_day_window_days"] == 14
    assert levy["cr_games_per_day_updated_at"] is not None
    assert levy["badge_count"] == 3
    assert levy["badge_highlights"][0]["name"] == "YearsPlayed"
    assert levy["badge_highlights"][1]["name"] == "BattleWins"
    assert levy["badge_highlights"][0]["icon_url"] == "https://cdn/years.png"
    assert levy["mastery_highlights"][0]["card_name"] == "Goblin Barrel"
    assert levy["mastery_highlights"][0]["icon_url"] == "https://cdn/mastery-gb.png"
    assert levy["achievement_star_count"] == 5
    assert levy["achievement_completed_count"] == 1
    assert levy["achievement_progress"][0]["name"] == "Team Player"
    assert levy["achievement_progress"][0]["completed"] is True
    assert levy["bio"] == "King Levy is one of the clan's war leaders and a steady ladder force."
    assert levy["highlight"] == "war"

    # Unknown join dates stay unknown until observed elsewhere or overridden.
    newbie = result["members"][1]
    assert newbie["date_joined"] is None
    assert newbie["cr_account_age_days"] is None
    assert newbie["cr_games_per_day"] is None
    assert newbie["badge_highlights"] == []
    assert newbie["achievement_progress"] == []


def test_build_roster_data_sorted_by_join_date(conn):
    """Roster is sorted by join date ascending."""
    db.set_member_join_date("TAG1", "Older", "2026-01-01T00:00:00Z", conn=conn)
    db.set_member_join_date("TAG2", "Newer", "2026-02-01T00:00:00Z", conn=conn)

    clan_data = {
        "memberList": [
            {"tag": "#TAG2", "name": "Newer", "role": "member", "arena": {}},
            {"tag": "#TAG1", "name": "Older", "role": "member", "arena": {}},
        ],
    }
    result = site_content.build_roster_data(clan_data, conn=conn)
    assert result["members"][0]["name"] == "Older"
    assert result["members"][1]["name"] == "Newer"


# ── validate_against_schema ──────────────────────────────────────────────────

def test_validate_home_schema(tmp_repo, monkeypatch):
    """Home message validates correctly."""
    monkeypatch.setattr(site_content, "SCHEMA_DIR", str(tmp_repo / "src" / "_data" / "schemas"))
    data = {"message": "Hello world", "generated": "2026-03-06T20:00:00Z"}
    assert site_content.validate_against_schema("home", data) is True


def test_validate_roster_schema(tmp_repo, monkeypatch):
    """Roster data validates correctly."""
    monkeypatch.setattr(site_content, "SCHEMA_DIR", str(tmp_repo / "src" / "_data" / "schemas"))
    data = {
        "updated": "2026-03-06T20:00:00Z",
        "members": [
            {"name": "King Levy", "tag": "ABC123", "role": "Co-Leader"},
        ],
    }
    assert site_content.validate_against_schema("roster", data) is True


def test_validate_promote_schema(tmp_repo, monkeypatch):
    """Promote data validates correctly."""
    monkeypatch.setattr(site_content, "SCHEMA_DIR", str(tmp_repo / "src" / "_data" / "schemas"))
    data = {
        "message": {"body": "Join us!"},
        "social": {"body": "Follow us!"},
        "email": {"subject": "Hi", "body": "Join!"},
        "discord": {"body": "Come play!"},
        "reddit": {"title": "POAP KINGS", "body": "Join!"},
    }
    assert site_content.validate_against_schema("promote", data) is True


# ── aggregate_card_usage ──────────────────────────────────────────────────

_filler_counter = [0]

def _make_deck(*named_cards):
    """Build an 8-card deck list, padding with unique filler cards."""
    cards = list(named_cards)
    for i in range(8 - len(cards)):
        cards.append({"name": f"_filler_{_filler_counter[0]}", "iconUrls": {"medium": ""}})
        _filler_counter[0] += 1
    return cards


def test_aggregate_card_usage():
    """Aggregates card usage from battle log correctly."""
    battle_log = [
        {
            "type": "PvP",
            "team": [{"tag": "#ABC123", "cards": _make_deck(
                {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
                {"name": "Fireball", "iconUrls": {"medium": "https://cdn/fireball.png"}},
                {"name": "Musketeer", "iconUrls": {"medium": "https://cdn/musk.png"}},
            )}],
        },
        {
            "type": "PvP",
            "team": [{"tag": "#ABC123", "cards": _make_deck(
                {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
                {"name": "Fireball", "iconUrls": {"medium": "https://cdn/fireball.png"}},
                {"name": "Valkyrie", "iconUrls": {"medium": "https://cdn/valk.png"}},
            )}],
        },
        {
            "type": "PvP",
            "team": [{"tag": "#ABC123", "cards": _make_deck(
                {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
                {"name": "Valkyrie", "iconUrls": {"medium": "https://cdn/valk.png"}},
                {"name": "Zap", "iconUrls": {"medium": "https://cdn/zap.png"}},
            )}],
        },
    ]
    result = site_content.aggregate_card_usage(battle_log, "ABC123")
    assert len(result) > 0
    # Hog Rider used in all 3 battles
    assert result[0]["name"] == "Hog Rider"
    assert result[0]["usage_pct"] == 100
    assert result[0]["icon_url"] == "https://cdn/hog.png"
    # Fireball and Valkyrie each used in 2 battles
    second_third_names = {result[1]["name"], result[2]["name"]}
    assert second_third_names == {"Fireball", "Valkyrie"}
    assert result[1]["usage_pct"] == 67


def test_aggregate_card_usage_empty():
    """None or empty input returns empty list."""
    assert site_content.aggregate_card_usage(None, "ABC") == []
    assert site_content.aggregate_card_usage([], "ABC") == []


def test_aggregate_card_usage_no_matching_player():
    """Returns empty when player tag not found in any battle."""
    battle_log = [
        {"type": "PvP", "team": [{"tag": "#OTHER", "cards": _make_deck(
            {"name": "Zap", "iconUrls": {"medium": ""}},
        )}]},
    ]
    assert site_content.aggregate_card_usage(battle_log, "ABC123") == []


def test_aggregate_card_usage_skips_friendlies_and_duels():
    """Friendlies, boat battles, and duels are excluded."""
    battle_log = [
        {"type": "friendly", "team": [{"tag": "#ABC123", "cards": _make_deck(
            {"name": "Golem", "iconUrls": {"medium": ""}},
        )}]},
        {"type": "boatBattle", "team": [{"tag": "#ABC123", "cards": _make_deck(
            {"name": "Golem", "iconUrls": {"medium": ""}},
        )}]},
        {"type": "riverRaceDuel", "team": [{"tag": "#ABC123", "cards": _make_deck(
            {"name": "Golem", "iconUrls": {"medium": ""}},
        )}]},
        {"type": "PvP", "team": [{"tag": "#ABC123", "cards": _make_deck(
            {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
        )}]},
    ]
    result = site_content.aggregate_card_usage(battle_log, "ABC123")
    card_names = [c["name"] for c in result]
    assert "Hog Rider" in card_names
    assert "Golem" not in card_names


def test_extract_current_deck():
    """Extracts card names from player profile."""
    player = {
        "currentDeck": [
            {"name": "Hog Rider", "id": 1},
            {"name": "Fireball", "id": 2},
        ]
    }
    result = site_content.extract_current_deck(player)
    assert result == ["Hog Rider", "Fireball"]


def test_extract_current_deck_empty():
    """Returns empty list for None or missing deck."""
    assert site_content.extract_current_deck(None) == []
    assert site_content.extract_current_deck({}) == []


# ── build_roster_data with cards ──────────────────────────────────────────

def test_build_roster_data_with_cards(conn):
    """With include_cards=True, members get favorite_cards and current_deck."""
    mock_battle_log = [
        {"type": "PvP", "team": [{"tag": "#ABC123", "cards": _make_deck(
            {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
        )}]},
    ]
    mock_player = {
        "currentDeck": [
            {"name": "Knight", "iconUrls": {"medium": "https://cdn/knight-medium.png"}, "level": 16, "maxLevel": 16, "rarity": "common", "evolutionLevel": 3, "maxEvolutionLevel": 3},
            {"name": "Zap", "iconUrls": {"medium": "https://cdn/zap-medium.png"}, "level": 11, "maxLevel": 11, "rarity": "epic", "maxEvolutionLevel": 1},
        ],
        "currentDeckSupportCards": [
            {"name": "Dagger Duchess", "iconUrls": {"medium": "https://cdn/duchess-medium.png"}, "level": 4, "maxLevel": 4, "rarity": "legendary"},
        ],
        "cards": [
            {"name": "Knight", "iconUrls": {"medium": "https://cdn/knight-medium.png"}, "level": 16, "maxLevel": 16, "rarity": "common", "evolutionLevel": 3, "maxEvolutionLevel": 3},
            {"name": "Zap", "iconUrls": {"medium": "https://cdn/zap-medium.png"}, "level": 11, "maxLevel": 11, "rarity": "epic", "maxEvolutionLevel": 1},
        ],
        "badges": [
            {"name": "YearsPlayed", "level": 4, "maxLevel": 11, "progress": 1473, "target": 1825, "iconUrls": {"large": "https://cdn/years.png"}},
            {"name": "MasteryHogRider", "level": 4, "maxLevel": 10, "progress": 220, "target": 240, "iconUrls": {"large": "https://cdn/mastery-hog.png"}},
        ],
        "achievements": [
            {"name": "Team Player", "stars": 3, "value": 1, "target": 1, "info": "Join a Clan", "completionInfo": None},
        ],
    }

    clan_data = {
        "memberList": [
            {"tag": "#ABC123", "name": "TestPlayer", "role": "member", "arena": {}},
        ],
    }

    with patch("integrations.poap_kings.site.cr_api.get_player_battle_log", return_value=mock_battle_log), \
         patch("integrations.poap_kings.site.cr_api.get_player", return_value=mock_player), \
         patch("integrations.poap_kings.site.time.sleep"):
        result = site_content.build_roster_data(clan_data, include_cards=True, conn=conn)

    m = result["members"][0]
    assert len(m["favorite_cards"]) == 8
    assert m["favorite_cards"][0]["name"] == "Hog Rider"
    assert "Knight" in m["current_deck"]
    assert "Zap" in m["current_deck"]
    assert m["current_deck_cards"][0]["icon_url"] == "https://cdn/knight-medium.png"
    assert m["current_deck_cards"][0]["mode_label"] == "Evo + Hero"
    assert m["current_deck_cards"][0]["mode_status_label"] == "Evo + Hero unlocked"
    assert m["current_deck_cards"][1]["supports_evo"] is True
    assert "mode_label" not in m["current_deck_cards"][1]
    assert m["current_deck_mode_note"] == "Activation depends on deck slot; these labels show what the card supports or has unlocked."
    assert m["current_deck_support_cards"][0]["icon_url"] == "https://cdn/duchess-medium.png"
    assert m["collection_highlights"][0]["mode_label"] == "Evo + Hero"
    assert m["badge_highlights"][0]["name"] == "YearsPlayed"
    assert m["badge_highlights"][0]["icon_url"] == "https://cdn/years.png"
    assert m["mastery_highlights"][0]["card_name"] == "Hog Rider"
    assert m["mastery_highlights"][0]["icon_url"] == "https://cdn/mastery-hog.png"
    assert m["achievement_progress"][0]["name"] == "Team Player"


def test_build_card_stats():
    """Aggregates clan-wide card stats from current decks."""
    members = [
        {
            "name": "King Levy",
            "clan_rank": 2,
            "current_deck": ["Hog Rider", "Fireball", "Zap"],
            "_current_deck_icons": {"Hog Rider": "https://cdn/hog.png", "Fireball": "https://cdn/fb.png"},
        },
        {
            "name": "Finn",
            "clan_rank": 1,
            "current_deck": ["Hog Rider", "Zap"],
            "_current_deck_icons": {"Zap": "https://cdn/zap.png"},
        },
        {
            "name": "Vijay",
            "clan_rank": 7,
            "current_deck": ["Zap"],
        },
        {"name": "NoDeck", "clan_rank": 99, "current_deck": []},
    ]
    result = site_content.build_card_stats(members)
    assert len(result) == 3
    assert result[0]["name"] == "Zap"
    assert result[0]["member_count"] == 3
    assert result[0]["avg_pct"] == 100
    assert result[0]["members"] == ["Finn", "King Levy", "Vijay"]
    assert result[1]["name"] == "Hog Rider"
    assert result[1]["member_count"] == 2
    assert result[1]["avg_pct"] == 67
    assert result[1]["icon_url"] == "https://cdn/hog.png"


def test_build_card_stats_empty():
    """Empty members returns empty stats."""
    assert site_content.build_card_stats([]) == []
    assert site_content.build_card_stats([{"current_deck": []}]) == []


def test_build_card_stats_limits_member_list_by_clan_rank():
    members = []
    for idx, name in enumerate(["G", "E", "C", "A", "F", "D", "B"], start=1):
        members.append(
            {
                "name": name,
                "clan_rank": idx,
                "current_deck": ["Hog Rider"],
            }
        )

    result = site_content.build_card_stats(members)

    assert result[0]["name"] == "Hog Rider"
    assert result[0]["member_count"] == 7
    assert result[0]["members"] == ["G", "E", "C", "A", "F"]


def test_build_roster_data_includes_card_stats(conn):
    """With include_cards=True, roster includes card_stats."""
    mock_battle_log = [
        {"type": "PvP", "team": [{"tag": "#ABC123", "cards": _make_deck(
            {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
        )}]},
    ]
    mock_player = {"currentDeck": [{"name": "Hog Rider"}]}
    clan_data = {
        "memberList": [
            {"tag": "#ABC123", "name": "TestPlayer", "role": "member", "arena": {}},
        ],
    }
    with patch("integrations.poap_kings.site.cr_api.get_player_battle_log", return_value=mock_battle_log), \
         patch("integrations.poap_kings.site.cr_api.get_player", return_value=mock_player), \
         patch("integrations.poap_kings.site.time.sleep"):
        result = site_content.build_roster_data(clan_data, include_cards=True, conn=conn)
    assert "card_stats" in result
    assert len(result["card_stats"]) >= 1
    card_names = [c["name"] for c in result["card_stats"]]
    assert "Hog Rider" in card_names


def test_build_roster_data_without_cards(conn):
    """Default (include_cards=False) does not include card fields."""
    clan_data = {
        "memberList": [
            {"tag": "#ABC123", "name": "TestPlayer", "role": "member", "arena": {}},
        ],
    }
    result = site_content.build_roster_data(clan_data, conn=conn)
    m = result["members"][0]
    assert "favorite_cards" not in m
    assert "current_deck" not in m


def test_build_roster_data_without_cards_uses_cached_card_data(conn):
    """Default roster builds should keep card data from the DB cache."""
    db.snapshot_members(
        [{"tag": "#ABC123", "name": "TestPlayer", "role": "member", "arena": {}}],
        conn=conn,
    )
    db.snapshot_player_profile(
        {
            "tag": "#ABC123",
            "name": "TestPlayer",
            "currentDeck": [
                {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
                {"name": "Zap", "iconUrls": {"medium": "https://cdn/zap.png"}},
            ],
            "currentDeckSupportCards": [
                {"name": "Tower Princess", "iconUrls": {"medium": "https://cdn/tower-princess.png"}},
            ],
            "cards": [
                {"name": "Hog Rider", "level": 14, "maxLevel": 14, "rarity": "rare", "iconUrls": {"medium": "https://cdn/hog.png"}},
                {"name": "Zap", "level": 11, "maxLevel": 11, "rarity": "epic", "iconUrls": {"medium": "https://cdn/zap.png"}},
                {"name": "Knight", "level": 16, "maxLevel": 16, "rarity": "common", "iconUrls": {"medium": "https://cdn/knight.png"}},
            ],
            "supportCards": [
                {"name": "Tower Princess", "level": 4, "maxLevel": 4, "rarity": "legendary", "iconUrls": {"medium": "https://cdn/tower-princess.png"}},
            ],
        },
        conn=conn,
    )
    db.snapshot_player_battlelog(
        "#ABC123",
        [
            {
                "type": "PvP",
                "battleTime": "20260309T120000.000Z",
                "team": [
                    {
                        "tag": "#ABC123",
                        "cards": _make_deck(
                            {"name": "Hog Rider", "iconUrls": {"medium": "https://cdn/hog.png"}},
                            {"name": "Zap", "iconUrls": {"medium": "https://cdn/zap.png"}},
                        ),
                        "supportCards": [],
                        "crowns": 3,
                    }
                ],
                "opponent": [{"tag": "#XYZ999", "crowns": 1, "cards": []}],
            }
        ],
        conn=conn,
    )

    clan_data = {
        "memberList": [
            {"tag": "#ABC123", "name": "TestPlayer", "role": "member", "arena": {}},
        ],
    }

    with patch("integrations.poap_kings.site.load_current", return_value=None):
        result = site_content.build_roster_data(clan_data, conn=conn)

    m = result["members"][0]
    assert m["favorite_cards"][0]["name"] == "Hog Rider"
    assert m["current_deck"] == ["Hog Rider", "Zap"]
    assert m["current_deck_cards"][0]["icon_url"] == "https://cdn/hog.png"
    assert m["current_deck_support_cards"][0]["name"] == "Tower Princess"
    assert m["current_deck_support_cards"][0]["icon_url"] == "https://cdn/tower-princess.png"
    assert m["card_collection_summary"]["highest_level"] == 16
    assert "Knight" in {card["name"] for card in m["collection_highlights"]}
    assert result["card_stats"][0]["name"] == "Hog Rider"
