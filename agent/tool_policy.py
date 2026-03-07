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

TOOLSETS_BY_WORKFLOW = {
    "observe": READ_TOOLS,
    "interactive": READ_TOOLS,
    "interactive_proactive": READ_TOOLS,
    "clanops": ALL_TOOLS,
    "clanops_proactive": ALL_TOOLS,
    "reception": [],
    "roster_bios": READ_TOOLS,
}

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    "observation": {"required": ["event_type", "summary", "content"]},
    "interactive": {"required": ["event_type", "summary", "content"]},
    "interactive_proactive": {"required": ["event_type", "summary", "content"]},
    "clanops": {"required": ["event_type", "summary", "content"]},
    "clanops_proactive": {"required": ["event_type", "summary", "content"]},
    "reception": {"required": ["event_type", "content"]},
    "roster_bios": {"required": ["intro", "members"]},
}

