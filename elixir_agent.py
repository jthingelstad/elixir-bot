"""elixir_agent.py — LLM-powered observation and response engine for Elixir.

Uses OpenAI function calling to let the LLM query member history, war
results, and player details on demand.

Personality, clan knowledge, and channel behaviors are loaded from
prompt files in the prompts/ directory.
"""

import json
import logging
import os
import subprocess
import time

from openai import OpenAI

import cr_api
import db
import prompts
import runtime_status

log = logging.getLogger("elixir_agent")


def _get_build_hash():
    """Capture the git short hash at import time."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


BUILD_HASH = _get_build_hash()

# Lazy client — only initialized when actually needed (allows tests to import without API key)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=60)
    return _client

MAX_TOOL_ROUNDS = 3
CLANOPS_WRITE_TOOLS_ENABLED = os.getenv("CLANOPS_WRITE_TOOLS_ENABLED", "1") != "0"
MAX_CONTEXT_MEMBERS_DEFAULT = 30
MAX_CONTEXT_MEMBERS_FULL = 50
TOOL_RESULT_MAX_ITEMS = 50
TOOL_RESULT_MAX_CHARS = 12000


def _build_system_prompt(*sections):
    """Combine prompt sections into a single system prompt."""
    parts = [s for s in sections if s]
    parts.append(f"Your build version: {BUILD_HASH}")
    return "\n\n".join(parts)


def _create_chat_completion(*, workflow, messages, model="gpt-4o", temperature=0.7, max_tokens=800, timeout=60, tools=None, tool_choice=None):
    started = time.perf_counter()
    kwargs = {
        "model": model,
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
            model=model,
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
            model=model,
            error=exc,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        raise


def _load_extracted(relative_path):
    path = os.path.join(os.path.dirname(__file__), relative_path)
    with open(path, "r") as f:
        exec(compile(f.read(), path, "exec"), globals())


# -- Extracted prompt, tool, and workflow modules --------------------------

_load_extracted("agent/prompts.py")
_load_extracted("agent/tools.py")
_load_extracted("agent/chat.py")
_load_extracted("agent/workflows.py")
