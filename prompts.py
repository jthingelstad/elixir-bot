"""prompts.py — Load externalized prompt files for Elixir.

Reads markdown files from the prompts/ directory and parses
configurable values from CLAN.md and DISCORD.md.
"""

import os
import re

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load(filename):
    """Load a prompt file and return its contents as a string."""
    path = os.path.join(_PROMPTS_DIR, filename)
    with open(path) as f:
        return f.read().strip()


def purpose():
    """Elixir's identity, voice, personality."""
    return _load("PURPOSE.md")


def game():
    """Clash Royale game mechanics."""
    return _load("GAME.md")


def clan():
    """Clan identity, rules, history, thresholds."""
    return _load("CLAN.md")


def discord():
    """Discord channel structure, behaviors, and config."""
    return _load("DISCORD.md")


def channels():
    """Alias for discord() — kept for backward compatibility."""
    return discord()


def channel_section(channel_name):
    """Extract a single channel's section from DISCORD.md.

    channel_name: e.g. "#elixir", "#leader-lounge", "#reception"
    Returns the text from that channel's heading to the next ## heading (or EOF).
    """
    text = discord()
    pattern = rf"(## {re.escape(channel_name)}\s*\n.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def knowledge_block():
    """Combined game + clan knowledge for LLM system prompts."""
    return f"{game()}\n\n{clan()}"


def _parse_config_section(text, heading):
    """Parse a ## heading section with `- key: value` lines into a dict.

    Returns dict of {key: int_value} for numeric values, {key: str_value} otherwise.
    """
    section_match = re.search(
        rf"## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    if not section_match:
        return {}

    result = {}
    for line in section_match.group(1).strip().splitlines():
        m = re.match(r"-\s*([\w]+)\s*:\s*(.+)", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            try:
                result[key] = int(val)
            except ValueError:
                result[key] = val
    return result


def thresholds():
    """Parse the ## Thresholds section from CLAN.md into a dict.

    Returns dict of {key: int_value}.
    """
    return _parse_config_section(clan(), "Thresholds")


def discord_config():
    """Parse the ## Config section from DISCORD.md into a dict.

    Returns dict of {key: int_value} for Discord IDs.
    """
    return _parse_config_section(discord(), "Config")


def clan_tag():
    """Extract the clan tag from CLAN.md (e.g. 'J2RGCRVG').

    Parses from the 'Clan tag: #J2RGCRVG' line.
    """
    text = clan()
    m = re.search(r"Clan tag:\s*#?(\w+)", text)
    return m.group(1) if m else "J2RGCRVG"
