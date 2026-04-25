"""prompts.py — Load externalized prompt files for Elixir.

Reads markdown files from the prompts/ directory and parses
configurable values from CLAN.md and DISCORD.md.
"""

import os
import re
from datetime import date, datetime

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
_SUBAGENT_PROMPTS_DIR = os.path.join(_PROMPTS_DIR, "subagents")

CHANNEL_SUBAGENT_CONFIG = {
    "promote-the-clan": {
        "workflow": "site_promote_content",
        "tool_policy": "none",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": False,
    },
    "poapkings-com": {
        "workflow": "channel_update",
        "tool_policy": "read_only",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": False,
    },
    "announcements": {
        "workflow": "weekly_digest",
        "tool_policy": "read_only",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "river-race": {
        "workflow": "channel_update",
        "tool_policy": "read_only",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "player-progress": {
        "workflow": "channel_update",
        "tool_policy": "read_only",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "trophy-road": {
        "workflow": "channel_update",
        "tool_policy": "read_only",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "clan-events": {
        "workflow": "channel_update",
        "tool_policy": "read_only",
        "reply_policy": "disabled",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "ask-elixir": {
        "workflow": "interactive",
        "tool_policy": "read_only",
        "reply_policy": "open_channel",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "reception": {
        "workflow": "reception",
        "tool_policy": "none",
        "reply_policy": "open_channel",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": False,
    },
    "general": {
        "workflow": "interactive",
        "tool_policy": "read_only",
        "reply_policy": "mention_only",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "war-talk": {
        "workflow": "interactive",
        "tool_policy": "read_only",
        "reply_policy": "mention_only",
        "singleton": True,
        "memory_scope": "public",
        "durable_memory_enabled": True,
    },
    "leader-lounge": {
        "workflow": "clanops",
        "tool_policy": "read_write",
        "reply_policy": "mention_only",
        "singleton": True,
        "memory_scope": "leadership",
        "durable_memory_enabled": True,
    },
}

SUBAGENT_ALIASES = {
    "onboarding": "reception",
    "weekly_digest": "announcements",
    "promotion": "promote-the-clan",
    "river_race": "river-race",
    "player_progress": "player-progress",
    "trophy_road": "trophy-road",
    "clan_events": "clan-events",
    "clanops": "leader-lounge",
    "ask_elixir": "ask-elixir",
    "poapkings_com": "poapkings-com",
}


def _normalize_subagent_name(value: str | None) -> str:
    key = (value or "").strip().lower()
    return SUBAGENT_ALIASES.get(key, key)


VALID_CHANNEL_WORKFLOWS = {
    None,
    "reception",
    "interactive",
    "clanops",
    "channel_update",
    "weekly_digest",
    "site_promote_content",
}
VALID_TOOL_POLICIES = {"none", "read_only", "read_write"}
VALID_MEMORY_SCOPES = {"public", "leadership"}
VALID_REPLY_POLICIES = {"disabled", "mention_only", "open_channel"}


def _parse_channel_field(section: str, label: str) -> str | None:
    match = re.search(rf"^{re.escape(label)}:\s*(.+?)\s*$", section, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _parse_bool_field(section: str, label: str) -> bool | None:
    value = _parse_channel_field(section, label)
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    raise ValueError(f"invalid boolean for {label}: {value}")


def _parse_optional_keyword(section: str, label: str) -> str | None:
    value = _parse_channel_field(section, label)
    if value is None:
        return None
    value = value.strip().lower()
    return None if value in {"", "none", "null"} else value


def validate_discord_channel_config():
    """Return a list of config errors found in DISCORD.md channel definitions."""
    channels = discord_channel_configs()
    errors = []

    seen_ids = {}
    seen_names = {}
    for channel in channels:
        subagent = channel["subagent"]
        if subagent not in CHANNEL_SUBAGENT_CONFIG:
            errors.append(f"unknown channel subagent '{subagent}' for {channel['name']}")
        workflow = channel.get("workflow")
        if workflow not in VALID_CHANNEL_WORKFLOWS:
            errors.append(f"invalid workflow '{workflow}' for {channel['name']}")
        tool_policy = channel.get("tool_policy")
        if tool_policy not in VALID_TOOL_POLICIES:
            errors.append(f"invalid tool policy '{tool_policy}' for {channel['name']}")
        reply_policy = channel.get("reply_policy")
        if reply_policy not in VALID_REPLY_POLICIES:
            errors.append(f"invalid reply policy '{reply_policy}' for {channel['name']}")
        memory_scope = channel.get("memory_scope")
        if memory_scope not in VALID_MEMORY_SCOPES:
            errors.append(f"invalid memory scope '{memory_scope}' for {channel['name']}")
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

    for subagent, config in CHANNEL_SUBAGENT_CONFIG.items():
        if not config.get("singleton"):
            continue
        matching = [channel for channel in channels if channel["subagent"] == subagent]
        if len(matching) != 1:
            errors.append(f"expected exactly one {subagent} channel, found {len(matching)}")

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


def _load_subagent_prompt(filename):
    path = os.path.join(_SUBAGENT_PROMPTS_DIR, filename)
    with open(path) as f:
        return f.read().strip()


def purpose():
    """Elixir's identity, voice, personality."""
    return _load("PURPOSE.md")


def soul():
    """Elixir's inner orientation and agentic identity."""
    return _load("SOUL.md")


def game():
    """Clash Royale game mechanics."""
    return _load("GAME.md")


def _clan_raw():
    """Unsubstituted CLAN.md — used by parsers (thresholds, clan_tag) that
    must not trigger phase substitution and by clan_phase itself to avoid a
    circular dependency with clan().
    """
    return _load("CLAN.md")


def clan(today: date | None = None) -> str:
    """Clan identity, rules, history, thresholds.

    Substitutes <<CLAN_AGE_TEXT>> and <<CLAN_PHASE_BEAT>> tokens in CLAN.md
    with phase-aware prose so the Current Stage section ages with the clan.
    Pass ``today`` for deterministic test output.
    """
    raw = _clan_raw()
    phase = clan_phase(today=today)
    return (
        raw
        .replace("<<CLAN_AGE_TEXT>>", phase["phase_text"])
        .replace("<<CLAN_PHASE_BEAT>>", phase["phase_beat"])
    )


def discord():
    """Discord channel structure, behaviors, and config."""
    return _load("DISCORD.md")


def channel_section(channel_name):
    """Extract a single channel's section from DISCORD.md.

    channel_name: e.g. "#ask-elixir", "#leader-lounge", "#reception"
    Returns the text from that channel's heading to the next ## heading (or EOF).
    """
    for channel in discord_channel_configs():
        if channel["name"] == channel_name:
            return channel["section"]
    return ""


def _channel_subagent_key(channel_name: str) -> str:
    key = (channel_name or "").strip().lower()
    if key.startswith("#"):
        key = key[1:]
    return re.sub(r"[^a-z0-9-]+", "-", key).strip("-")


def subagent_key_for_channel(channel_name: str, workflow: str | None = None) -> str:
    """Resolve the best subagent key for a channel/workflow pair.

    Configured channels use their explicit subagent key. Unknown channels fall
    back to the generic subagent for their workflow so ad hoc interactive or
    leadership channels do not require dedicated prompt files.
    """
    query = (channel_name or "").strip().lower()
    if query:
        for channel in discord_channel_configs():
            if channel["name"].lower() == query:
                return channel["subagent_key"]

    workflow_key = (workflow or "").strip().lower()
    if workflow_key.startswith("interactive"):
        return "general"
    if workflow_key.startswith("clanops"):
        return "leader-lounge"
    if workflow_key == "reception":
        return "reception"
    if workflow_key in {"weekly_digest", "announcements"}:
        return "announcements"

    return _channel_subagent_key(channel_name)


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
        subagent_match = re.search(r"^Subagent:\s*([A-Za-z0-9_-]+)\s*$", section, re.MULTILINE)
        if not id_match or not subagent_match:
            continue

        subagent = _normalize_subagent_name(subagent_match.group(1))
        channel_id = int(id_match.group(1))
        subagent_config = CHANNEL_SUBAGENT_CONFIG.get(subagent, {})
        workflow = _parse_optional_keyword(section, "Workflow")
        if workflow is None:
            workflow = subagent_config.get("workflow")
        tool_policy = _parse_optional_keyword(section, "ToolPolicy")
        if tool_policy is None:
            tool_policy = subagent_config.get("tool_policy", "none")
        reply_policy = _parse_optional_keyword(section, "ReplyPolicy")
        if reply_policy is None:
            reply_policy = subagent_config.get("reply_policy", "disabled")
        memory_scope = _parse_optional_keyword(section, "MemoryScope")
        if memory_scope is None:
            memory_scope = subagent_config.get("memory_scope", "public")
        durable_memory_enabled = _parse_bool_field(section, "DurableMemory")
        if durable_memory_enabled is None:
            durable_memory_enabled = subagent_config.get("durable_memory_enabled", False)

        channels.append(
            {
                "name": heading,
                "id": channel_id,
                "subagent": subagent,
                "subagent_key": subagent,
                "workflow": workflow,
                "tool_policy": tool_policy,
                "reply_policy": reply_policy,
                "singleton": subagent_config.get("singleton", False),
                "memory_scope": memory_scope,
                "durable_memory_enabled": durable_memory_enabled,
                "section": section,
            }
        )
    return channels


def discord_channels_by_id():
    """Return parsed Discord channel config keyed by numeric channel ID."""
    return {channel["id"]: channel for channel in discord_channel_configs()}


def discord_channels_for_subagent(subagent):
    """Return parsed Discord channel configs for a subagent."""
    subagent = _normalize_subagent_name(subagent)
    return [channel for channel in discord_channel_configs() if channel["subagent"] == subagent]


def discord_channels_by_workflow(workflow):
    """Return parsed Discord channel configs for a workflow family."""
    workflow = (workflow or "").strip().lower()
    return [channel for channel in discord_channel_configs() if (channel.get("workflow") or "").lower() == workflow]


def discord_channels_by_subagent():
    """Return parsed Discord channel configs keyed by subagent key."""
    return {channel["subagent_key"]: channel for channel in discord_channel_configs()}


def discord_singleton_subagent(subagent):
    """Return the unique configured channel for a singleton subagent."""
    subagent = _normalize_subagent_name(subagent)
    subagent_config = CHANNEL_SUBAGENT_CONFIG.get(subagent, {})
    if not subagent_config.get("singleton"):
        raise ValueError(f"subagent is not singleton: {subagent}")
    channels = discord_channels_for_subagent(subagent)
    if len(channels) != 1:
        raise ValueError(f"expected exactly one {subagent} channel, found {len(channels)}")
    return channels[0]


def discord_channels_by_role(role):
    """Backward-compatible alias for discord_channels_for_subagent()."""
    return discord_channels_for_subagent(role)


def discord_singleton_channel(role):
    """Backward-compatible alias for discord_singleton_subagent()."""
    return discord_singleton_subagent(role)


def resolve_channel_reference(value):
    """Resolve a channel by exact heading name or singleton subagent."""
    query = (value or "").strip().lower()
    if not query:
        return None
    for channel in discord_channel_configs():
        if channel["name"].lower() == query:
            return channel
    query = _normalize_subagent_name(query)
    subagent_config = CHANNEL_SUBAGENT_CONFIG.get(query)
    if subagent_config and subagent_config.get("singleton"):
        channels = discord_channels_for_subagent(query)
        if len(channels) == 1:
            return channels[0]
    return None


def subagent_prompt(subagent_key: str) -> str:
    """Load a subagent prompt file from prompts/subagents."""
    key = (subagent_key or "").strip().lower()
    if not key:
        return ""
    filename = f"{key}.md"
    return _load_subagent_prompt(filename)


def identity_block():
    """Combined identity stack for Elixir's stable sense of self."""
    return f"{soul()}\n\n{purpose()}"


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

    Returns dict of {key: int_value}. Reads the raw file to avoid triggering
    clan-phase substitution (which itself depends on this).
    """
    return _parse_config_section(_clan_raw(), "Thresholds")


# Phase boundaries in days. 30.4375 days/month is the mean Gregorian month
# length, so day↔month conversion stays close to a calendar reading.
_DAYS_PER_MONTH = 30.4375
_PHASE_BOUNDARIES_DAYS = {
    "founding": 92,         # < 3 months
    "establishing": 274,    # < 9 months
    "established": 731,     # < 2 years
}

_PHASE_TEXTS = {
    "founding": "POAP KINGS is {age} old, still in its founding era.",
    "establishing": "POAP KINGS is {age} old and is in its establishing era.",
    "established": "POAP KINGS has been building for {age} and is in its established era.",
    "mature": "POAP KINGS has been around for {age} and is a mature clan.",
}

_PHASE_BEATS = {
    "founding": "The founding era is still happening right now.",
    "establishing": "The earliest founding moments are recent memory; the clan is settling into its identity.",
    "established": "The founding era is well behind us; building the clan is steady, deliberate work now.",
    "mature": "Many seasons of clan history are behind us.",
}

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve",
}


def _spell_or_numeric(n: int) -> str:
    return _NUMBER_WORDS.get(n, str(n))


def _format_age_phrase(days: int, months: float) -> str:
    """Natural-language age, e.g. 'two months', '14 months', 'five years'."""
    if days <= 0:
        return "brand new"
    if days < 14:
        return "less than two weeks"
    if days < 30:
        return "less than a month"
    months_int = int(round(months))
    # Months read more concretely under two years; switch to years at 24+ so
    # the year-2 boundary lands cleanly on "two years" rather than "24 months".
    if months_int < 24:
        unit = "month" if months_int == 1 else "months"
        return f"{_spell_or_numeric(months_int)} {unit}"
    years_int = months_int // 12
    unit = "year" if years_int == 1 else "years"
    return f"{_spell_or_numeric(years_int)} {unit}"


def _phase_for_days(days: int) -> str:
    if days < _PHASE_BOUNDARIES_DAYS["founding"]:
        return "founding"
    if days < _PHASE_BOUNDARIES_DAYS["establishing"]:
        return "establishing"
    if days < _PHASE_BOUNDARIES_DAYS["established"]:
        return "established"
    return "mature"


def clan_phase(today: date | None = None) -> dict:
    """Return clan age + phase classification.

    Args:
        today: Reference date for testability. Defaults to ``date.today()``.

    Returns:
        ``{"founded": str, "days": int, "months": float, "phase": str,
        "phase_text": str, "phase_beat": str}``
    """
    if today is None:
        today = date.today()
    founded_str = thresholds().get("clan_founded")
    founded = datetime.strptime(founded_str, "%Y-%m-%d").date() if founded_str else today
    days = max(0, (today - founded).days)
    months = round(days / _DAYS_PER_MONTH, 1)
    phase = _phase_for_days(days)
    age = _format_age_phrase(days, days / _DAYS_PER_MONTH)
    return {
        "founded": founded_str,
        "days": days,
        "months": months,
        "phase": phase,
        "phase_text": _PHASE_TEXTS[phase].format(age=age),
        "phase_beat": _PHASE_BEATS[phase],
    }


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
