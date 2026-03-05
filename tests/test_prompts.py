"""Tests for prompts.py — prompt file loading and threshold parsing."""

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


def test_channels_loads():
    """CHANNELS.md loads and contains channel definitions."""
    text = prompts.channels()
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
