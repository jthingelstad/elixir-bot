"""Tests for storage/opponent_intel.py and runtime/helpers/_intel_report.py."""

from datetime import datetime

import pytest

from storage.opponent_intel import (
    analyze_clan_roster,
    analyze_war_participants,
    build_intel_report,
    compute_threat_rating,
)
from runtime.helpers._intel_report import (
    format_intel_report,
    format_intel_summary_for_memory,
)

DISCORD_MAX_MESSAGE_LEN = 2000

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_member(name, trophies=6000, exp_level=14, role="member", last_seen="20260411T120000"):
    return {
        "tag": f"#{name.upper()[:6]}",
        "name": name,
        "trophies": trophies,
        "expLevel": exp_level,
        "role": role,
        "lastSeen": last_seen,
        "clanRank": 1,
        "previousClanRank": 1,
        "donations": 50,
        "donationsReceived": 30,
        "arena": {"id": 54, "name": "Arena 22"},
    }


def _make_clan_profile(tag, name, members=None, war_trophies=3000, clan_score=50000):
    if members is None:
        members = [_make_member(f"Player{i}") for i in range(10)]
    return {
        "tag": f"#{tag}",
        "name": name,
        "type": "inviteOnly",
        "clanScore": clan_score,
        "clanWarTrophies": war_trophies,
        "requiredTrophies": 5000,
        "donationsPerWeek": 8000,
        "members": len(members),
        "memberList": members,
    }


def _make_war_participant(tag, name, fame=100, decks_used=4, decks_today=4):
    return {
        "tag": f"#{tag}",
        "name": name,
        "fame": fame,
        "repairPoints": 0,
        "boatAttacks": 0,
        "decksUsed": decks_used,
        "decksUsedToday": decks_today,
    }


def _make_war_clan_entry(tag, name, participants=None, fame=500):
    if participants is None:
        participants = [_make_war_participant(f"P{i}", f"Player{i}") for i in range(10)]
    return {
        "tag": f"#{tag}",
        "name": name,
        "fame": fame,
        "repairPoints": 0,
        "periodPoints": 0,
        "clanScore": 50000,
        "participants": participants,
    }


# ---------------------------------------------------------------------------
# analyze_clan_roster
# ---------------------------------------------------------------------------

class TestAnalyzeClanRoster:
    def test_basic_metrics(self):
        members = [
            _make_member("Alpha", trophies=7000, exp_level=14, role="leader"),
            _make_member("Bravo", trophies=6000, exp_level=13, role="coLeader"),
            _make_member("Charlie", trophies=5000, exp_level=12, role="member"),
        ]
        profile = _make_clan_profile("ABC", "Test Clan", members=members)
        result = analyze_clan_roster(profile)

        assert result["name"] == "Test Clan"
        assert result["member_count"] == 3
        assert result["avg_trophies"] == 6000
        assert result["max_trophies"] == 7000
        assert result["avg_exp_level"] == 13.0
        assert result["role_breakdown"]["leader"] == 1
        assert result["role_breakdown"]["coLeader"] == 1
        assert result["role_breakdown"]["member"] == 1

    def test_empty_roster(self):
        profile = _make_clan_profile("ABC", "Empty Clan", members=[])
        result = analyze_clan_roster(profile)
        assert result["member_count"] == 0
        assert result["avg_trophies"] == 0
        assert result["top_players"] == []

    def test_top_players_capped_at_five(self):
        members = [_make_member(f"P{i}", trophies=7000 - i * 100) for i in range(10)]
        profile = _make_clan_profile("ABC", "Big Clan", members=members)
        result = analyze_clan_roster(profile)
        assert len(result["top_players"]) == 5
        assert result["top_players"][0]["trophies"] == 7000

    def test_activity_detection(self):
        now = datetime(2026, 4, 11, 12, 0, 0)
        recent = "20260411T100000"  # 2h ago
        old = "20260401T100000"  # 10 days ago
        members = [
            _make_member("Recent", last_seen=recent),
            _make_member("Old", last_seen=old),
        ]
        profile = _make_clan_profile("ABC", "Activity Test", members=members)
        result = analyze_clan_roster(profile, now=now)
        assert result["recently_active_count"] == 1
        assert result["active_within_week_count"] == 1


