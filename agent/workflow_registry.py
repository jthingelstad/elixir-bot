"""Canonical workflow metadata for Elixir agent turns."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from agent.tool_defs import SURFACE_TOOLS, TOOLS


def _awareness_max_rounds() -> int:
    """Tool-round cap for the awareness brain. Cost scales ~linearly with rounds
    (each round re-reads + re-caches the growing context), so this is the main
    per-tick cost dial. 6 gives ample headroom for a grounded post (a join
    welcome needs ~3) while trimming runaway 8-round exploration. Env-tunable so
    cost/quality can be dialed without a deploy."""
    try:
        return max(1, int(os.getenv("ELIXIR_AWARENESS_MAX_ROUNDS", "6")))
    except ValueError:
        return 6


ModelFamily = Literal["chat", "creative", "lightweight", "intensive"]


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
    "save_clan_memory",
    "record_leadership_followup",
    "schedule_followup",
}

# Posting is a write. These reach members, so they are the most consequential
# writes Elixir has.
_SURFACE_TOOL_NAMES = frozenset({"post_to_discord", "post_to_clan_chat"})

AWARENESS_WRITE_TOOL_NAMES = {
    "save_clan_memory",
    "record_leadership_followup",
    # Phase 5. In BOTH write sets deliberately: a shipped tool was once offered
    # to a model zero times because it landed in _WRITE_TOOL_NAMES alone, and an
    # intention Elixir can only form in a leadership conversation is useless —
    # the member says "my phone broke" in #ask-elixir, not in #leaders.
    "schedule_followup",
}

AWARENESS_WRITE_BUDGET_PER_TICK = 3

# Per-workflow write budgets, and WHICH tools they apply to. A budget exists to
# stop a loop from spending the clan's durable memory on one turn — it is not a
# general brake on every write. The chassis posting tools are deliberately
# absent from the budgeted set: a welcome legitimately calls two of them
# (Discord + clan chat) and a validator bounce can legitimately cost a third, so
# a shared 3-call cap would fail the very turn it was meant to protect.
WRITE_BUDGET_BY_WORKFLOW = {
    "awareness": AWARENESS_WRITE_BUDGET_PER_TICK,
    "wake_response": AWARENESS_WRITE_BUDGET_PER_TICK,
    "wake_response_chat": AWARENESS_WRITE_BUDGET_PER_TICK,
}
BUDGETED_WRITE_TOOLS_BY_WORKFLOW = {
    "awareness": frozenset(AWARENESS_WRITE_TOOL_NAMES),
    "wake_response": frozenset(AWARENESS_WRITE_TOOL_NAMES),
    "wake_response_chat": frozenset(AWARENESS_WRITE_TOOL_NAMES),
}
EXTERNAL_LOOKUP_TOOL_NAMES = {"cr_api"}
_NO_EXTERNAL_LOOKUP_WORKFLOWS = {"reception", "roster_bios"}

TOOL_DEFINITIONS = []
# Surface tools are included so the write gate SEES them. They are not in TOOLS
# (no shared workflow may call one), but a definition the gate cannot find
# defaults to side_effect="read" — which would let any workflow that somehow got
# the tool post to the clan unchecked. Declaring them here means posting is
# permitted only for a spec that opted into writes.
for _tool in list(TOOLS) + list(SURFACE_TOOLS):
    _name = _tool["name"]
    _side_effect = "write" if _name in _WRITE_TOOL_NAMES or _name in _SURFACE_TOOL_NAMES else "read"
    TOOL_DEFINITIONS.append(
        {
            "tool": _tool,
            "name": _name,
            "side_effect": _side_effect,
        }
    )

TOOL_DEFINITIONS_BY_NAME = {d["name"]: d for d in TOOL_DEFINITIONS}

READ_TOOLS = [d["tool"] for d in TOOL_DEFINITIONS if d["side_effect"] == "read"]
# Surface tools are declared in TOOL_DEFINITIONS so the write gate can see them,
# but they must never reach a shared toolset: WRITE_TOOLS feeds ALL_TOOLS, which
# is clanops's surface, and a posting tool offered there would let a leadership
# command post to the whole clan. They are handed out per-turn by
# agent.chassis.surface_tools and nowhere else.
WRITE_TOOLS = [
    d["tool"]
    for d in TOOL_DEFINITIONS
    if d["side_effect"] == "write" and d["name"] not in _SURFACE_TOOL_NAMES
]
ALL_TOOLS = READ_TOOLS + WRITE_TOOLS
READ_TOOLS_NO_EXTERNAL = [t for t in READ_TOOLS if t["name"] not in EXTERNAL_LOOKUP_TOOL_NAMES]

_INTEL_REPORT_TOOL_NAMES = {"cr_api"}
INTEL_REPORT_TOOLS = [t for t in READ_TOOLS if t["name"] in _INTEL_REPORT_TOOL_NAMES]

_TOURNAMENT_RECAP_TOOL_NAMES = {"cr_api"}
TOURNAMENT_RECAP_TOOLS = [t for t in READ_TOOLS if t["name"] in _TOURNAMENT_RECAP_TOOL_NAMES]

_TOURNAMENT_UPDATE_TOOL_NAMES = {"cr_api"}
TOURNAMENT_UPDATE_TOOLS = [t for t in READ_TOOLS if t["name"] in _TOURNAMENT_UPDATE_TOOL_NAMES]

INTERACTIVE_READ_TOOLS = READ_TOOLS
AWARENESS_TOOLS = READ_TOOLS + [
    d["tool"] for d in TOOL_DEFINITIONS if d["name"] in AWARENESS_WRITE_TOOL_NAMES
]

_CHANNEL_SCHEMA = {"required": ["event_type", "summary", "content"]}

_WORKFLOW_SPECS = (
    WorkflowSpec(
        "channel_update",
        response_schema=_CHANNEL_SCHEMA,
        tools=READ_TOOLS,
        max_tool_rounds=6,
        model_family="chat",
    ),
    WorkflowSpec(
        "channel_update_leadership",
        response_schema=_CHANNEL_SCHEMA,
        tools=READ_TOOLS,
        max_tool_rounds=6,
        model_family="chat",
    ),
    WorkflowSpec(
        "interactive",
        response_schema=_CHANNEL_SCHEMA,
        tools=INTERACTIVE_READ_TOOLS,
        max_tool_rounds=4,
        model_family="chat",
    ),
    WorkflowSpec(
        "clanops",
        response_schema=_CHANNEL_SCHEMA,
        tools=ALL_TOOLS,
        max_tool_rounds=5,
        write_tools_allowed=True,
        model_family="chat",
    ),
    WorkflowSpec(
        "reception",
        response_schema={"required": ["event_type", "content"]},
        tools=[],
        max_tool_rounds=0,
        tools_allowed=False,
        model_family="chat",
    ),
    WorkflowSpec(
        "roster_bios",
        response_schema={"required": ["intro", "members"]},
        tools=READ_TOOLS_NO_EXTERNAL,
        max_tool_rounds=3,
    ),
    WorkflowSpec(
        "deck_review",
        response_schema=_CHANNEL_SCHEMA,
        tools=INTERACTIVE_READ_TOOLS,
        # 10 -> 6 (2026-08-05). Measured over 18 real reviews: the median takes
        # 5 calls and the 75th percentile 5, but the tail ran to 11 and 16 — and
        # a round is not cheap here, because each one writes ~14K tokens of
        # accumulated tool results into the prompt cache at 1.25x input. Cache
        # writes are 62% of this workflow's cost, so the round count IS the bill.
        #
        # 6 bounds the tail without touching the median. It is deliberately not
        # 4 (interactive's cap): a deck review legitimately fetches deck
        # intelligence, battle intelligence, the card collection and a
        # recommendation, and squeezing that would trade cost for a worse answer
        # rather than for less waste.
        max_tool_rounds=6,
        model_family="chat",
    ),
    WorkflowSpec(
        "screenshot_readout",
        response_schema=_CHANNEL_SCHEMA,
        tools=READ_TOOLS,
        max_tool_rounds=4,
    ),
    WorkflowSpec(
        "intel_report",
        response_schema=_CHANNEL_SCHEMA,
        tools=INTEL_REPORT_TOOLS,
        max_tool_rounds=15,
        model_family="intensive",
    ),
    WorkflowSpec(
        "tournament_recap",
        response_schema={"required": ["content"]},
        tools=TOURNAMENT_RECAP_TOOLS,
        max_tool_rounds=8,
        model_family="intensive",
    ),
    WorkflowSpec(
        "tournament_update",
        response_schema=_CHANNEL_SCHEMA,
        tools=TOURNAMENT_UPDATE_TOOLS,
        max_tool_rounds=4,
    ),
    WorkflowSpec(
        "awareness",
        response_schema={"required": ["posts"]},
        tools=AWARENESS_TOOLS,
        max_tool_rounds=_awareness_max_rounds(),
        write_tools_allowed=True,
        model_family="chat",
    ),
    # Agentic Loop v2 chassis turns. Two specs rather than a model argument
    # because model choice is registry data everywhere else in this file, and
    # the escalation ladder is then just "run the same attention under the other
    # workflow name". No response_schema: a chassis turn's output IS its tool
    # calls, so there is no JSON envelope to validate — the caller passes
    # allowed_tools explicitly (see the note on the derived-dict filter).
    WorkflowSpec(
        "wake_response",
        tools=[],
        max_tool_rounds=6,
        write_tools_allowed=True,
        model_family="lightweight",
    ),
    WorkflowSpec(
        "wake_response_chat",
        tools=[],
        max_tool_rounds=6,
        write_tools_allowed=True,
        model_family="chat",
    ),
    WorkflowSpec(
        "awareness_repair",
        response_schema={"required": ["posts"]},
        tools=[],
        max_tool_rounds=0,
        tools_allowed=False,
        model_family="chat",
    ),
    WorkflowSpec(
        "game_factual_repair",
        response_schema=_CHANNEL_SCHEMA,
        tools=[],
        max_tool_rounds=0,
        tools_allowed=False,
        model_family="chat",
    ),
    WorkflowSpec(
        "ask_elixir_daily",
        response_schema={"required": ["post"]},
        tools=INTERACTIVE_READ_TOOLS,
        max_tool_rounds=6,
        model_family="chat",
    ),
    WorkflowSpec(
        "memory_synthesis",
        response_schema={
            "required": ["arc_memories", "stale_memory_ids", "contradictions", "digest"]
        },
        tools=[],
        max_tool_rounds=2,
        model_family="intensive",
        tools_allowed=False,
    ),
    # Phase 4. Toolless on purpose: everything it may reason about is handed to
    # it as evidence, so it cannot go and find a fact to justify a lesson it
    # already wanted to write. `chat` rather than `intensive` because it reads
    # one day, not a quarter — the weekly Opus synthesis is still the deep pass.
    WorkflowSpec(
        "reflection",
        response_schema={"required": ["lessons", "notes"]},  # `dossiers` optional
        tools=[],
        max_tool_rounds=2,
        model_family="chat",
        tools_allowed=False,
    ),
    WorkflowSpec(
        "leader_action_feedback",
        response_schema={
            "required": [
                "action_type",
                "sample_count",
                "summary",
                "guidance",
                "evidence",
            ]
        },
        tools=[],
        max_tool_rounds=1,
        # Opus -> chat/Sonnet 5 (2026-07-23) -> lightweight/Haiku 4.5 (2026-07-31),
        # each step justified by replaying REAL captured prompts, not by intuition
        # (scripts/replay_model_swap.py). This is a single-shot structured synthesis
        # of a leader-action sample into internal guidance the brain reads — never a
        # public post, so the prose bar that protects awareness does not apply.
        # Haiku replay over the 8 most recent live prompts: 8/8 parsed, 8/8
        # schema-complete, output slightly LONGER than Sonnet's and equally specific
        # (both grounded the same approval rate from the same sample). At 17% of
        # spend and ~5.5 calls/day this was the second-largest line; Haiku is 1/3 the
        # price across every token class.
        model_family="lightweight",
        tools_allowed=False,
    ),
    WorkflowSpec(
        "clan_chat_copy",
        response_schema={"required": ["messages"]},
        tools=[],
        max_tool_rounds=1,
        model_family="chat",
        tools_allowed=False,
    ),
    WorkflowSpec(
        "weekly_recap",
        response_schema={"required": ["recap"]},
        tools=INTERACTIVE_READ_TOOLS,
        max_tool_rounds=6,
        model_family="intensive",
    ),
    # The emailed Weekly Clan Report — a separate composition from `weekly_recap`,
    # not a reformat of it. Discord gets the short punchy post; email gets the
    # expansive edition with headings and tables.
    WorkflowSpec(
        "weekly_recap_email",
        response_schema={"required": ["email"]},
        tools=INTERACTIVE_READ_TOOLS,
        max_tool_rounds=6,
        model_family="intensive",
    ),
    WorkflowSpec("member_report", model_family="intensive"),
    # Weekly public Elder Standing report — standalone, no tools, composed from a
    # pre-materialized facts brief (runtime.elder_standing), grounding-guarded.
    WorkflowSpec("elder_standing", tools=[], tools_allowed=False, model_family="intensive"),
    # DM outreach ask (runtime.outreach via app): a short warm profile-outreach DM
    # composed in Elixir's voice from a member facts brief. No tools; a leader
    # approves the draft before it sends.
    WorkflowSpec("member_outreach_ask", tools=[], tools_allowed=False, model_family="chat"),
    # Awareness cost gate (runtime.awareness.gate): a lightweight (Haiku) binary
    # post-vs-silence triage that runs before the expensive Sonnet brain on
    # soft-signal ticks. No tools, tiny prompt — it only gates, never posts.
    WorkflowSpec(
        "awareness_triage",
        tools=[],
        tools_allowed=False,
        max_tool_rounds=1,
        model_family="lightweight",
    ),
    # Leader-note interpreter (runtime.leader_note_interpreter): a lightweight
    # (Haiku) classifier that reads a leader's free-text on an #actions card and
    # maps it to exactly one structured effect (timing_hold / invalidate_premise
    # / persist_context / none). No tools, one round, strict JSON — it never
    # posts; the effect is applied deterministically off the delivery path.
    WorkflowSpec(
        "leader_note_interpret",
        response_schema={"required": ["effect", "reading"]},
        tools=[],
        tools_allowed=False,
        max_tool_rounds=1,
        model_family="lightweight",
    ),
    WorkflowSpec("recruiting_copy", model_family="creative"),
    # Release-notes announcement (agent/release_notes.py, ported from Oliver):
    # Elixir's first-person "what I can do now" post — chat-tier, no tools.
    WorkflowSpec(
        "release_notes",
        tools=[],
        max_tool_rounds=1,
        tools_allowed=False,
        model_family="intensive",
    ),
)

WORKFLOW_SPECS = {spec.name: spec for spec in _WORKFLOW_SPECS}
SONNET_RETAINED_WORKFLOWS = frozenset(
    spec.name for spec in _WORKFLOW_SPECS if spec.model_family == "chat"
)
_ALIASES = {alias: spec.name for spec in _WORKFLOW_SPECS for alias in spec.aliases}


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
    spec.name: spec.tools for spec in _WORKFLOW_SPECS if spec.response_schema is not None
}
for _alias, _canonical in _ALIASES.items():
    if _canonical in TOOLSETS_BY_WORKFLOW:
        TOOLSETS_BY_WORKFLOW[_alias] = TOOLSETS_BY_WORKFLOW[_canonical]

# Every spec, schema or not. The round cap has nothing to do with whether a
# workflow returns JSON, and filtering on response_schema silently discarded the
# declared value: awareness_triage declares 1 and got 3, release_notes the same,
# and a chassis turn declaring 6 got 3 — which cost it the retry round the
# delivery validator's bounce-and-fix loop depends on. Both older cases have
# tools_allowed=False so their cap was inert; making the declaration
# authoritative is what a registry is for.
MAX_ROUNDS_BY_WORKFLOW = {spec.name: spec.max_tool_rounds for spec in _WORKFLOW_SPECS}
for _alias, _canonical in _ALIASES.items():
    if _canonical in MAX_ROUNDS_BY_WORKFLOW:
        MAX_ROUNDS_BY_WORKFLOW[_alias] = MAX_ROUNDS_BY_WORKFLOW[_canonical]

RESPONSE_SCHEMAS_BY_WORKFLOW = {
    spec.name: spec.response_schema for spec in _WORKFLOW_SPECS if spec.response_schema is not None
}
for _alias, _canonical in _ALIASES.items():
    if _canonical in RESPONSE_SCHEMAS_BY_WORKFLOW:
        RESPONSE_SCHEMAS_BY_WORKFLOW[_alias] = RESPONSE_SCHEMAS_BY_WORKFLOW[_canonical]
