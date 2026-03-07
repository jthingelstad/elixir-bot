"""prompts.py — Load externalized prompt files for Elixir.

Reads markdown files from the prompts/ directory and parses
configurable values from CLAN.md and DISCORD.md.
"""

import os
import re

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

CHANNEL_ROLE_CONFIG = {
    "announcements": {
        "workflow": None,
        "mention_required": False,
        "allow_proactive": False,
        "singleton": True,
        "respond_allowed": False,
    },
    "promotion": {
        "workflow": None,
        "mention_required": False,
        "allow_proactive": False,
        "singleton": True,
        "respond_allowed": False,
    },
    "arena_relay": {
        "workflow": None,
        "mention_required": False,
        "allow_proactive": False,
        "singleton": True,
        "respond_allowed": False,
    },
    "onboarding": {
        "workflow": "reception",
        "mention_required": True,
        "allow_proactive": False,
        "singleton": True,
        "respond_allowed": True,
    },
    "interactive": {
        "workflow": "interactive",
        "mention_required": True,
        "allow_proactive": False,
        "singleton": False,
        "respond_allowed": True,
    },
    "clanops": {
        "workflow": "clanops",
        "mention_required": False,
        "allow_proactive": True,
        "singleton": False,
        "respond_allowed": True,
    },
}


def validate_discord_channel_config():
    """Return a list of config errors found in DISCORD.md channel definitions."""
    channels = discord_channel_configs()
    errors = []

    seen_ids = {}
    seen_names = {}
    for channel in channels:
        role = channel["role"]
        if role not in CHANNEL_ROLE_CONFIG:
            errors.append(f"unknown channel role '{role}' for {channel['name']}")
        if channel["id"] in seen_ids:
            errors.append(
                f"duplicate channel id {channel['id']} for {seen_ids[channel['id']]} and {channel['name']}"
            )
        else:
            seen_ids[channel["id"]] = channel["name"]
        if channel["name"].lower() in seen_names:
            errors.append(f"duplicate channel heading {channel['name']}")
        else:
            seen_names[channel["name"].lower()] = channel["id"]

    for role, config in CHANNEL_ROLE_CONFIG.items():
        if not config.get("singleton"):
            continue
        matching = [channel for channel in channels if channel["role"] == role]
        if len(matching) != 1:
            errors.append(f"expected exactly one {role} channel, found {len(matching)}")

    return errors


def ensure_valid_discord_channel_config():
    """Raise ValueError if DISCORD.md channel definitions are invalid."""
    errors = validate_discord_channel_config()
    if errors:
        raise ValueError("; ".join(errors))


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
    for channel in discord_channel_configs():
        if channel["name"] == channel_name:
            return channel["section"]
    return ""


def discord_channel_configs():
    """Parse DISCORD.md channel sections into structured channel config."""
    text = discord()
    heading_matches = list(re.finditer(r"^## (.+?)\s*$", text, re.MULTILINE))
    channels = []

    for i, match in enumerate(heading_matches):
        heading = match.group(1).strip()
        if heading == "Config":
            continue
        start = match.start()
        end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
        section = text[start:end].strip()

        id_match = re.search(r"^ID:\s*(\d+)\s*$", section, re.MULTILINE)
        role_match = re.search(r"^Role:\s*([A-Za-z0-9_-]+)\s*$", section, re.MULTILINE)
        if not id_match or not role_match:
            continue

        role = role_match.group(1).strip().lower()
        channel_id = int(id_match.group(1))
        role_config = CHANNEL_ROLE_CONFIG.get(role, {})

        channels.append(
            {
                "name": heading,
                "id": channel_id,
                "role": role,
                "workflow": role_config.get("workflow"),
                "mention_required": role_config.get("mention_required", True),
                "allow_proactive": role_config.get("allow_proactive", False),
                "singleton": role_config.get("singleton", False),
                "respond_allowed": role_config.get("respond_allowed", True),
                "section": section,
            }
        )
    return channels


def discord_channels_by_id():
    """Return parsed Discord channel config keyed by numeric channel ID."""
    return {channel["id"]: channel for channel in discord_channel_configs()}


def discord_channels_by_role(role):
    """Return parsed Discord channel configs for a role."""
    role = (role or "").strip().lower()
    return [channel for channel in discord_channel_configs() if channel["role"] == role]


def discord_singleton_channel(role):
    """Return the unique configured channel for a singleton role."""
    role = (role or "").strip().lower()
    role_config = CHANNEL_ROLE_CONFIG.get(role, {})
    if not role_config.get("singleton"):
        raise ValueError(f"role is not singleton: {role}")
    channels = discord_channels_by_role(role)
    if len(channels) != 1:
        raise ValueError(f"expected exactly one {role} channel, found {len(channels)}")
    return channels[0]


def resolve_channel_reference(value):
    """Resolve a channel by exact heading name or singleton role."""
    query = (value or "").strip().lower()
    if not query:
        return None
    for channel in discord_channel_configs():
        if channel["name"].lower() == query:
            return channel
    role_config = CHANNEL_ROLE_CONFIG.get(query)
    if role_config and role_config.get("singleton"):
        channels = discord_channels_by_role(query)
        if len(channels) == 1:
            return channels[0]
    return None


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
