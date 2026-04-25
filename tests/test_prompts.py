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
    assert "#leader-lounge" in text
    assert "#reception" in text
    assert "#poapkings-com" in text


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


def test_reception_channel_is_open_channel():
    channel = prompts.discord_singleton_subagent("reception")
    assert channel["reply_policy"] == "open_channel"


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

    assert channels[200]["workflow"] == "clanops"
    assert channels[200]["tool_policy"] == "read_write"
    assert channels[200]["reply_policy"] == "mention_only"
    assert channels[200]["memory_scope"] == "leadership"
    assert channels[300]["subagent"] == "poapkings-com"
    assert channels[300]["reply_policy"] == "disabled"
    assert channels[300]["durable_memory_enabled"] is False
    assert prompts.discord_singleton_subagent("leader-lounge")["id"] == 200



def test_validate_discord_channel_config_flags_singleton_errors(monkeypatch):
    monkeypatch.setattr(
        prompts,
        "discord",
        lambda: (
            "## Config\n\n"
            "- application_id: 1\n\n"
            "## #one\n\n"
            "ID: 100\n"
            "Subagent: leader-lounge\n\n"
            "Primary leadership room.\n\n"
            "## #two\n\n"
            "ID: 101\n"
            "Subagent: leader-lounge\n\n"
            "Duplicate singleton.\n"
        ),
    )
    errors = prompts.validate_discord_channel_config()

    assert any("expected exactly one leader-lounge channel" in error for error in errors)


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


def test_clan_phase_founding_phase():
    """0-91 days inclusive is founding."""
    from datetime import date
    p = prompts.clan_phase(today=date(2026, 4, 25))  # day 80
    assert p["phase"] == "founding"
    assert p["days"] == 80
    assert "founding era" in p["phase_text"]
    assert p["phase_beat"] == "The founding era is still happening right now."


def test_clan_phase_establishing_phase():
    """92-273 days inclusive is establishing."""
    from datetime import date
    p = prompts.clan_phase(today=date(2026, 5, 7))  # day 92
    assert p["phase"] == "establishing"
    assert p["days"] == 92
    assert "establishing era" in p["phase_text"]


def test_clan_phase_established_phase():
    """274-730 days inclusive is established."""
    from datetime import date
    p = prompts.clan_phase(today=date(2026, 11, 5))  # day 274
    assert p["phase"] == "established"
    assert p["days"] == 274
    assert "established era" in p["phase_text"]


def test_clan_phase_mature_phase():
    """731+ days is mature."""
    from datetime import date
    p = prompts.clan_phase(today=date(2028, 2, 5))  # day 731 (2y+1d)
    assert p["phase"] == "mature"
    assert p["days"] == 731
    assert "mature clan" in p["phase_text"]


def test_clan_phase_boundary_91_to_92_days():
    """Phase flip from founding → establishing at day 92."""
    from datetime import date
    assert prompts.clan_phase(today=date(2026, 5, 6))["phase"] == "founding"      # day 91
    assert prompts.clan_phase(today=date(2026, 5, 7))["phase"] == "establishing"  # day 92


def test_clan_phase_boundary_273_to_274_days():
    """Phase flip from establishing → established at day 274."""
    from datetime import date
    assert prompts.clan_phase(today=date(2026, 11, 4))["phase"] == "establishing"  # day 273
    assert prompts.clan_phase(today=date(2026, 11, 5))["phase"] == "established"   # day 274


def test_clan_phase_boundary_730_to_731_days():
    """Phase flip from established → mature at day 731."""
    from datetime import date
    assert prompts.clan_phase(today=date(2028, 2, 4))["phase"] == "established"  # day 730
    assert prompts.clan_phase(today=date(2028, 2, 5))["phase"] == "mature"       # day 731


def test_clan_phase_natural_phrasing_at_milestone_dates():
    """phase_text reads naturally at month-1, month-3, month-9, year-2, year-5."""
    from datetime import date
    cases = [
        (date(2026, 3, 6), "one month"),       # 30 days
        (date(2026, 5, 6), "three months"),    # day 91, edge of founding
        (date(2026, 11, 5), "nine months"),    # day 274, just established
        (date(2028, 2, 5), "two years"),       # day 731, just mature
        (date(2031, 2, 4), "five years"),      # ~1826 days
    ]
    for today, expected_age in cases:
        p = prompts.clan_phase(today=today)
        assert expected_age in p["phase_text"], (
            f"expected '{expected_age}' in phase_text for {today}, got: {p['phase_text']}"
        )


def test_clan_phase_handles_pre_founding_date():
    """A reference date before clan_founded clamps to 0 days, brand-new phrasing."""
    from datetime import date
    p = prompts.clan_phase(today=date(2025, 1, 1))
    assert p["days"] == 0
    assert p["phase"] == "founding"
    assert "brand new" in p["phase_text"]


def test_clan_substitutes_age_and_phase_tokens():
    """clan() replaces both <<CLAN_AGE_TEXT>> and <<CLAN_PHASE_BEAT>>."""
    from datetime import date
    text = prompts.clan(today=date(2026, 4, 25))  # founding, day 80
    assert "<<CLAN_AGE_TEXT>>" not in text
    assert "<<CLAN_PHASE_BEAT>>" not in text
    assert "POAP KINGS is three months old" in text
    assert "founding era is still happening" in text


def test_clan_substitution_varies_by_phase():
    """Same CLAN.md produces different prose at different phases."""
    from datetime import date
    founding = prompts.clan(today=date(2026, 4, 25))
    mature = prompts.clan(today=date(2031, 2, 4))
    assert "founding era is still happening" in founding
    assert "founding era is still happening" not in mature
    assert "mature clan" in mature
    assert "mature clan" not in founding


def test_thresholds_does_not_trigger_substitution():
    """thresholds() reads the raw CLAN.md, no substitution on tokens."""
    t = prompts.thresholds()
    assert t["clan_founded"] == "2026-02-04"
    # Confirms no AttributeError or recursion through clan_phase.


def test_observation_prompt_includes_custom_emoji_guidance():
    system_prompt = agent_prompts._observe_system()

    assert "Use readable Discord-native formatting." in system_prompt
    assert "Keep most messages compact unless the task genuinely calls for more structure." in system_prompt
    assert "Use occasional **bold** emphasis" in system_prompt
    assert "Elixir server custom emoji" in system_prompt
    assert "Do not invent custom emoji names" in system_prompt
    assert "channel subagent" in system_prompt
    assert "Default to one Discord message" in system_prompt
    assert "Do not split one update across multiple near-duplicate messages." in system_prompt
