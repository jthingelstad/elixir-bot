"""Tests for #reception onboarding — name matching and event handlers."""

import json
from unittest.mock import patch

import pytest

import elixir


SAMPLE_SNAPSHOT = {
    "#ABC123": "King Levy",
    "#DEF456": "Vijay",
    "#GHI789": "JaxikoLane",
}


class TestMatchClanMember:
    """Tests for the _match_clan_member helper."""

    def _match(self, nickname):
        matches = [
            {
                "player_tag": tag,
                "current_name": name,
                "match_source": "current_name_exact",
                "match_score": 100,
            }
            for tag, name in SAMPLE_SNAPSHOT.items()
            if name.lower().strip() == nickname.lower().strip()
        ]
        with patch("elixir.db.resolve_member", return_value=matches):
            return elixir._match_clan_member(nickname)

    def test_exact_match(self):
        result = self._match("King Levy")
        assert result == ("#ABC123", "King Levy")

    def test_case_insensitive(self):
        result = self._match("king levy")
        assert result == ("#ABC123", "King Levy")

    def test_case_insensitive_upper(self):
        result = self._match("VIJAY")
        assert result == ("#DEF456", "Vijay")

    def test_whitespace_stripped(self):
        result = self._match("  King Levy  ")
        assert result == ("#ABC123", "King Levy")

    def test_no_match(self):
        result = self._match("UnknownPlayer")
        assert result is None

    def test_empty_nickname(self):
        result = self._match("")
        assert result is None

    def test_empty_snapshot(self):
        with patch("elixir.db.resolve_member", return_value=[]), \
             patch("elixir.db.get_active_roster_map", return_value={}):
            result = elixir._match_clan_member("King Levy")
        assert result is None
