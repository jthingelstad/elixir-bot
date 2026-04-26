"""Canonical workflow metadata for Elixir agent turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from agent.tool_defs import TOOLS

ModelFamily = Literal["chat", "promotion", "lightweight"]


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    response_schema: dict | None = None
    tools: list[dict] = field(default_factory=list)
    max_tool_rounds: int = 3
    model_family: ModelFamily = "lightweight"
    aliases: tuple[str, ...] = ()
    tools_allowed: bool = True
    write_tools_allowed: bool = False


_WRITE_TOOL_NAMES = {
    "update_member",
    "save_clan_memory",
    "flag_member_watch",
    "record_leadership_followup",
    "schedule_revisit",
}

AWARENESS_WRITE_TOOL_NAMES = {
    "save_clan_memory",
    "flag_member_watch",
    "record_leadership_followup",
    "schedule_revisit",
}

AWARENESS_WRITE_BUDGET_PER_TICK = 3
EXTERNAL_LOOKUP_TOOL_NAMES = {"cr_api"}
_NO_EXTERNAL_LOOKUP_WORKFLOWS = {"observe", "observation", "reception", "roster_bios"}

TOOL_DEFINITIONS = []
for _tool in TOOLS:
    _name = _tool["name"]
    _side_effect = "write" if _name in _WRITE_TOOL_NAMES else "read"
    TOOL_DEFINITIONS.append({
        "tool": _tool,
        "name": _name,
        "side_effect": _side_effect,
    })

TOOL_DEFINITIONS_BY_NAME = {d["name"]: d for d in TOOL_DEFINITIONS}

READ_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "read"]
WRITE_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "write"]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
READ_TOOLS_NO_EXTERNAL = [t for t in READ_TOOLS if t["name"] not in EXTERNAL_LOOKUP_TOOL_NAMES]

_INTEL_REPORT_TOOL_NAMES = {"cr_api", "get_clan_intel_report"}
INTEL_REPORT_TOOLS = [t for t in READ_TOOLS if t["name"] in _INTEL_REPORT_TOOL_NAMES]

_TOURNAMENT_RECAP_TOOL_NAMES = {"cr_api"}
TOURNAMENT_RECAP_TOOLS = [t for t in READ_TOOLS if t["name"] in _TOURNAMENT_RECAP_TOOL_NAMES]

_TOURNAMENT_UPDATE_TOOL_NAMES = {"cr_api"}
TOURNAMENT_UPDATE_TOOLS = [t for t in READ_TOOLS if t["name"] in _TOURNAMENT_UPDATE_TOOL_NAMES]

INTERACTIVE_READ_TOOLS = READ_TOOLS
AWARENESS_TOOLS = READ_TOOLS + [
    d["tool"] for d in TOOL_DEFINITIONS
    if d["name"] in AWARENESS_WRITE_TOOL_NAMES
]

_CHANNEL_SCHEMA = {"required": ["event_type", "summary", "content"]}

_WORKFLOW_SPECS = (
    WorkflowSpec(
        "observation",
        aliases=("observe",),
        response_schema=_CHANNEL_SCHEMA,
        tools=READ_TOOLS_NO_EXTERNAL,
        max_tool_rounds=3,
    ),
    WorkflowSpec("channel_update", response_schema=_CHANNEL_SCHEMA, tools=READ_TOOLS, max_tool_rounds=6),
    WorkflowSpec("channel_update_leadership", response_schema=_CHANNEL_SCHEMA, tools=READ_TOOLS, max_tool_rounds=6),
    WorkflowSpec("interactive", response_schema=_CHANNEL_SCHEMA, tools=INTERACTIVE_READ_TOOLS, max_tool_rounds=4),
    WorkflowSpec(
        "clanops",
        response_schema=_CHANNEL_SCHEMA,
        tools=ALL_TOOLS,
        max_tool_rounds=5,
        write_tools_allowed=True,
    ),
    WorkflowSpec(
        "reception",
        response_schema={"required": ["event_type", "content"]},
        tools=[],
        max_tool_rounds=0,
        tools_allowed=False,
    ),
    WorkflowSpec(
        "roster_bios",
        response_schema={"required": ["intro", "members"]},
        tools=READ_TOOLS_NO_EXTERNAL,
        max_tool_rounds=3,
    ),
    WorkflowSpec("deck_review", response_schema=_CHANNEL_SCHEMA, tools=INTERACTIVE_READ_TOOLS, max_tool_rounds=10),
    WorkflowSpec("intel_report", response_schema=_CHANNEL_SCHEMA, tools=INTEL_REPORT_TOOLS, max_tool_rounds=15, model_family="chat"),
    WorkflowSpec("tournament_recap", response_schema={"required": ["content"]}, tools=TOURNAMENT_RECAP_TOOLS, max_tool_rounds=8, model_family="chat"),
    WorkflowSpec("tournament_update", response_schema=_CHANNEL_SCHEMA, tools=TOURNAMENT_UPDATE_TOOLS, max_tool_rounds=4),
    WorkflowSpec("war_recap", response_schema=_CHANNEL_SCHEMA, tools=[], max_tool_rounds=1, tools_allowed=False),
    WorkflowSpec("season_awards", response_schema=_CHANNEL_SCHEMA, tools=[], max_tool_rounds=1, tools_allowed=False),
    WorkflowSpec(
        "awareness",
        response_schema={"required": ["posts"]},
        tools=AWARENESS_TOOLS,
        max_tool_rounds=8,
        write_tools_allowed=True,
    ),
    WorkflowSpec(
        "memory_synthesis",
        response_schema={"required": ["arc_memories", "stale_memory_ids", "contradictions", "digest"]},
        tools=[],
        max_tool_rounds=2,
        model_family="chat",
        tools_allowed=False,
    ),
    WorkflowSpec("weekly_digest", model_family="chat"),
    WorkflowSpec("site_promote_content", model_family="promotion"),
)

WORKFLOW_SPECS = {spec.name: spec for spec in _WORKFLOW_SPECS}
SONNET_RETAINED_WORKFLOWS = frozenset(
    spec.name for spec in _WORKFLOW_SPECS if spec.model_family == "chat"
)
_ALIASES = {
    alias: spec.name
    for spec in _WORKFLOW_SPECS
    for alias in spec.aliases
}


def canonical_workflow_name(name: str | None) -> str:
    workflow = name or ""
    return _ALIASES.get(workflow, workflow)


def get_workflow_spec(name: str | None) -> WorkflowSpec:
    workflow = canonical_workflow_name(name)
    try:
        return WORKFLOW_SPECS[workflow]
    except KeyError as exc:
        raise KeyError(f"unknown workflow: {name!r}") from exc


def workflow_model_family(name: str | None) -> ModelFamily:
    try:
        return get_workflow_spec(name).model_family
    except KeyError:
        return "lightweight"


TOOLSETS_BY_WORKFLOW = {
    spec.name: spec.tools
    for spec in _WORKFLOW_SPECS
    if spec.response_schema is not None
}
for _alias, _canonical in _ALIASES.items():
    if _canonical in TOOLSETS_BY_WORKFLOW:
        TOOLSETS_BY_WORKFLOW[_alias] = TOOLSETS_BY_WORKFLOW[_canonical]

MAX_ROUNDS_BY_WORKFLOW = {
    spec.name: spec.max_tool_rounds
    for spec in _WORKFLOW_SPECS
    if spec.response_schema is not None
}
for _alias, _canonical in _ALIASES.items():
    if _canonical in MAX_ROUNDS_BY_WORKFLOW:
        MAX_ROUNDS_BY_WORKFLOW[_alias] = MAX_ROUNDS_BY_WORKFLOW[_canonical]

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    spec.name: spec.response_schema
    for spec in _WORKFLOW_SPECS
    if spec.response_schema is not None
}
for _alias, _canonical in _ALIASES.items():
    if _canonical in RESPONSE_SCHEMAS_BY_WORKFLOW:
        RESPONSE_SCHEMAS_BY_WORKFLOW[_alias] = RESPONSE_SCHEMAS_BY_WORKFLOW[_canonical]
