"""Shared core state for the agent package."""

import contextvars
import logging
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import NamedTuple

from anthropic import Anthropic, APIConnectionError, APIError, BadRequestError

import db
from agent.workflow_registry import workflow_model_family
from runtime import status as runtime_status

log = logging.getLogger("elixir_agent")


class SpendCeilingReached(RuntimeError):
    """Raised instead of making a model call once the daily ceiling is hit.

    An exception rather than a None return on purpose: every caller already
    handles a failed turn (the cursors hold, the daily deliberation inherits),
    whereas a silent empty result would look like "nothing to say" and quietly
    consume the moment. Hard-post workflows never see this — see the explicit
    policy boundary in agent/spend_budget.py.
    """


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

# Per-workflow timeouts live in MODEL_CALL_POLICY below, alongside the ceiling
# and the effort level. They used to be split across this module-level map and
# ~6 literals at call sites, and the two disagreed about precedence: the map won
# over an explicit per-call `timeout=`, so a caller asking for more time on a
# mapped workflow was silently ignored. The policy has one rule instead — an
# explicit argument always wins, because that is what "override" means.


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
RELEASE_CODENAME = os.getenv("ELIXIR_RELEASE_CODENAME", "Decisive Dart Goblin")
RELEASE_STAMP = os.getenv("ELIXIR_RELEASE_STAMP", "2026-08-02")
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


def _creative_model_name():
    return os.getenv("ELIXIR_CREATIVE_MODEL", "claude-opus-5")


def _intensive_model_name():
    # Low-volume intensive writing: release notes, weekly recap, scouting.
    # Opus 5 (2026-07-31): ~30 calls/week where writing quality IS the product.
    return os.getenv("ELIXIR_INTENSIVE_MODEL", "claude-opus-5")


def _lightweight_model_name():
    return os.getenv("ELIXIR_LIGHTWEIGHT_MODEL", "claude-haiku-4-5-20251001")


def _model_for_workflow(workflow, model=None):
    if model:
        return model
    model_family = workflow_model_family(workflow)
    if model_family == "creative":
        return _creative_model_name()
    if model_family == "chat":
        return _chat_model_name()
    if model_family == "intensive":
        return _intensive_model_name()
    return _lightweight_model_name()


# ── The model-call policy ────────────────────────────────────────────────────
#
# One row per workflow: how long its answer may be, and how hard it may think.
# This is the single place either number is chosen.
#
# It is centralized because the alternative was tried and failed quietly. Until
# 2026-08-08 `max_tokens` was a literal at ~25 call sites across six files —
# 200, 300, 400, 512, 600, 900, 1200, 1600, 2000, 2048, 2500, 4096, 8192, 9000,
# 16384 — almost none carrying a reason, and `effort` was set nowhere at all.
# Nobody could see the set, so nobody could tell a deliberate ceiling from a
# copied one, and three separate workflows silently truncated for weeks.
#
# max_tokens is a CEILING (what the answer may not exceed) and effort is the
# SPEND (how much thinking actually happens). They are different levers and the
# common mistake is to reach for the first when you mean the second: raising a
# ceiling never shortens an answer, and lowering one truncates rather than
# tightens. Keep ceilings generous — an unused ceiling is free — and control
# cost with effort.
#
# `budget_tokens` is deliberately absent: it was removed from these models and
# now returns a 400, so effort is the only thinking control that exists.
# A too-low timeout does not just fail — it fails EXPENSIVELY. `max_retries` is
# left at the SDK default of 2, which is what we want for 429/529/5xx, but it
# also applies to timeouts: a request that times out is retried twice, so the
# real wall clock before the caller learns anything is timeout x 3.
#
# That is not theoretical. Every timeout failure in the telemetry — all five of
# them, across awareness, deck_review and memory_synthesis — took ~181.7s, which
# is 60s x 3 exactly. And several SUCCESSES sit above 60s (deck_review at 104.8,
# weekly_recap_email at 102.8): those are first attempts that timed out and were
# silently rescued by a retry, at double the latency, with nothing logged.
#
# So the rule for these values is: clear the workflow's observed maximum with
# real margin. Timeouts are not a cost control — effort is. A timeout's only job
# is to stop a genuinely wedged call, and setting it near the working duration
# buys 3x latency and a failure instead.
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 60


