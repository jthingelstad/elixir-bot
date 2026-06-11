"""elixir_agent — stable public entrypoint for Elixir's LLM layer.

This module is a deliberate, explicit facade over the agent/ package: runtime
code and tests address the agent layer through these names, so the import list
below is the public API. Two of the re-exports are load-bearing patch seams:

- ``db``: agent.tool_exec late-binds its data access through
  ``elixir_agent.db`` so a test that patches it intercepts every tool call.
- ``cr_api``: the same module object the tools call, so patching
  ``elixir_agent.cr_api.get_player`` reaches them.

Submodules must never import this facade at module level (it imports them);
late, function-level ``import elixir_agent`` is the supported pattern.
"""

import cr_api
import db

from agent import memory_tasks
from agent.core import (
    BUILD_HASH,
    RELEASE_LABEL,
    _create_chat_completion,
    _model_for_workflow,
    log,
    response_text,
    response_tool_uses,
    runtime_status,
)
from agent.tool_policy import (
    MAX_ROUNDS_BY_WORKFLOW,
    RESPONSE_SCHEMAS_BY_WORKFLOW,
    TOOLSETS_BY_WORKFLOW,
)
from agent.chat import (
    _build_tool_result_envelope,
    _chat_with_tools,
)
from agent.tool_exec import _execute_tool
from agent.workflows import (
    analyze_arena_relay_screenshot,
    explain_quiz_answer,
    generate_channel_update,
    generate_home_message,
    generate_intel_report,
    generate_members_message,
    generate_message,
    generate_promote_content,
    generate_roster_bios,
    generate_season_awards_post,
    generate_tournament_recap,
    generate_tournament_update,
    generate_war_recap_update,
    generate_weekly_digest,
    observe_and_post,
    respond_in_channel,
    respond_in_deck_review,
    respond_in_reception,
    respond_to_help_request,
    run_awareness_tick,
    run_memory_synthesis,
    synthesize_leader_action_feedback,
)

__all__ = [
    "BUILD_HASH",
    "MAX_ROUNDS_BY_WORKFLOW",
    "RELEASE_LABEL",
    "RESPONSE_SCHEMAS_BY_WORKFLOW",
    "TOOLSETS_BY_WORKFLOW",
    "_build_tool_result_envelope",
    "_chat_with_tools",
    "_create_chat_completion",
    "_execute_tool",
    "_model_for_workflow",
    "analyze_arena_relay_screenshot",
    "cr_api",
    "db",
    "explain_quiz_answer",
    "generate_channel_update",
    "generate_home_message",
    "generate_intel_report",
    "generate_members_message",
    "generate_message",
    "generate_promote_content",
    "generate_roster_bios",
    "generate_season_awards_post",
    "generate_tournament_recap",
    "generate_tournament_update",
    "generate_war_recap_update",
    "generate_weekly_digest",
    "log",
    "memory_tasks",
    "observe_and_post",
    "respond_in_channel",
    "respond_in_deck_review",
    "respond_in_reception",
    "respond_to_help_request",
    "response_text",
    "response_tool_uses",
    "run_awareness_tick",
    "run_memory_synthesis",
    "runtime_status",
    "synthesize_leader_action_feedback",
]
