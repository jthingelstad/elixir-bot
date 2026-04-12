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

    def test_fallback_roster_map_folds_diacritics(self):
        with patch("elixir.db.resolve_member", return_value=[]), \
             patch("elixir.db.get_active_roster_map", return_value={"#ABC123": "José"}):
            result = elixir._match_clan_member("jose")
        assert result == ("#ABC123", "José")


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


class TestRemoveMemberRoleForTag:
    """Tests for remove_member_role_for_tag called when a clan member leaves."""

    def _run(self, *, identity, guild_member, member_role, manage_roles=True, forbidden=False):
        guild = SimpleNamespace(
            get_member=lambda uid: guild_member,
            get_role=lambda rid: member_role,
        )
        bot = SimpleNamespace(get_guild=lambda gid: guild)
        remove_mock = AsyncMock(side_effect=__import__("discord").Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "forbidden",
        )) if forbidden else AsyncMock()
        if guild_member is not None:
            guild_member.remove_roles = remove_mock

        with (
            patch("runtime.onboarding.db.get_member_identity", return_value=identity),
            patch("runtime.app.MEMBER_ROLE_ID", 777),
            patch("runtime.app.GUILD_ID", 100),
            patch("runtime.app.bot", new=bot),
            patch("runtime.app._member_role_grant_status", return_value={"ok": manage_roles, "reason": "x"}),
        ):
            return asyncio.run(
                onboarding.remove_member_role_for_tag("#ABC123", reason="left clan"),
            ), remove_mock

    def test_removes_role_when_linked_and_present(self):
        role = SimpleNamespace(id=777)
        guild_member = SimpleNamespace(id=555, display_name="King Levy", roles=[role])
        (ok, detail), remove_mock = self._run(
            identity={"discord_user_id": "555"},
            guild_member=guild_member,
            member_role=role,
        )
        assert ok is True
        assert "Removed Member role" in detail
        remove_mock.assert_awaited_once()

    def test_noop_when_role_already_absent(self):
        role = SimpleNamespace(id=777)
        guild_member = SimpleNamespace(id=555, display_name="King Levy", roles=[])
        (ok, detail), remove_mock = self._run(
            identity={"discord_user_id": "555"},
            guild_member=guild_member,
            member_role=role,
        )
        assert ok is True
        assert "did not have the Member role" in detail
        remove_mock.assert_not_awaited()

    def test_no_link_short_circuits(self):
        (ok, detail), remove_mock = self._run(
            identity={"discord_user_id": None},
            guild_member=None,
            member_role=None,
        )
        assert ok is False
        assert "No linked Discord user" in detail
        remove_mock.assert_not_awaited()

    def test_guild_member_missing(self):
        role = SimpleNamespace(id=777)
        (ok, detail), remove_mock = self._run(
            identity={"discord_user_id": "555"},
            guild_member=None,
            member_role=role,
        )
        assert ok is False
        assert "not in guild" in detail
        remove_mock.assert_not_awaited()

    def test_forbidden_returns_false(self):
        role = SimpleNamespace(id=777)
        guild_member = SimpleNamespace(id=555, display_name="King Levy", roles=[role])
        (ok, detail), _ = self._run(
            identity={"discord_user_id": "555"},
            guild_member=guild_member,
            member_role=role,
            forbidden=True,
        )
        assert ok is False
        assert "Discord permissions" in detail
