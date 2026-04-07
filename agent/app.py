"""agent.app — LLM-powered observation and response engine for Elixir."""

import cr_api
import db
import prompts
from agent.core import (
    BUILD_HASH,
    CLANOPS_WRITE_TOOLS_ENABLED,
    MAX_CONTEXT_MEMBERS_DEFAULT,
    MAX_CONTEXT_MEMBERS_FULL,
    MAX_TOOL_ROUNDS,
    RELEASE_CODENAME,
    RELEASE_LABEL,
    RELEASE_VERSION,
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_MAX_ITEMS,
    _build_system_prompt,
    _create_chat_completion,
    _get_client,
    _model_for_workflow,
    log,
    runtime_status,
)


def __export_public(module):
    names = getattr(module, "__all__", None) or [
        name for name in vars(module) if not name.startswith("__")
    ]
    for name in names:
        globals()[name] = getattr(module, name)
    return names


from agent import prompts as _prompts_module
from agent import tool_defs as _tool_defs_module
from agent import tool_policy as _tool_policy_module
from agent import tool_exec as _tool_exec_module
from agent import chat as _chat_module
from agent import workflows as _workflows_module

__all__ = [name for name in globals() if not name.startswith("__")]
for _module in (
    _prompts_module,
    _tool_defs_module,
    _tool_policy_module,
    _tool_exec_module,
    _chat_module,
    _workflows_module,
):
    __export_public(_module)

__all__ = [name for name in globals() if not name.startswith("__")]
