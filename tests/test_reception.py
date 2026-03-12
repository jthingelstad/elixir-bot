"""Tests for #reception onboarding — name matching and event handlers."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

import elixir
from runtime import onboarding


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


def test_send_onboarding_message_uses_shared_sender():
    channel = object()

    with (
        patch("runtime.onboarding._onboarding_channel", new=AsyncMock(return_value=channel)),
        patch("runtime.onboarding.elixir_agent.generate_message", return_value="Welcome :elixir_happy:"),
        patch("runtime.app._post_to_elixir", new=AsyncMock()) as mock_post,
    ):
        asyncio.run(
            onboarding._send_onboarding_message(
                "discord_member_join",
                "welcome prompt",
                "fallback",
            )
        )

    mock_post.assert_awaited_once_with(channel, {"content": "Welcome :elixir_happy:"})
