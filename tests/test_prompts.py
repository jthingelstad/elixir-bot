"""Tests for prompts.py — prompt file loading and config parsing."""

import prompts


def test_purpose_loads():
    """PURPOSE.md loads and contains identity info."""
    text = prompts.purpose()
    assert "Elixir" in text
    assert "chronicler" in text.lower()


def test_game_loads():
    """GAME.md loads and contains game mechanics."""
    text = prompts.game()
    assert "River Race" in text
    assert "THURSDAY" in text.upper()


def test_clan_loads():
    """CLAN.md loads and contains clan info."""
    text = prompts.clan()
    assert "POAP KINGS" in text
    assert "J2RGCRVG" in text


def test_discord_loads():
    """DISCORD.md loads and contains channel definitions."""
    text = prompts.discord()
    assert "#elixir" in text
    assert "#leader-lounge" in text
    assert "#reception" in text


def test_channel_section_elixir():
    """Extracts #elixir section."""
    section = prompts.channel_section("#elixir")
    assert "announcements" in section.lower()
    assert "#elixir" in section


def test_channel_section_leader():
    """Extracts #leader-lounge section."""
    section = prompts.channel_section("#leader-lounge")
    assert "clanops" in section.lower()
    assert "leader" in section.lower()


def test_channel_section_reception():
    """Extracts #reception section."""
    section = prompts.channel_section("#reception")
    assert "onboarding" in section.lower()
    assert "nickname" in section.lower()


def test_channel_section_nonexistent():
    """Returns empty string for unknown channel."""
    section = prompts.channel_section("#nonexistent")
    assert section == ""


def test_discord_channel_configs_parse_roles_and_policies(monkeypatch):
    monkeypatch.setattr(
        prompts,
        "discord",
        lambda: (
            "# Discord Channels\n\n"
            "## Config\n\n"
            "- application_id: 1\n\n"
            "## #member-chat\n\n"
            "ID: 100\n"
            "Role: interactive\n\n"
            "Read-only member Q&A.\n\n"
            "## #elixir\n\n"
            "ID: 150\n"
            "Role: announcements\n\n"
            "Main stage.\n\n"
            "## #clan-ops\n\n"
            "ID: 200\n"
            "Role: clanops\n\n"
            "Private operations.\n"
        ),
    )
    channels = prompts.discord_channels_by_id()

    assert channels[100]["workflow"] == "interactive"
    assert channels[100]["mention_required"] is True
    assert channels[100]["allow_proactive"] is False

    assert channels[150]["workflow"] is None
    assert channels[150]["singleton"] is True
    assert channels[150]["respond_allowed"] is False

    assert channels[200]["workflow"] == "clanops"
    assert channels[200]["mention_required"] is False
    assert channels[200]["allow_proactive"] is True

    assert prompts.discord_singleton_channel("announcements")["id"] == 150


def test_knowledge_block():
    """Combined knowledge includes both game and clan content."""
    block = prompts.knowledge_block()
    assert "River Race" in block
    assert "POAP KINGS" in block


def test_thresholds():
    """Thresholds are parsed from CLAN.md."""
    t = prompts.thresholds()
    assert t["trophy_milestone_interval"] == 1000
    assert t["trophy_milestone_max"] == 15000
    assert t["inactivity_days"] == 3
    assert t["donation_highlight_hour"] == 20


def test_discord_config():
    """Discord config IDs are parsed from DISCORD.md."""
    dc = prompts.discord_config()
    assert dc["application_id"] == 1477043197443182832
    assert dc["guild_id"] == 1474760692992180429
    assert dc["member_role"] == 1474762690692911104
    assert dc["bot_role"] == 1477050812789293117


def test_clan_tag():
    """Clan tag is extracted from CLAN.md."""
    tag = prompts.clan_tag()
    assert tag == "J2RGCRVG"