class CallPolicy(NamedTuple):
    max_tokens: int
    effort: str = DEFAULT_EFFORT
    timeout: int = DEFAULT_TIMEOUT


# Effort is only named where it differs from the API default, so a row with just
# a ceiling reads as "nothing special about this one".
MODEL_CALL_POLICY: dict[str, CallPolicy] = {
    # ── The brain and the long-form writing: full depth, room to finish ──
    #
    # The 16384s are not guesses. Each of these ran on an Opus-tier model with a
    # ceiling sized for its visible answer, spent the whole budget on thinking
    # before writing a character, and failed silently: memory_synthesis returned
    # completion_chars=0 against a 3000 ceiling (2026-08-02), and weekly_recap
    # did the same at 1600 and never sent the Monday clan report (2026-08-03).
    # These run weekly or less, so headroom is a rounding error against the cost
    # of the output silently not existing — and an unused ceiling is not billed.
    #
    # Timeouts here are 300s against observed maxima of 152s (awareness), 111s
    # (release_notes), 103s (weekly_recap_email) and 87s (memory_synthesis).
    # These are background jobs on a weekly or twice-daily cadence, so latency
    # costs nothing and a timeout costs the whole deliverable.
    "awareness": CallPolicy(8192, timeout=300),
    # A repair must return the complete original plan as JSON as well as its
    # corrected copy.  A live multi-post repair exhausted 8192 tokens and then
    # failed closed because the truncated response omitted posts (2026-08-17).
    "awareness_repair": CallPolicy(16384, timeout=300),
    "memory_synthesis": CallPolicy(16384, timeout=300),
    "weekly_recap": CallPolicy(16384, timeout=300),
    "weekly_recap_email": CallPolicy(16384, timeout=300),
    "release_notes": CallPolicy(8192, timeout=300),
    "screenshot_readout": CallPolicy(9000, timeout=120),
    # ── Conversation and composition: real judgment, not deep reasoning ──
    "interactive": CallPolicy(4096, "medium", timeout=90),
    "clanops": CallPolicy(8192, "medium", timeout=90),
    # Ran at the 60s default and its longest SUCCESS was 104.8s — i.e. a first
    # attempt that timed out and was rescued by a retry — plus one outright
    # failure at 181.6s (60 x 3).
    "deck_review": CallPolicy(4096, "medium", timeout=180),
    # One call writes copy for all FIVE channels, so its old 1500 was ~300 each
    # before thinking — and it truncated at exactly 1500 on 2026-08-07, failing
    # promotion_content_cycle outright.
    "recruiting_copy": CallPolicy(8192, "medium", timeout=90),
    # Its largest SUCCESSFUL response was 1367 tokens against a 1400 ceiling; a
    # 2% margin is not a margin. Length scales with how many battle types a
    # member played.
    "member_report": CallPolicy(2048, "medium"),
    # Its longest success is 60.0s to the tenth — sitting exactly on the old
    # default, which means it has been clipping the wall rather than finishing.
    "leader_action_feedback": CallPolicy(1200, "medium", timeout=120),
    "intel_report": CallPolicy(4096, "medium"),
    "tournament_recap": CallPolicy(2500, "medium"),
    # Never had a ceiling of its own — it inherited _chat_with_tools' 4096
    # default, which nobody chose for it. Written down at the value it was
    # already getting; surfaced by the policy-coverage test, not by an outage.
    "tournament_update": CallPolicy(4096, "medium"),
    "war_intel": CallPolicy(2000, "medium"),
    "game_factual_repair": CallPolicy(1600, "medium"),
    "elder_standing": CallPolicy(1200, "medium"),
    "clan_chat_copy": CallPolicy(900, "medium"),
    # One short Discord post from an already-assembled read, and the worked
    # example for why these are two separate levers. Sized against its visible
    # answer it failed twice: at 1400 it died mid-tool-call every day for two
    # weeks, and at 4096 it truncated having emitted no text and no tool call at
    # all — the whole budget gone to thinking. The ceiling is generous because
    # ceilings are free; `medium` is what actually holds the spend down, and it
    # cut a real run from 1708 tokens to 788.
    "ask_elixir_daily": CallPolicy(16384, "medium", timeout=120),
    # ── Scoped: pick a route, answer one event, write one line ──
    #
    # These sit on a member-facing path, so a short timeout is deliberate — but
    # remember the retry multiplier: 30s here is 90s of wall clock before the
    # caller falls back, which is why none of them go lower.
    "help": CallPolicy(600, "low"),
    "intent_router": CallPolicy(512, "low", timeout=30),
    "reception": CallPolicy(400, "low"),
    "leader_note_interpret": CallPolicy(400, "low"),
    "member_outreach_ask": CallPolicy(300, "low"),
    "wake_response": CallPolicy(4096, "low", timeout=90),
    "wake_response_chat": CallPolicy(4096, "low", timeout=90),
    # ── Lightweight family (Haiku 4.5): effort is REJECTED on these models and
    # is dropped before the request, so the value here is inert. See
    # `_supports_effort`. ──
    # These run inline behind a member's message, so they keep tight timeouts.
    "memory_inference": CallPolicy(1500, timeout=20),
    # 12 of 71 calls hit the old 100 ceiling while successful ones averaged 71
    # tokens and peaked at 99 — finishing flush against the wall. The 1-2
    # sentence budget is set by the system prompt; this is only the safety net.
    "memory_distill": CallPolicy(256, timeout=15),
    "lightweight": CallPolicy(200),
}