# ---------------------------------------------------------------------------
# analyze_war_participants
# ---------------------------------------------------------------------------

class TestAnalyzeWarParticipants:
    def test_basic_metrics(self):
        participants = [
            _make_war_participant("A", "Alpha", fame=200, decks_used=8, decks_today=4),
            _make_war_participant("B", "Bravo", fame=100, decks_used=4, decks_today=0),
            _make_war_participant("C", "Charlie", fame=0, decks_used=0, decks_today=0),
        ]
        entry = _make_war_clan_entry("ABC", "Test", participants=participants)
        result = analyze_war_participants(entry)

        assert result["participant_count"] == 3
        assert result["total_fame"] == 300
        assert result["active_participants"] == 2
        assert result["full_deck_today"] == 1
        assert result["zero_deck_today"] == 2
        assert result["engagement_pct"] == pytest.approx(66.7, abs=0.1)

    def test_empty_participants(self):
        entry = _make_war_clan_entry("ABC", "Empty", participants=[])
        result = analyze_war_participants(entry)
        assert result["participant_count"] == 0
        assert result["engagement_pct"] == 0


# ---------------------------------------------------------------------------
# compute_threat_rating
# ---------------------------------------------------------------------------

class TestComputeThreatRating:
    def test_high_threat(self):
        roster = {
            "war_trophies": 4500,
            "avg_trophies": 7500,
            "member_count": 50,
            "max_members": 50,
            "recently_active_count": 45,
            "donations_per_week": 15000,
        }
        war = {"engagement_pct": 90}
        rating = compute_threat_rating(roster, war)
        assert rating >= 4

    def test_low_threat(self):
        roster = {
            "war_trophies": 500,
            "avg_trophies": 2000,
            "member_count": 15,
            "max_members": 50,
            "recently_active_count": 3,
            "donations_per_week": 500,
        }
        war = {"engagement_pct": 10}
        rating = compute_threat_rating(roster, war)
        assert rating <= 2

    def test_no_data(self):
        assert compute_threat_rating(None, None) == 1


# ---------------------------------------------------------------------------
# build_intel_report
# ---------------------------------------------------------------------------

class TestBuildIntelReport:
    def test_sorts_by_threat_our_clan_last(self):
        our_tag = "OUR"
        war_data = {
            "clan": _make_war_clan_entry("OUR", "POAP KINGS"),
            "clans": [
                _make_war_clan_entry("AAA", "Strong Clan"),
                _make_war_clan_entry("BBB", "Weak Clan"),
            ],
        }
        profiles = {
            "OUR": _make_clan_profile("OUR", "POAP KINGS", war_trophies=4000),
            "AAA": _make_clan_profile("AAA", "Strong Clan", war_trophies=4500),
            "BBB": _make_clan_profile("BBB", "Weak Clan", war_trophies=500,
                                       members=[_make_member("Solo", trophies=2000)]),
        }
        analyses = build_intel_report(war_data, profiles, our_tag)

        # Our clan should be last
        assert analyses[-1]["is_us"] is True
        assert analyses[-1]["name"] == "POAP KINGS"

        # Opponents sorted by threat (descending)
        opponent_names = [a["name"] for a in analyses if not a["is_us"]]
        assert opponent_names[0] == "Strong Clan"

    def test_missing_profile_graceful(self):
        war_data = {
            "clan": _make_war_clan_entry("OUR", "POAP KINGS"),
            "clans": [_make_war_clan_entry("AAA", "Mystery Clan")],
        }
        profiles = {"OUR": _make_clan_profile("OUR", "POAP KINGS"), "AAA": None}
        analyses = build_intel_report(war_data, profiles, "OUR")

        mystery = [a for a in analyses if a["name"] == "Mystery Clan"][0]
        assert mystery["profile_available"] is False
        assert mystery["roster"] is None
        assert mystery["war"] is not None


