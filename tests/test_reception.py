"""Tests for #reception onboarding — name matching and event handlers."""

import asyncio
from types import SimpleNamespace
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


def test_handle_member_join_refreshes_roster_before_welcome():
    member = SimpleNamespace(
        id=42,
        name="jamie",
        display_name="Jamie",
        global_name=None,
        mention="<@42>",
    )

    with (
        patch("runtime.onboarding.db.upsert_discord_user"),
        patch("runtime.onboarding.refresh_clan_roster_from_api", new=AsyncMock(return_value=True)) as mock_refresh,
        patch("runtime.onboarding._send_onboarding_message", new=AsyncMock()) as mock_send,
    ):
        asyncio.run(onboarding.handle_member_join(member))

    mock_refresh.assert_awaited_once_with(reason="discord_member_join")
    mock_send.assert_awaited_once()


def test_handle_member_update_refreshes_roster_only_after_initial_no_match():
    member_role = SimpleNamespace(id=777)
    guild = SimpleNamespace(get_role=lambda role_id: member_role if role_id == 777 else None)
    before = SimpleNamespace(nick="Old Name")
    after = SimpleNamespace(
        id=42,
        name="jamie",
        display_name="Jamie",
        global_name=None,
        mention="<@42>",
        nick="King Levy",
        guild=guild,
        roles=[],
    )

    with (
        patch("runtime.onboarding.db.upsert_discord_user"),
        patch("runtime.onboarding.db.link_discord_user_to_member"),
        patch("runtime.onboarding.refresh_clan_roster_from_api", new=AsyncMock(return_value=True)) as mock_refresh,
        patch("runtime.onboarding._ensure_member_role", new=AsyncMock(return_value=(True, "Granted"))) as mock_grant,
        patch("runtime.onboarding._send_onboarding_message", new=AsyncMock()) as mock_send,
        patch("runtime.app.MEMBER_ROLE_ID", 777),
        patch("runtime.app._match_clan_member", side_effect=[None, ("#ABC123", "King Levy")]) as mock_match,
    ):
        asyncio.run(onboarding.handle_member_update(before, after))

    mock_refresh.assert_awaited_once_with(reason="nickname_update_no_match")
    assert mock_match.call_count == 2
    mock_grant.assert_awaited_once()
    mock_send.assert_awaited_once()


def test_handle_member_update_skips_refresh_when_initial_match_succeeds():
    member_role = SimpleNamespace(id=777)
    guild = SimpleNamespace(get_role=lambda role_id: member_role if role_id == 777 else None)
    before = SimpleNamespace(nick="Old Name")
    after = SimpleNamespace(
        id=42,
        name="jamie",
        display_name="Jamie",
        global_name=None,
        mention="<@42>",
        nick="King Levy",
        guild=guild,
        roles=[],
    )

    with (
        patch("runtime.onboarding.db.upsert_discord_user"),
        patch("runtime.onboarding.db.link_discord_user_to_member"),
        patch("runtime.onboarding.refresh_clan_roster_from_api", new=AsyncMock(return_value=True)) as mock_refresh,
        patch("runtime.onboarding._ensure_member_role", new=AsyncMock(return_value=(True, "Granted"))) as mock_grant,
        patch("runtime.onboarding._send_onboarding_message", new=AsyncMock()) as mock_send,
        patch("runtime.app.MEMBER_ROLE_ID", 777),
        patch("runtime.app._match_clan_member", return_value=("#ABC123", "King Levy")) as mock_match,
    ):
        asyncio.run(onboarding.handle_member_update(before, after))

    mock_refresh.assert_not_awaited()
    mock_match.assert_called_once_with("King Levy")
    mock_grant.assert_awaited_once()
    mock_send.assert_awaited_once()