# One agent turn can be several API calls — a tool-using workflow loops until the
# model stops asking for tools. Without a shared id, per-workflow totals answer
# "what did awareness cost this week" but never "what did one tick cost", which
# is the question a cost report actually needs. A ContextVar rather than a
# parameter so nothing in the call chain has to thread it through, and so
# concurrent turns on different asyncio tasks cannot borrow each other's id.
_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "elixir_turn_id", default=None
)


class turn:
    """Group every model call made inside this block under one turn id.

    Re-entrant on purpose: a workflow that calls another workflow stays part of
    the outer turn, because the outer turn is what actually cost the money.
    """

    def __init__(self):
        self._token = None

    def __enter__(self):
        if _turn_id.get() is None:
            self._token = _turn_id.set(uuid.uuid4().hex[:16])
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _turn_id.reset(self._token)
        return False


def current_turn_id() -> str | None:
    return _turn_id.get()


def policy_for(workflow: str) -> CallPolicy:
    """The ceiling, thinking depth and timeout for one workflow.

    Unknown workflows get the defaults rather than raising: a missing row should
    degrade to a sane call, not take down the caller.
    """
    return MODEL_CALL_POLICY.get(workflow, CallPolicy(DEFAULT_MAX_TOKENS, DEFAULT_EFFORT))


# ── Model capability gates ───────────────────────────────────────────────────
#
# These are API *removals*, not style preferences: sending a parameter a model
# has dropped is a 400, not a warning. Both are keyed on the model id prefix
# because that is what the API validates against, and they point in opposite
# directions — which is why they live together instead of being discovered one
# outage at a time.
#
# `output_config.effort` is rejected on Haiku 4.5, the whole `lightweight`
# family (memory_distill, triage, the wake responder's first rung). Those models
# also do not think by default, so there is nothing to bound there anyway.
_MODELS_WITHOUT_EFFORT = ("claude-haiku-4-5",)

# `temperature` / `top_p` / `top_k` were removed on the Claude 5 generation and
# on Opus 4.7/4.8. They remain live on Haiku 4.5 and Sonnet 4.6, so this cannot
# be a blanket removal — dropping temperature everywhere would silently change
# lightweight behaviour.
#
# Until 2026-08-08 this module sent temperature to every model and absorbed the
# resulting 400 with a retry, so EVERY claude-sonnet-5 and claude-opus-5 call
# made two API round trips: one guaranteed failure, then the real request.
# Measured over the 7 days to 2026-08-08: 233 sonnet-5 + 16 opus-5 calls, i.e.
# 249 wasted round trips, on a workload averaging 18s (sonnet) and 40s (opus)
# per call. The retry below still exists as a backstop for a model whose rules
# change under us, but it now logs when it fires instead of hiding the cost.
_MODELS_WITHOUT_SAMPLING = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
)