# ---------------------------------------------------------------------------
# format_intel_report
# ---------------------------------------------------------------------------

class TestFormatIntelReport:
    def _sample_analyses(self):
        war_data = {
            "clan": _make_war_clan_entry("OUR", "POAP KINGS"),
            "clans": [
                _make_war_clan_entry("A1", "Clan Alpha"),
                _make_war_clan_entry("B2", "Clan Bravo"),
                _make_war_clan_entry("C3", "Clan Charlie"),
                _make_war_clan_entry("D4", "Clan Delta"),
                _make_war_clan_entry("E5", "Clan Echo"),
            ],
        }
        profiles = {
            "OUR": _make_clan_profile("OUR", "POAP KINGS"),
            "A1": _make_clan_profile("A1", "Clan Alpha"),
            "B2": _make_clan_profile("B2", "Clan Bravo"),
            "C3": _make_clan_profile("C3", "Clan Charlie"),
            "D4": _make_clan_profile("D4", "Clan Delta"),
            "E5": _make_clan_profile("E5", "Clan Echo"),
        }
        return build_intel_report(war_data, profiles, "OUR")

    def test_six_messages_with_five_opponents(self):
        analyses = self._sample_analyses()
        messages = format_intel_report(analyses, season_id=131)
        # 1 header + 5 opponent clans = 6 messages
        assert len(messages) == 6

    def test_header_contains_season(self):
        analyses = self._sample_analyses()
        messages = format_intel_report(analyses, season_id=131)
        assert "Season 131" in messages[0]

    def test_llm_summary_in_header(self):
        analyses = self._sample_analyses()
        messages = format_intel_report(
            analyses, season_id=131,
            llm_summary="Clan Alpha is the biggest threat due to high activity.",
        )
        assert "Clan Alpha is the biggest threat" in messages[0]
        # Still 6 messages (summary merged into header, not separate)
        assert len(messages) == 6

    def test_all_messages_under_discord_limit(self):
        analyses = self._sample_analyses()
        messages = format_intel_report(analyses, season_id=131)
        for i, msg in enumerate(messages):
            assert len(msg) <= DISCORD_MAX_MESSAGE_LEN, f"Message {i} exceeds {DISCORD_MAX_MESSAGE_LEN} chars: {len(msg)}"

    def test_no_markdown_tables(self):
        analyses = self._sample_analyses()
        messages = format_intel_report(analyses, season_id=131)
        for msg in messages:
            # Check for pipe-delimited table rows
            lines = msg.split("\n")
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("|") and stripped.endswith("|"):
                    pytest.fail(f"Found markdown table syntax: {line}")

    def test_our_clan_excluded_from_messages(self):
        analyses = self._sample_analyses()
        messages = format_intel_report(analyses, season_id=131)
        for msg in messages[1:]:  # skip header
            assert "POAP KINGS" not in msg


# ---------------------------------------------------------------------------
# format_intel_summary_for_memory
# ---------------------------------------------------------------------------

class TestFormatIntelSummaryForMemory:
    def test_excludes_our_clan(self):
        analyses = [
            {"name": "Rival", "tag": "#R1", "is_us": False, "threat_rating": 3,
             "roster": {"member_count": 40, "avg_trophies": 6000, "war_trophies": 3000},
             "war": {"engagement_pct": 70}},
            {"name": "Us", "tag": "#US", "is_us": True, "threat_rating": 4,
             "roster": {"member_count": 50, "avg_trophies": 7000, "war_trophies": 4000},
             "war": {"engagement_pct": 90}},
        ]
        summary = format_intel_summary_for_memory(analyses)
        assert "Rival" in summary
        assert "Us" not in summary

    def test_returns_string(self):
        analyses = [
            {"name": "Clan A", "tag": "#A", "is_us": False, "threat_rating": 2,
             "roster": {"member_count": 30, "avg_trophies": 5000, "war_trophies": 1500},
             "war": {"engagement_pct": 40}},
        ]
        result = format_intel_summary_for_memory(analyses)
        assert isinstance(result, str)
        assert "Clan A" in result
