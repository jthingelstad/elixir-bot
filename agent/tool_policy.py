from agent.tool_defs import TOOLS

TOOL_DEFINITIONS = []
for _tool in TOOLS:
    _name = _tool["function"]["name"]
    _side_effect = "write" if _name.startswith("set_member_") else "read"
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
SENSITIVE_READ_TOOL_NAMES = {"get_promotion_candidates", "get_members_at_risk"}
INTERACTIVE_READ_TOOLS = [
    tool for tool in READ_TOOLS if tool["function"]["name"] not in SENSITIVE_READ_TOOL_NAMES
]

TOOLSETS_BY_WORKFLOW = {
    "observe": INTERACTIVE_READ_TOOLS,
    "channel_update": INTERACTIVE_READ_TOOLS,
    "channel_update_leadership": READ_TOOLS,
    "interactive": INTERACTIVE_READ_TOOLS,
    "clanops": ALL_TOOLS,
    "reception": [],
    "roster_bios": READ_TOOLS,
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
