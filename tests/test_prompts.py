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
    assert "broadcast" in section.lower()
    assert "#elixir" in section


def test_channel_section_leader():
    """Extracts #leader-lounge section."""
    section = prompts.channel_section("#leader-lounge")
    assert "interactive" in section.lower()
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
    assert dc["announcements_channel"] == 1477043729503359198
    assert dc["leadership_channel"] == 1475139718525227089
    assert dc["reception_channel"] == 1476456514121109514
    assert dc["member_role"] == 1474762690692911104


def test_clan_tag():
    """Clan tag is extracted from CLAN.md."""
    tag = prompts.clan_tag()
    assert tag == "J2RGCRVG"
