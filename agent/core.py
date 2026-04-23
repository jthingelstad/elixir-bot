"""Shared core state for the agent package."""

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field

from anthropic import Anthropic, APIError, APIConnectionError

import db
from runtime import status as runtime_status

log = logging.getLogger("elixir_agent")


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


SONNET_RETAINED_WORKFLOWS = frozenset({
    "weekly_digest",
    "tournament_recap",
    "intel_report",
    "memory_synthesis",
})


def _model_for_workflow(workflow, model=None):
    if model:
        return model
    workflow = workflow or ""
    if workflow == "site_promote_content":
        return _promotion_model_name()
    if workflow in SONNET_RETAINED_WORKFLOWS:
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


# ── Response wrapper dataclasses ─────────────────────────────────────────────
# These let callers use the same attribute access patterns as the old OpenAI
# response objects: resp.choices[0].message.content, .tool_calls, .usage, etc.


@dataclass
class _Function:
    name: str
    arguments: str  # JSON string


@dataclass
class _ToolCall:
    id: str
    type: str  # always "function"
    function: _Function


@dataclass
class _Message:
    role: str
    content: str | None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _Choice:
    message: _Message
    stop_reason: str | None = None


@dataclass
class _Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass
class _CacheStats:
    creation_tokens: int | None = None
    read_tokens: int | None = None


@dataclass
class _LLMResponse:
    choices: list[_Choice]
    usage: _Usage
    cache_stats: _CacheStats = field(default_factory=_CacheStats)


def _wrap_anthropic_response(response):
    """Convert an Anthropic Message response into an _LLMResponse wrapper."""
    content_text = None
    tool_calls = []

    for block in response.content:
        if block.type == "text":
            content_text = block.text
        elif block.type == "tool_use":
            tool_calls.append(_ToolCall(
                id=block.id,
                type="function",
                function=_Function(
                    name=block.name,
                    arguments=json.dumps(block.input),
                ),
            ))

    usage = response.usage
    cache_stats = _CacheStats(
        creation_tokens=getattr(usage, "cache_creation_input_tokens", None),
        read_tokens=getattr(usage, "cache_read_input_tokens", None),
    )

    return _LLMResponse(
        choices=[_Choice(
            message=_Message(
                role="assistant",
                content=content_text,
                tool_calls=tool_calls if tool_calls else None,
            ),
            stop_reason=getattr(response, "stop_reason", None),
        )],
        usage=_Usage(
            prompt_tokens=getattr(usage, "input_tokens", None),
            completion_tokens=getattr(usage, "output_tokens", None),
            total_tokens=(getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0),
        ),
        cache_stats=cache_stats,
    )


# ── Message translation ─────────────────────────────────────────────────────


def _translate_messages(messages):
    """Extract system prompt and convert messages to Anthropic format.

    Returns (system_text, anthropic_messages).
    """
    system_parts = []
    translated = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            # First system message -> system param; later ones -> user messages
            if not translated:
                system_parts.append(content)
            else:
                # Mid-conversation system message (e.g. repair prompt) -> user message
                translated.append({"role": "user", "content": content})

        elif role == "user":
            translated.append({"role": "user", "content": content})

        elif role == "assistant":
            # Could be a plain text message or have tool_calls from _normalize_message
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            log.warning(
                                "tool_call_args_parse_failed name=%s args_preview=%r",
                                fn.get("name", ""), args[:200], exc_info=True,
                            )
                            args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                translated.append({"role": "assistant", "content": blocks})
            else:
                translated.append({"role": "assistant", "content": content or ""})

        elif role == "tool":
            # Tool result -> merge into a user message with tool_result blocks
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content or "",
            }
            # Merge with previous user message if it's already tool_results
            if translated and translated[-1]["role"] == "user" and isinstance(translated[-1]["content"], list):
                translated[-1]["content"].append(tool_result_block)
            else:
                translated.append({"role": "user", "content": [tool_result_block]})

    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, translated


def _translate_tool_choice(tool_choice):
    """Convert tool_choice string to Anthropic format."""
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        return {"type": "any"}
    return None


# ── Main completion function ─────────────────────────────────────────────────


def _create_chat_completion(*, workflow, messages, model=None, temperature=0.7, max_tokens=4096, timeout=60, tools=None, tool_choice=None):
    started = time.perf_counter()
    selected_model = _model_for_workflow(workflow, model=model)

    system_text, translated_messages = _translate_messages(messages)

    kwargs = {
        "model": selected_model,
        "messages": translated_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }

    # System prompt with prompt caching
    if system_text:
        kwargs["system"] = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    # Tools with prompt caching on the last tool definition
    if tools:
        cached_tools = [dict(t) for t in tools]
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
        kwargs["tools"] = cached_tools
    if tool_choice:
        translated_tc = _translate_tool_choice(tool_choice)
        if translated_tc:
            kwargs["tool_choice"] = translated_tc

    try:
        resp = _get_client().messages.create(**kwargs)
        wrapped = _wrap_anthropic_response(resp)
        usage = wrapped.usage
        duration = round((time.perf_counter() - started) * 1000, 2)
        runtime_status.record_llm_call(
            workflow,
            ok=True,
            model=selected_model,
            duration_ms=duration,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cache_creation_tokens=wrapped.cache_stats.creation_tokens,
            cache_read_tokens=wrapped.cache_stats.read_tokens,
        )
        try:
            db.record_llm_call(
                workflow, selected_model,
                ok=True,
                duration_ms=duration,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cache_creation_tokens=wrapped.cache_stats.creation_tokens,
                cache_read_tokens=wrapped.cache_stats.read_tokens,
            )
        except (OSError, sqlite3.Error):
            log.warning("llm_call_persist_failed workflow=%s", workflow, exc_info=True)
        return wrapped
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
        # which workflow / caller ran. Lazy import to dodge the runtime.app →
        # elixir_agent → agent.core → runtime.app cycle.
        try:
            from runtime.app import schedule_llm_failure_alert
            schedule_llm_failure_alert(workflow)
        except Exception:
            log.warning("schedule_llm_failure_alert_import_failed", exc_info=True)
        raise




__all__ = [name for name in globals() if not name.startswith("__")]
