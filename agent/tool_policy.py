from agent.tool_defs import TOOLS

_WRITE_TOOL_NAMES = {
    "update_member",
    "save_clan_memory",
    "flag_member_watch",
    "record_leadership_followup",
    "schedule_revisit",
}

# Write tools the awareness loop is allowed to call per tick. Intentionally a
# narrow subset of _WRITE_TOOL_NAMES: awareness can save memories, flag
# members, queue followups, and schedule revisits, but cannot mutate member
# metadata (that stays a human leadership action via the clanops path).
AWARENESS_WRITE_TOOL_NAMES = {
    "save_clan_memory",
    "flag_member_watch",
    "record_leadership_followup",
    "schedule_revisit",
}

# Max write-tool calls the awareness loop may make per tick. The agent is
# nudged by the system prompt to stay under this; chat.py enforces the cap and
# records issued/succeeded/denied counts in awareness_ticks.
AWARENESS_WRITE_BUDGET_PER_TICK = 3

# Tools that hit external (CR) APIs and should be rate-capped per LLM turn.
# Enforced by agent/chat.py::_chat_with_tools.
EXTERNAL_LOOKUP_TOOL_NAMES = {"cr_api"}

# Workflows that don't need the conversational CR bridge — they're one-shot
# observation/formatting jobs with narrow, local-only scopes. channel_update
# is intentionally NOT here: as of v4.5 the proactive channel poster gets
# cr_api so it can investigate (e.g. resolve streak opponents) before posting.
_NO_EXTERNAL_LOOKUP_WORKFLOWS = {"observe", "reception", "roster_bios"}

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

READ_TOOLS_NO_EXTERNAL = [t for t in READ_TOOLS if t["name"] not in EXTERNAL_LOOKUP_TOOL_NAMES]

# The scheduled Clan Wars Intel Report workflow uses a narrow read-only toolset:
# the CR API bridge (to confirm current opponents and fetch profiles) and the
# intel-scoring tool that wraps the threat analysis. The scheduled job handles
# Discord posting and memory persistence outside the LLM loop.
_INTEL_REPORT_TOOL_NAMES = {"cr_api", "get_clan_intel_report"}
INTEL_REPORT_TOOLS = [t for t in READ_TOOLS if t["name"] in _INTEL_REPORT_TOOL_NAMES]

# get_clan_health has sensitive aspects (at_risk, promotion_candidates) but
# aspect-level gating is handled in tool_exec.py, so we keep it available
# to all workflows. This avoids confusing the LLM by hiding the tool entirely.
INTERACTIVE_READ_TOOLS = READ_TOOLS

TOOLSETS_BY_WORKFLOW = {
    "observe": READ_TOOLS_NO_EXTERNAL,
    "channel_update": READ_TOOLS,
    "channel_update_leadership": READ_TOOLS,
    "interactive": INTERACTIVE_READ_TOOLS,
    "clanops": ALL_TOOLS,
    "reception": [],
    "roster_bios": READ_TOOLS_NO_EXTERNAL,
    "deck_review": INTERACTIVE_READ_TOOLS,
    "intel_report": INTEL_REPORT_TOOLS,
    # Awareness loop: one agent turn per heartbeat that sees the full
    # situation and emits a post plan. Gets the full read-tool set so it can
    # investigate before posting, plus a narrow write surface (save_clan_memory,
    # flag_member_watch, record_leadership_followup) capped at
    # AWARENESS_WRITE_BUDGET_PER_TICK calls per tick.
    "awareness": READ_TOOLS + [
        d["tool"] for d in TOOL_DEFINITIONS
        if d["name"] in AWARENESS_WRITE_TOOL_NAMES
    ],
    # Weekly memory synthesis: the LLM produces a structured plan (arc
    # memories, stale IDs, contradictions, digest); the job function handles
    # persistence. Tools are intentionally zero — the full week's context is
    # assembled upfront by the job, so the agent reasons from the prompt
    # payload rather than by chaining tool calls.
    "memory_synthesis": [],
}

MAX_ROUNDS_BY_WORKFLOW = {
    "clanops": 5,
    "channel_update_leadership": 6,
    "interactive": 3,
    "observation": 3,
    "observe": 3,
    # channel_update gets real reach as of v4.5 — investigate streak opponents,
    # scout rivals, check standings — before posting. 6 rounds buys ~5 tool
    # calls plus the final answer turn.
    "channel_update": 6,
    "reception": 0,
    "roster_bios": 3,
    # deck_review chains are unusually long: war reconstruction → losses
    # lookup → multiple lookup_cards calls → ownership validation → final
    # answer. Suggest mode adds a validator-driven revision turn. 10 rounds
    # leaves headroom without inviting runaway loops.
    "deck_review": 10,
    # intel_report fans out across 4 opponents (~2 tool calls each) plus the
    # initial clan_war confirmation; 15 leaves headroom for retries.
    "intel_report": 15,
    # awareness loop: one situation in, possibly N posts out. Budget for a
    # couple of investigative tool calls (cr_api lookups for streak opponents,
    # rival scouting) plus the final post-plan answer turn.
    "awareness": 8,
    # memory synthesis: no tool calls expected (toolset is empty). Keep a
    # tiny round budget so a stray repair loop still has headroom.
    "memory_synthesis": 2,
}

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    "observation": {"required": ["event_type", "summary", "content"]},
    "channel_update": {"required": ["event_type", "summary", "content"]},
    "channel_update_leadership": {"required": ["event_type", "summary", "content"]},
    "interactive": {"required": ["event_type", "summary", "content"]},
    "clanops": {"required": ["event_type", "summary", "content"]},
    "reception": {"required": ["event_type", "content"]},
    "roster_bios": {"required": ["intro", "members"]},
    "deck_review": {"required": ["event_type", "summary", "content"]},
    "intel_report": {"required": ["event_type", "summary", "content"]},
    # awareness emits a post plan: zero or more posts, each routed to a channel.
    "awareness": {"required": ["posts"]},
    # memory_synthesis: arcs + stale list + contradictions + digest. Any field
    # may be empty; the job checks each independently before acting.
    "memory_synthesis": {"required": ["arc_memories", "stale_memory_ids", "contradictions", "digest"]},
}
