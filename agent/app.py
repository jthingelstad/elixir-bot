"""agent.app — LLM-powered observation and response engine for Elixir."""

import json
import time

import cr_api
import db
import prompts
from agent.core import (
    BUILD_HASH,
    CLANOPS_WRITE_TOOLS_ENABLED,
    MAX_CONTEXT_MEMBERS_DEFAULT,
    MAX_CONTEXT_MEMBERS_FULL,
    MAX_TOOL_ROUNDS,
    TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_MAX_ITEMS,
    _build_system_prompt,
    _get_client,
    _model_for_workflow,
    log,
    runtime_status,
)


def _create_chat_completion(*, workflow, messages, model=None, temperature=0.7, max_tokens=800, timeout=60, tools=None, tool_choice=None):
    started = time.perf_counter()
    selected_model = _model_for_workflow(workflow, model=model)
    kwargs = {
        "model": selected_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    try:
        resp = _get_client().chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        runtime_status.record_openai_call(
            workflow,
            ok=True,
            model=selected_model,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )
        return resp
    except Exception as exc:
        runtime_status.record_openai_call(
            workflow,
            ok=False,
            model=selected_model,
            error=exc,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise


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
