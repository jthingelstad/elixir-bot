from agent.tool_defs import TOOLS

_WRITE_TOOL_NAMES = {"update_member", "save_clan_memory"}

TOOL_DEFINITIONS = []
for _tool in TOOLS:
    _name = _tool["name"]
    _side_effect = "write" if _name in _WRITE_TOOL_NAMES else "read"
    TOOL_DEFINITIONS.append({
        "tool": _tool,
        "name": _name,
        "side_effect": _side_effect,
    })

TOOL_DEFINITIONS_BY_NAME = {
    d["name"]: d for d in TOOL_DEFINITIONS
}

READ_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "read"]
WRITE_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "write"]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS

# get_clan_health has sensitive aspects (at_risk, promotion_candidates) but
# aspect-level gating is handled in tool_exec.py, so we keep it available
# to all workflows. This avoids confusing the LLM by hiding the tool entirely.
INTERACTIVE_READ_TOOLS = READ_TOOLS

TOOLSETS_BY_WORKFLOW = {
    "observe": INTERACTIVE_READ_TOOLS,
    "channel_update": INTERACTIVE_READ_TOOLS,
    "channel_update_leadership": READ_TOOLS,
    "interactive": INTERACTIVE_READ_TOOLS,
    "clanops": ALL_TOOLS,
    "reception": [],
    "roster_bios": READ_TOOLS,
}

MAX_ROUNDS_BY_WORKFLOW = {
    "clanops": 5,
    "channel_update_leadership": 5,
    "interactive": 3,
    "observation": 3,
    "observe": 3,
    "channel_update": 3,
    "reception": 0,
    "roster_bios": 3,
}

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    "observation": {"required": ["event_type", "summary", "content"]},
    "channel_update": {"required": ["event_type", "summary", "content"]},
    "channel_update_leadership": {"required": ["event_type", "summary", "content"]},
    "interactive": {"required": ["event_type", "summary", "content"]},
    "clanops": {"required": ["event_type", "summary", "content"]},
    "reception": {"required": ["event_type", "content"]},
    "roster_bios": {"required": ["intro", "members"]},
}
