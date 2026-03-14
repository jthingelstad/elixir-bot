"""Tests for prompts.py — prompt file loading and config parsing."""

import agent.prompts as agent_prompts
import prompts


def test_purpose_loads():
    """PURPOSE.md loads and contains identity info."""
    text = prompts.purpose()
    assert "Elixir" in text
    assert "Mission" in text


def test_soul_loads():
    """SOUL.md loads and contains Elixir's agentic identity."""
    text = prompts.soul()
    assert "Elixir" in text
    assert "agent" in text.lower()
    assert "not a person" in text.lower()


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
    assert "#poapkings-com" in text


def test_channel_section_elixir():
    """Extracts #elixir section."""
    section = prompts.channel_section("#elixir")
    assert "legacy" in section.lower()
    assert "#elixir" in section


def test_channel_section_leader():
    """Extracts #leader-lounge section."""
    section = prompts.channel_section("#leader-lounge")
    assert "leader-lounge" in section.lower()
    assert "leader" in section.lower()


def test_channel_section_reception():
    """Extracts #reception section."""
    section = prompts.channel_section("#reception")
    assert "onboarding" in section.lower()
    assert "nickname" in section.lower()


def test_channel_section_poapkings_com():
    """Extracts #poapkings-com section."""
    section = prompts.channel_section("#poapkings-com")
    assert "publish visibility" in section.lower()
    assert "github-backed site publish" in section.lower()


def test_channel_section_nonexistent():
    """Returns empty string for unknown channel."""
    section = prompts.channel_section("#nonexistent")
    assert section == ""


def test_discord_channel_configs_parse_subagents_and_policies(monkeypatch):
    monkeypatch.setattr(
        prompts,
        "discord",
        lambda: (
            "# Discord Channels\n\n"
            "## Config\n\n"
            "- application_id: 1\n\n"
            "## #member-chat\n\n"
            "ID: 100\n"
            "Subagent: general\n\n"
            "Workflow: interactive\n"
            "ToolPolicy: read_only\n"
            "ReplyPolicy: mention_only\n"
            "MemoryScope: public\n"
            "DurableMemory: true\n\n"
            "Read-only member Q&A.\n\n"
            "## #elixir\n\n"
            "ID: 150\n"
            "Subagent: legacy\n\n"
            "Workflow: none\n"
            "ToolPolicy: none\n"
            "ReplyPolicy: disabled\n"
            "MemoryScope: public\n"
            "DurableMemory: false\n\n"
            "Legacy room.\n\n"
            "## #leader-lounge\n\n"
            "ID: 200\n"
            "Subagent: leader-lounge\n\n"
            "Workflow: clanops\n"
            "ToolPolicy: read_write\n"
            "ReplyPolicy: mention_only\n"
            "MemoryScope: leadership\n"
            "DurableMemory: true\n\n"
            "Private operations.\n\n"
            "## #poapkings-com\n\n"
            "ID: 300\n"
            "Subagent: poapkings-com\n\n"
            "Workflow: channel_update\n"
            "ToolPolicy: read_only\n"
            "ReplyPolicy: disabled\n"
            "MemoryScope: public\n"
            "DurableMemory: false\n\n"
            "Publish visibility.\n"
        ),
    )
    channels = prompts.discord_channels_by_id()

    assert channels[100]["workflow"] == "interactive"
    assert channels[100]["subagent"] == "general"
    assert channels[100]["tool_policy"] == "read_only"
    assert channels[100]["reply_policy"] == "mention_only"
    assert "role" not in channels[100]
    assert "interaction_mode" not in channels[100]
    assert "mention_required" not in channels[100]
    assert "allow_proactive" not in channels[100]
    assert "respond_allowed" not in channels[100]

    assert channels[150]["workflow"] is None
    assert channels[150]["tool_policy"] == "none"
    assert channels[150]["reply_policy"] == "disabled"
    assert channels[150]["singleton"] is True
    assert channels[150]["subagent_key"] == "legacy"

    assert channels[200]["workflow"] == "clanops"
    assert channels[200]["tool_policy"] == "read_write"
    assert channels[200]["reply_policy"] == "mention_only"
    assert channels[200]["memory_scope"] == "leadership"
    assert channels[300]["subagent"] == "poapkings-com"
    assert channels[300]["reply_policy"] == "disabled"
    assert channels[300]["durable_memory_enabled"] is False
    assert prompts.discord_singleton_subagent("legacy")["id"] == 150


def test_subagent_prompt_poapkings_com_loads():
    text = prompts.subagent_prompt("poapkings-com")
    assert "POAP KINGS website publish outcomes" in text
    assert "commit sha" in text.lower()


def test_subagent_prompt_ask_elixir_mentions_conversational_followups():
    text = prompts.subagent_prompt("ask-elixir")
    assert "short follow-ups" in text.lower()
    assert "repeating the previous factual answer" in text.lower()
    assert "correct yourself" in text.lower()


def test_validate_discord_channel_config_flags_singleton_errors(monkeypatch):
    monkeypatch.setattr(
        prompts,
        "discord",
        lambda: (
            "## Config\n\n"
            "- application_id: 1\n\n"
            "## #one\n\n"
            "ID: 100\n"
            "Subagent: legacy\n\n"
            "Primary legacy room.\n\n"
            "## #two\n\n"
            "ID: 101\n"
            "Subagent: legacy\n\n"
            "Duplicate singleton.\n"
        ),
    )
    errors = prompts.validate_discord_channel_config()

    assert any("expected exactly one legacy channel" in error for error in errors)


def test_knowledge_block():
    """Combined knowledge includes both game and clan content."""
    block = prompts.knowledge_block()
    assert "River Race" in block
    assert "POAP KINGS" in block


def test_identity_block():
    """Combined identity includes soul and purpose."""
    block = prompts.identity_block()
    assert "Elixir's Soul" in block
    assert "Elixir's Purpose" in block


def test_thresholds():
    """Thresholds are parsed from CLAN.md."""
    t = prompts.thresholds()
    assert t["inactivity_days"] == 3
    assert t["donation_highlight_hour"] == 20


def test_discord_config():
    """Discord config IDs are parsed from DISCORD.md."""
    dc = prompts.discord_config()
    assert dc["application_id"] == 1477043197443182832
    assert dc["guild_id"] == 1474760692992180429
    assert dc["member_role"] == 1474762690692911104
    assert dc["leader_role"] == 1474762111287824584
    assert dc["bot_role"] == 1477050812789293117


def test_clan_tag():
    """Clan tag is extracted from CLAN.md."""
    tag = prompts.clan_tag()
    assert tag == "J2RGCRVG"


def test_observation_prompt_includes_custom_emoji_guidance():
    system_prompt = agent_prompts._observe_system()

    assert "Use at most 1-2 custom emoji in most messages." in system_prompt
    assert ":elixir_hype:" in system_prompt
    assert "channel subagent" in system_prompt
    assert "Default to one Discord message" in system_prompt
    assert "Do not split one update across multiple near-duplicate messages." in system_prompt
