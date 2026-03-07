"""Shared core state for the agent package."""

import logging
import os
import subprocess
import time

from openai import OpenAI

from runtime import status as runtime_status

log = logging.getLogger("elixir_agent")


def _get_build_hash():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


BUILD_HASH = _get_build_hash()

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


__all__ = [name for name in globals() if not name.startswith("__")]
