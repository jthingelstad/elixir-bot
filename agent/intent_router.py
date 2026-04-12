"""LLM-based intent router.

Replaces the regex waterfall in `runtime/channel_router.py`. Given a raw user
message and the channel context, returns a structured `Intent` describing
which handler should respond.

The router uses Haiku via tool-use for forced structured output — there is no
JSON parsing, the SDK hands us a dict directly.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Literal, TypedDict

from agent.core import _create_chat_completion, _lightweight_model_name
from runtime.intent_registry import (
    ROUTE_KEYS,
    get_route,
    router_route_summaries,
)

log = logging.getLogger("elixir_agent")

INTENT_ROUTER_WORKFLOW = "intent_router"

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "subagents" / "intent_router.md"


class Intent(TypedDict, total=False):
    route: str
    mode: str | None
    target_member: Literal["self", "other"] | None
    confidence: float
    rationale: str
    latency_ms: float
    model: str
    fallback_reason: str  # set when classification failed and we returned llm_chat


_SELECT_ROUTE_TOOL = {
    "name": "select_route",
    "description": "Pick the route that best matches the user's intent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "enum": ROUTE_KEYS,
                "description": "The single route key that should handle this message.",
            },
            "mode": {
                "type": ["string", "null"],
                "enum": ["regular", "war", "full", "short", None],
                "description": "Sub-mode for routes that need it (deck_*, clan_status). Omit otherwise.",
            },
            "target_member": {
                "type": ["string", "null"],
                "enum": ["self", "other", None],
                "description": "Whether the request is about the speaker (self), a specific other member (other), or no specific member (null).",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "How confident you are in this classification (0.0–1.0).",
            },
            "rationale": {
                "type": "string",
                "description": "One short sentence explaining the choice.",
            },
        },
        "required": ["route", "confidence", "rationale"],
    },
}


def _load_prompt(workflows: tuple[str, ...]) -> str:
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{ROUTE_TABLE}", router_route_summaries(workflows))


_HISTORY_TURNS = 4  # Last N turns (user+assistant combined) the router sees.
_HISTORY_CHAR_CAP = 800  # Per-turn content cap to keep router prompt compact.


def _format_conversation_history(history: list[dict] | None) -> str:
    if not history:
        return ""
    # Oldest-first preferred; db.list_thread_messages already returns ASC.
    recent = list(history)[-_HISTORY_TURNS:]
    lines = []
    for turn in recent:
        role = turn.get("role") or turn.get("author_type") or "user"
        content = turn.get("content") or ""
        if isinstance(content, list):
            content = " ".join(str(c) for c in content if c)
        content = str(content).strip().replace("\n", " ")
        if len(content) > _HISTORY_CHAR_CAP:
            content = content[:_HISTORY_CHAR_CAP] + "…"
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _build_user_message(
    question: str,
    *,
    workflow: str,
    mentioned: bool,
    allows_open_channel_reply: bool,
    history_text: str = "",
) -> str:
    parts = [
        f"Channel workflow: {workflow}",
        f"Bot was mentioned: {mentioned}",
        f"Channel allows open-channel replies: {allows_open_channel_reply}",
        "",
    ]
    if history_text:
        parts.extend([
            "Recent conversation (oldest first):",
            history_text,
            "",
        ])
    parts.extend([
        f"Current message: {question}",
        "",
        "Call select_route exactly once.",
    ])
    return "\n".join(parts)


def classify_intent(
    question: str,
    *,
    workflow: str,
    mentioned: bool,
    allows_open_channel_reply: bool = False,
    conversation_history: list[dict] | None = None,
    model: str | None = None,
) -> Intent:
    """Classify a Discord message into a route. Always returns an Intent.

    ``conversation_history`` is a list of ``{role, content}`` dicts, oldest
    first. The most recent turns are included in the router prompt so that
    follow-ups inherit mode/subject from the previous bot response.

    On failure (LLM error, missing tool call, unknown route) returns an Intent
    with route='llm_chat' and a fallback_reason field set, so the caller can
    log it and continue.
    """
    started = time.perf_counter()
    selected_model = model or _lightweight_model_name()

    workflows = ("interactive", "clanops") if workflow not in {"interactive", "clanops"} else (workflow,)
    system_prompt = _load_prompt(workflows)
    history_text = _format_conversation_history(conversation_history)
    user_msg = _build_user_message(
        question,
        workflow=workflow,
        mentioned=mentioned,
        allows_open_channel_reply=allows_open_channel_reply,
        history_text=history_text,
    )

    try:
        resp = _create_chat_completion(
            workflow=INTENT_ROUTER_WORKFLOW,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            model=selected_model,
            temperature=0.0,
            max_tokens=512,
            tools=[_SELECT_ROUTE_TOOL],
            tool_choice="required",
        )
    except Exception as exc:
        log.warning("intent_router_call_failed: %s", exc, exc_info=True)
        return _fallback_intent(started, selected_model, f"llm_error: {exc.__class__.__name__}")

    tool_calls = (resp.choices[0].message.tool_calls or []) if resp.choices else []
    if not tool_calls:
        return _fallback_intent(started, selected_model, "no_tool_call")

    call = tool_calls[0]
    try:
        import json as _json
        args = _json.loads(call.function.arguments) if isinstance(call.function.arguments, str) else dict(call.function.arguments)
    except Exception:
        return _fallback_intent(started, selected_model, "tool_args_parse_error")

    route = args.get("route")
    if route not in ROUTE_KEYS:
        return _fallback_intent(started, selected_model, f"unknown_route: {route!r}")

    intent: Intent = {
        "route": route,
        "mode": _normalize_mode(route, args.get("mode")),
        "target_member": _normalize_target(args.get("target_member")),
        "confidence": _coerce_float(args.get("confidence"), default=0.0),
        "rationale": str(args.get("rationale") or "")[:500],
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "model": selected_model,
    }
    return intent


def _fallback_intent(started: float, model: str, reason: str) -> Intent:
    return {
        "route": "llm_chat",
        "mode": None,
        "target_member": None,
        "confidence": 0.0,
        "rationale": f"router fallback: {reason}",
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "model": model,
        "fallback_reason": reason,
    }


def _normalize_mode(route: str, mode) -> str | None:
    if mode in (None, "", "null"):
        return None
    route_def = get_route(route)
    allowed = (route_def or {}).get("mode_choices") or []
    if not allowed:
        return None
    return mode if mode in allowed else None


def _normalize_target(target) -> Literal["self", "other"] | None:
    if target in ("self", "other"):
        return target
    return None


def _coerce_float(value, *, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "Intent",
    "INTENT_ROUTER_WORKFLOW",
    "classify_intent",
]