def _supports_effort(model: str) -> bool:
    return not any(model.startswith(prefix) for prefix in _MODELS_WITHOUT_EFFORT)


def _supports_sampling(model: str) -> bool:
    """False when the model rejects temperature/top_p/top_k outright."""
    return not any(model.startswith(prefix) for prefix in _MODELS_WITHOUT_SAMPLING)


def _effort_for_workflow(workflow: str) -> str:
    return policy_for(workflow).effort


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
    JSON string — or None on failure.

    Also records a census of every content block. These models emit `thinking`
    blocks without being asked to (nothing here ever sets the `thinking`
    parameter), and `response_text`/`response_tool_uses` filter by exact type, so
    a response that was ALL thinking used to serialize as empty text and no tool
    calls. On 2026-08-08 that made a 4096-token ask_elixir_daily truncation look
    like the model had returned literally nothing, and the cause was only found
    by re-running the workflow and inspecting the live blocks. Sizes, not
    content: enough to explain where the tokens went, cheap to store.
    """
    if resp is None:
        return None
    try:
        import json as _json

        census: dict[str, dict] = {}
        for block in getattr(resp, "content", None) or []:
            kind = getattr(block, "type", None) or "unknown"
            entry = census.setdefault(kind, {"blocks": 0, "chars": 0})
            entry["blocks"] += 1
            if kind == "text":
                entry["chars"] += len(getattr(block, "text", "") or "")
            elif kind == "tool_use":
                entry["chars"] += len(str(getattr(block, "input", "") or ""))
            else:
                entry["chars"] += len(str(getattr(block, "thinking", "") or ""))

        return _json.dumps(
            {
                "stop_reason": getattr(resp, "stop_reason", None),
                "text": response_text(resp),
                "tool_uses": [{"name": b.name, "input": b.input} for b in response_tool_uses(resp)],
                "block_census": census,
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
    max_tokens=None,
    timeout=None,
    tools=None,
    tool_choice=None,
):
    """Call the Anthropic Messages API and return the native Message response.

    `max_tokens=None` and `timeout=None` — the normal case — take the workflow's
    values from MODEL_CALL_POLICY. Pass a number only for a genuine per-call
    override, such as a retry that needs more headroom than the usual ceiling;
    an explicit argument always wins over the policy.

    messages: native Anthropic messages (user/assistant roles only; content is
    a string or a list of content blocks — SDK block objects from a prior
    response are fine). The system prompt goes in `system`, not a message.
    """
    started = time.perf_counter()

    policy = policy_for(workflow)
    if max_tokens is None:
        max_tokens = policy.max_tokens
    if timeout is None:
        timeout = policy.timeout

    # The daily spend ceiling. Refused BEFORE the call, because the point is to
    # not spend. Only awareness and Ask Elixir workflows participate; scheduled
    # jobs and hard-post responders are outside this ledger. Raising rather than
    # returning is deliberate: a caller that silently treats "no budget" as
    # "no news" would turn a cost control into missing clan history.
    from agent.spend_budget import may_run

    allowed, why = may_run(workflow)
    if not allowed:
        log.warning("spend budget: refusing %s — %s", workflow, why)
        raise SpendCeilingReached(f"{workflow}: {why}")

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

    kwargs = {
        "model": selected_model,
        "messages": sanitized_messages,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }

    # Only send what this model still accepts. Both gates are cheap string
    # checks that replace a round trip each.
    if _supports_sampling(selected_model):
        kwargs["temperature"] = temperature

    # Bound how much of max_tokens thinking may consume. See EFFORT_BY_WORKFLOW —
    # this replaces the removed `budget_tokens`.
    if _supports_effort(selected_model):
        kwargs["output_config"] = {"effort": policy.effort}

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

    # API round trips this call cost. Anything above 1 is waste worth seeing:
    # a rejected-parameter retry, and (invisibly to us, inside the SDK) a
    # timeout retry. It stayed at 2 for every claude-5 call for months with
    # nothing recording it.
    attempts = 1

    try:
        call_started_at = datetime.now(timezone.utc)
        try:
            resp = _get_client().messages.create(**kwargs)
        except BadRequestError as e:
            # Backstop only. `_supports_sampling` should already have kept
            # temperature off any model that rejects it, so reaching here means
            # a model started refusing it without being in
            # `_MODELS_WITHOUT_SAMPLING` — the request still succeeds, but every
            # call to that model is now paying a wasted round trip until the
            # list is updated. That is worth an ERROR: it was silent for months.
            if "temperature" in str(e) and "temperature" in kwargs:
                log.error(
                    "sampling_rejected model=%s — temperature was sent to a model that "
                    "refuses it; add its prefix to _MODELS_WITHOUT_SAMPLING or every "
                    "call to it costs an extra failed round trip",
                    selected_model,
                )
                kwargs.pop("temperature")
                attempts += 1
                call_started_at = datetime.now(timezone.utc)
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
        # A truncated answer is a failed call that the API reports as a success,
        # so nothing above raises and every caller sees a plausible short string.
        # This is the ONLY point every call passes through — `_chat_with_tools`
        # catches truncation for the workflows that pass `return_errors=True`,
        # but direct `chat()` callers (memory_distill, member_report,
        # release_notes) had no detection at all. Telemetry recorded 39
        # truncations between 2026-07-28 and 2026-08-06 while the log held 16,
        # all at WARNING, so none reached logs/elixir-error.log and none were
        # ever acted on. ERROR because the output was cut off mid-thought: the
        # job did not degrade, it produced a wrong answer.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            log.error(
                "llm_truncated workflow=%s model=%s max_tokens=%d completion_tokens=%s — "
                "output was cut off; raise max_tokens for this workflow",
                workflow,
                selected_model,
                max_tokens,
                completion_tokens,
            )
        # Charge the clan-DB spend counter. Wrapped because a counter that can
        # fail a successful call is worse than one that undercounts by one.
        # `cost_usd` is kept rather than discarded: it was already being computed
        # here on every call, and storing it prices each row at the rates in
        # force when it ran, so history stays correct when rates change and
        # reports stop maintaining a second copy of the pricing table.
        cost_usd = None
        try:
            from agent.spend_budget import call_cost_usd, record_spend_usd

            cost_usd = call_cost_usd(
                selected_model,
                prompt_tokens,
                completion_tokens,
                cache_creation_tokens,
                cache_read_tokens,
                effective_at=call_started_at,
            )
            record_spend_usd(workflow, cost_usd, now=call_started_at)
        except Exception:
            log.debug("spend budget: could not record call cost", exc_info=True)

        response_json = _serialize_response(resp)
        # Promote the block census to its own column. response_json is pruned at
        # 14 days while the row lives 90, and the census is how you tell "the
        # model returned nothing" from "the model spent the budget thinking" —
        # so it must outlive the blob it is currently carried in.
        census_json = None
        try:
            import json as _json

            census = (_json.loads(response_json) or {}).get("block_census")
            census_json = _json.dumps(census) if census else None
        except Exception:  # noqa: BLE001
            # hygiene: the census is a reporting detail on an optional blob. The
            # row still records without it.
            census_json = None
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
                response_json=response_json,
                effort=policy.effort if _supports_effort(selected_model) else None,
                max_tokens=max_tokens,
                timeout_s=timeout,
                stop_reason=getattr(resp, "stop_reason", None),
                block_census=census_json,
                attempts=attempts,
                cost_usd=cost_usd,
                turn_id=current_turn_id(),
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
                # A failure is exactly when the configuration matters most: the
                # 181.7s timeout cluster was only diagnosable once you knew the
                # timeout was 60 and the SDK retries twice.
                effort=policy.effort if _supports_effort(selected_model) else None,
                max_tokens=max_tokens,
                timeout_s=timeout,
                attempts=attempts,
                turn_id=current_turn_id(),
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
