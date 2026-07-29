"""Shared core state for the agent package."""

import logging
import os
import sqlite3
import subprocess
import threading
import time

from anthropic import Anthropic, APIConnectionError, APIError, BadRequestError

import db
from agent.workflow_registry import workflow_model_family
from runtime import status as runtime_status

log = logging.getLogger("elixir_agent")

# Workflows to exclude from prompt caching. Any workflow that makes several
# tool-calling rounds seconds apart within one turn gets cache reads on rounds
# 2+, so the 1.25x write premium pays off inside the same turn. The feedback
# synthesis is a single-shot, sparsely triggered call: its measured 7-day
# read/write ratio was below the 0.28 break-even point (#237).
# (Awareness used to live here from its v4.5 single-call-per-tick reflex days —
# it is now a multi-round Sonnet agentic loop where caching is a large net win.)
WORKFLOWS_WITHOUT_CACHE: set[str] = {"leader_action_feedback"}

# Workflows whose STABLE prefix (system prompt + tool defs) should use the 1-hour
# cache TTL instead of the 5-minute default. The 1h TTL costs a 2x write premium and
# only pays off when consecutive calls land within ~1h so the prefix is cache-READ on
# the next call instead of re-created.
#
# Awareness was here (it ran hourly), but the cost gate (runtime/awareness/gate.py,
# 2026-07-12) means the Sonnet brain now runs only on posts — sparsely, typically
# MORE than 1h apart. At that cadence the 1h prefix always expires between ticks, so
# the 2x write premium buys nothing (no cross-tick read) while the cheaper 5m default
# still fully covers the within-tick multi-round loop (rounds are seconds apart). So
# awareness is intentionally NOT here anymore — the 5m default is strictly cheaper for
# a sparse cadence. Re-add it only if the brain returns to a dense <=1h cadence.
# NOTE: session/bursty workflows (interactive/clanops/deck_review) were never here —
# they complete in one burst, so the cheaper 5m write is better for them too.
LONG_CACHE_TTL_WORKFLOWS: set[str] = set()

# Per-workflow request timeouts (seconds) override the 60s default. Sonnet 4.6
# on the weekly memory_synthesis batch (~75K input tokens) routinely completes
# in 55-120s, which trips the default timeout and triggers SDK retries that
# the model can't outrun — three Sunday runs failed in a row (2026-05-03/10/17)
# before this override was added.
WORKFLOW_TIMEOUT_OVERRIDES = {
    "memory_synthesis": 300,
    # The awareness loop is Sonnet deliberating over the full read plus a tool
    # round-trip (get_elixir_state); like memory_synthesis it can run past the
    # 60s default, and a timeout there silently collapses the tick to "silence."
    "awareness": 180,
}


def _get_build_hash():
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=os.path.dirname(__file__) or ".",
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except subprocess.SubprocessError, OSError:
        log.warning(
            "build hash unavailable — git failed at boot; releases will record 'unknown'",
            exc_info=True,
        )
        return "unknown"


BUILD_HASH = _get_build_hash()
# Releases are identified by coined name + date + build hash (no version number).
# cut_release.py rewrites these defaults; BUILD_HASH is read live from git at boot.
RELEASE_CODENAME = os.getenv("ELIXIR_RELEASE_CODENAME", "Phantom Phoenix")
RELEASE_STAMP = os.getenv("ELIXIR_RELEASE_STAMP", "2026-07-27")
RELEASE_LABEL = (
    f"{RELEASE_CODENAME} ({RELEASE_STAMP})"
    if RELEASE_CODENAME and RELEASE_STAMP
    else (RELEASE_CODENAME or RELEASE_STAMP or "unversioned")
)

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
    return os.getenv("ELIXIR_CHAT_MODEL", "claude-sonnet-5")


def _promotion_model_name():
    return os.getenv("ELIXIR_PROMOTION_MODEL", "claude-opus-4-8")


def _intensive_model_name():
    # Low-volume intensive writing: release notes, weekly recap, scouting.
    return os.getenv("ELIXIR_INTENSIVE_MODEL", "claude-opus-4-8")


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
    if model_family == "intensive":
        return _intensive_model_name()
    return _lightweight_model_name()


MAX_TOOL_ROUNDS = 3
MAX_CONTEXT_MEMBERS_DEFAULT = 30
MAX_CONTEXT_MEMBERS_FULL = 50
TOOL_RESULT_MAX_ITEMS = 50
TOOL_RESULT_MAX_CHARS = 20000


def _build_system_prompt(*sections):
    parts = [s for s in sections if s]
    parts.append(f"Your release: {RELEASE_LABEL}")
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
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    if str(block.get("text") or "").strip():
                        return True
                    continue
                if block.get("type"):
                    return True
                continue
            if block:
                return True
        return False
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


