"""prompts.py — Load externalized prompt files for Elixir.

Reads markdown files from the prompts/ directory and parses
configurable thresholds from CLAN.md.
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


def channels():
    """Discord channel structure and behaviors."""
    return _load("CHANNELS.md")


def channel_section(channel_name):
    """Extract a single channel's section from CHANNELS.md.

    channel_name: e.g. "#elixir", "#leader-lounge", "#reception"
    Returns the text from that channel's heading to the next ## heading (or EOF).
    """
    text = channels()
    pattern = rf"(## {re.escape(channel_name)}\s*\n.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def knowledge_block():
    """Combined game + clan knowledge for LLM system prompts."""
    return f"{game()}\n\n{clan()}"


def thresholds():
    """Parse the ## Thresholds section from CLAN.md into a dict.

    Expected format:
        ## Thresholds
        - key: value
        - key: value

    Returns dict of {key: int_value}.
    """
    text = clan()
    section_match = re.search(
        r"## Thresholds\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL
    )
    if not section_match:
        return {}

    result = {}
    for line in section_match.group(1).strip().splitlines():
        m = re.match(r"-\s*(\w+)\s*:\s*(\d+)", line)
        if m:
            result[m.group(1)] = int(m.group(2))
    return result
