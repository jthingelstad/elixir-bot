"""Shared core state for the agent package."""

import logging
import os
import sqlite3
import subprocess
import threading
import time

from anthropic import Anthropic, APIError, APIConnectionError

import db
from agent.workflow_registry import SONNET_RETAINED_WORKFLOWS, workflow_model_family
from runtime import status as runtime_status

log = logging.getLogger("elixir_agent")

# Workflows whose call cadence (1+ hours apart) exceeds the 5-min ephemeral
# cache TTL, so caching the system+tools prefix pays the 1.25x write premium
# but rarely yields a read. Confirmed in May 2026 sampling: awareness had
# 89% write-only calls, 10% read-only, 1% mixed — net cost penalty.
WORKFLOWS_WITHOUT_CACHE = {"awareness"}

# Per-workflow request timeouts (seconds) override the 60s default. Sonnet 4.6
# on the weekly memory_synthesis batch (~75K input tokens) routinely completes
# in 55-120s, which trips the default timeout and triggers SDK retries that
# the model can't outrun — three Sunday runs failed in a row (2026-05-03/10/17)
# before this override was added.
WORKFLOW_TIMEOUT_OVERRIDES = {
    "memory_synthesis": 300,
}


def _get_build_hash():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"


BUILD_HASH = _get_build_hash()
RELEASE_VERSION = os.getenv("ELIXIR_RELEASE_VERSION", "v4.8")
RELEASE_CODENAME = os.getenv("ELIXIR_RELEASE_CODENAME", "Trophy Hall")
RELEASE_LABEL = f'{RELEASE_VERSION} "{RELEASE_CODENAME}"'

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = Anthropic(api_key=os.getenv("CLAUDE_API_KEY"), timeout=60)
    return _client


def _chat_model_name():
    return os.getenv("ELIXIR_CHAT_MODEL", "claude-sonnet-4-6")


def _promotion_model_name():
    return os.getenv("ELIXIR_PROMOTION_MODEL", "claude-sonnet-4-6")


def _lightweight_model_name():
    return os.getenv("ELIXIR_LIGHTWEIGHT_MODEL", "claude-haiku-4-5-20251001")


def _model_for_workflow(workflow, model=None):
    if model:
        return model
    model_family = workflow_model_family(workflow)
    if model_family == "promotion":
        return _promotion_model_name()
    if model_family == "chat":
        return _chat_model_name()
    return _lightweight_model_name()


MAX_TOOL_ROUNDS = 3
CLANOPS_WRITE_TOOLS_ENABLED = os.getenv("CLANOPS_WRITE_TOOLS_ENABLED", "1") != "0"
MAX_CONTEXT_MEMBERS_DEFAULT = 30
MAX_CONTEXT_MEMBERS_FULL = 50
TOOL_RESULT_MAX_ITEMS = 50
TOOL_RESULT_MAX_CHARS = 20000


def _build_system_prompt(*sections):
    parts = [s for s in sections if s]
    parts.append(f"Your release version: {RELEASE_LABEL}")
    parts.append(f"Your build version: {BUILD_HASH}")
    return "\n\n".join(parts)


# ── Native response helpers ──────────────────────────────────────────────────
# The agent layer speaks native Anthropic shapes: messages are user/assistant
# dicts whose content is a string or a list of content blocks, and responses
# are the SDK's Message objects. These helpers cover the two access patterns
# every consumer needs.


def response_text(resp) -> str | None:
    """Concatenated text blocks from a native Anthropic Message, or None."""
    parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    return "".join(parts) if parts else None


def response_tool_uses(resp) -> list:
    """tool_use blocks (each has .id, .name, .input) from a native Message."""
    return [block for block in resp.content if getattr(block, "type", None) == "tool_use"]


def _content_has_anthropic_payload(content) -> bool:
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return bool(content)
    return True


def _sanitize_anthropic_messages(messages):
    """Drop empty turns after translation so Anthropic never sees blank content."""
    sanitized = []
    dropped = 0
    for msg in messages:
        if _content_has_anthropic_payload(msg.get("content")):
            sanitized.append(msg)
        else:
            dropped += 1
    if dropped:
        log.info("anthropic_empty_messages_dropped count=%s", dropped)
    if not sanitized:
        sanitized.append({"role": "user", "content": "No user message content was provided."})
        log.warning("anthropic_messages_empty_after_sanitize inserted_placeholder=true")
    return sanitized


_TOOL_CHOICE_MAP = {
    "auto": {"type": "auto"},
    "none": {"type": "none"},
    "required": {"type": "any"},
}


# ── Main completion function ─────────────────────────────────────────────────


def _create_chat_completion(*, workflow, messages, system=None, model=None, temperature=0.7, max_tokens=4096, timeout=60, tools=None, tool_choice=None):
    """Call the Anthropic Messages API and return the native Message response.

    messages: native Anthropic messages (user/assistant roles only; content is
    a string or a list of content blocks — SDK block objects from a prior
    response are fine). The system prompt goes in `system`, not a message.
    """
    started = time.perf_counter()
    selected_model = _model_for_workflow(workflow, model=model)

    sanitized_messages = _sanitize_anthropic_messages(messages)

    effective_timeout = WORKFLOW_TIMEOUT_OVERRIDES.get(workflow, timeout)

    kwargs = {
        "model": selected_model,
        "messages": sanitized_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": effective_timeout,
    }

    cache_enabled = workflow not in WORKFLOWS_WITHOUT_CACHE

    # System prompt with optional prompt caching
    if system:
        system_block = {"type": "text", "text": system}
        if cache_enabled:
            system_block["cache_control"] = {"type": "ephemeral"}
        kwargs["system"] = [system_block]

    # Tools with optional prompt caching on the last tool definition
    if tools:
        if cache_enabled:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = cached_tools
        else:
            kwargs["tools"] = list(tools)
    if tool_choice:
        translated_tc = _TOOL_CHOICE_MAP.get(tool_choice)
        if translated_tc:
            kwargs["tool_choice"] = translated_tc

    try:
        resp = _get_client().messages.create(**kwargs)
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "input_tokens", None)
        completion_tokens = getattr(usage, "output_tokens", None)
        total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", None)
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", None)
        duration = round((time.perf_counter() - started) * 1000, 2)
        runtime_status.record_llm_call(
            workflow,
            ok=True,
            model=selected_model,
            duration_ms=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        try:
            db.record_llm_call(
                workflow, selected_model,
                ok=True,
                duration_ms=duration,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        except (OSError, sqlite3.Error):
            log.warning("llm_call_persist_failed workflow=%s", workflow, exc_info=True)
        return resp
    except (APIError, APIConnectionError) as exc:
        duration = round((time.perf_counter() - started) * 1000, 2)
        runtime_status.record_llm_call(
            workflow,
            ok=False,
            model=selected_model,
            error=exc,
            duration_ms=duration,
        )
        try:
            db.record_llm_call(
                workflow, selected_model,
                ok=False,
                error=exc,
                duration_ms=duration,
            )
        except (OSError, sqlite3.Error):
            log.warning("llm_call_persist_failed workflow=%s", workflow, exc_info=True)
        # Central alert trigger: runs for every failing LLM call regardless of
        # which workflow / caller ran. Lazy import: runtime.alerts is cheap and
        # cycle-free, but importing it at module load would still drag the
        # runtime package into every agent-layer unit test.
        try:
            from runtime.alerts import schedule_llm_failure_alert
            schedule_llm_failure_alert(workflow)
        except Exception:
            log.warning("schedule_llm_failure_alert_import_failed", exc_info=True)
        raise




__all__ = [name for name in globals() if not name.startswith("__")]