def _with_message_cache_breakpoint(messages):
    """Mark the final content block of the last message with an ephemeral
    cache_control breakpoint, so the whole conversation prefix (system + tools +
    all prior turns) is cached up to that point.

    In a multi-round tool loop the history only grows — each round appends and
    re-sends everything before it — so putting the breakpoint at the current end
    lets the *next* round read that prefix from cache instead of re-billing it at
    full price. The last message before a completion is always one we build (the
    initial user string, or a tool_result user turn); raw SDK assistant blocks
    are only ever mid-history, never last, so this never mutates an SDK object.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        new_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        new_content = list(content)
        new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
    else:
        return messages  # unexpected shape (e.g. SDK-object tail) — skip, don't risk it
    return messages[:-1] + [{**last, "content": new_content}]


# ── Prompt capture (always on) ───────────────────────────────────────────────
# Every LLM call records its full assembled prompt + response onto the
# ``llm_calls`` row, so anything Elixir sends to the model is inspectable in the
# Observatory (drill into any call). Serialization is best-effort and guarded —
# a capture failure must never break the actual call. The blobs are pruned after
# LLM_PROMPT_RETENTION_DAYS; the metadata row lives LLM_CALL_RETENTION_DAYS.


def _jsonable_block(block):
    """One content block (dict OR native SDK block) → a plain JSON-able dict."""
    if isinstance(block, dict):
        return block
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "input": getattr(block, "input", None),
        }
    if btype == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "content": getattr(block, "content", None),
        }
    return {"type": btype or "unknown", "repr": str(block)[:2000]}


def _jsonable_messages(messages):
    """Serialize the messages list (mixed dict / SDK-block content) for capture."""
    out = []
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            ser = content
        elif isinstance(content, list):
            ser = [_jsonable_block(b) for b in content]
        else:
            ser = content
        out.append({"role": m.get("role") if isinstance(m, dict) else "?", "content": ser})
    return out


def _serialize_prompt(system, messages, tools, max_tokens, temperature):
    """The full assembled prompt, as a JSON string — or None on failure.
    Tool DEFINITIONS are large and static; the names are the useful signal."""
    try:
        import json as _json

        return _json.dumps(
            {
                "system": system,
                "messages": _jsonable_messages(messages),
                "tools": [t.get("name") for t in (tools or []) if isinstance(t, dict)],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            default=str,
        )
    except Exception:
        log.debug("prompt capture: prompt serialize failed (ignored)", exc_info=True)
        return None


def _serialize_response(resp):
    """The model's response (text + requested tool calls + stop reason) as a
    JSON string — or None on failure."""
    if resp is None:
        return None
    try:
        import json as _json

        return _json.dumps(
            {
                "stop_reason": getattr(resp, "stop_reason", None),
                "text": response_text(resp),
                "tool_uses": [{"name": b.name, "input": b.input} for b in response_tool_uses(resp)],
            },
            default=str,
        )
    except Exception:
        log.debug("prompt capture: response serialize failed (ignored)", exc_info=True)
        return None


# ── Main completion function ─────────────────────────────────────────────────


def _create_chat_completion(
    *,
    workflow,
    messages,
    system=None,
    model=None,
    temperature=0.7,
    max_tokens=4096,
    timeout=60,
    tools=None,
    tool_choice=None,
):
    """Call the Anthropic Messages API and return the native Message response.

    messages: native Anthropic messages (user/assistant roles only; content is
    a string or a list of content blocks — SDK block objects from a prior
    response are fine). The system prompt goes in `system`, not a message.
    """
    started = time.perf_counter()
    selected_model = _model_for_workflow(workflow, model=model)

    sanitized_messages = _sanitize_anthropic_messages(messages)
    # Snapshot the semantic prompt (pre cache-control markers) for capture — what
    # the model sees, without the ephemeral-cache plumbing.
    prompt_json = _serialize_prompt(system, sanitized_messages, tools, max_tokens, temperature)

    cache_enabled = workflow not in WORKFLOWS_WITHOUT_CACHE
    # Stable prefix (system + tools) gets a 1h TTL for periodic workflows so it
    # survives the gap between runs; the volatile message prefix always stays 5m.
    prefix_cc = {"type": "ephemeral"}
    if cache_enabled and workflow in LONG_CACHE_TTL_WORKFLOWS:
        prefix_cc = {"type": "ephemeral", "ttl": "1h"}
    if cache_enabled:
        # Cache the growing message prefix (the read + accumulated tool results),
        # not just system+tools — that payload is the bulk of the re-sent input.
        sanitized_messages = _with_message_cache_breakpoint(sanitized_messages)

    effective_timeout = WORKFLOW_TIMEOUT_OVERRIDES.get(workflow, timeout)

    kwargs = {
        "model": selected_model,
        "messages": sanitized_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": effective_timeout,
    }

    # System prompt with optional prompt caching
    if system:
        system_block = {"type": "text", "text": system}
        if cache_enabled:
            system_block["cache_control"] = prefix_cc
        kwargs["system"] = [system_block]

    # Tools with optional prompt caching on the last tool definition
    if tools:
        if cache_enabled:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": prefix_cc}
            kwargs["tools"] = cached_tools
        else:
            kwargs["tools"] = list(tools)
    if tool_choice:
        translated_tc = _TOOL_CHOICE_MAP.get(tool_choice)
        if translated_tc:
            kwargs["tool_choice"] = translated_tc

    try:
        try:
            resp = _get_client().messages.create(**kwargs)
        except BadRequestError as e:
            # Sonnet 5 rejects the temperature param outright ("`temperature`
            # is deprecated for this model"). Retry once without it so one
            # model family's API change can't 400 every chat-tier call.
            if "temperature" in str(e) and "temperature" in kwargs:
                kwargs.pop("temperature")
                resp = _get_client().messages.create(**kwargs)
            else:
                raise
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
                workflow,
                selected_model,
                ok=True,
                duration_ms=duration,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
                prompt_json=prompt_json,
                response_json=_serialize_response(resp),
            )
        except OSError, sqlite3.Error:
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
                workflow,
                selected_model,
                ok=False,
                error=exc,
                duration_ms=duration,
                prompt_json=prompt_json,
                response_json=None,
            )
        except OSError, sqlite3.Error:
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
