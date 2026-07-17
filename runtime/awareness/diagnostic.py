"""Diagnostic rendering for the awareness loop.

The loop writes a diagnostic to the private leader-only **#thinking** channel
showing what the read looked like and what the brain posted this tick — the
train of thought behind every live decision.

This module is pure: ``build_diagnostic_render`` turns (read, plan, tool_trace)
into a structured render (an embed summary + a threaded detail body). The actual
Discord posting is done bot-native by the caller (runtime/app.py
``_awareness_post``), which owns the client and the event loop — this keeps the
renderer testable and free of I/O. The old #elixir-log webhook path was retired
2026-07-09.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("elixir")

# Tailnet-only Observatory base. The #thinking end-of-loop convenience link
# lands on the LLM view filtered to this tick's workflow — every awareness API
# round (the loop's rounds at the top) is drillable there for the full
# prompt/response. Overridable for a different tailnet host / port.
OBSERVATORY_URL = os.getenv("ELIXIR_OBSERVATORY_URL", "https://otto.tail09aaf9.ts.net:8444")

_HEADER = "AWARENESS (live)"
_MAX_LEN = 1900
_FIELD_MAX = 1024  # Discord embed field value cap

# Embed accent by outcome — a failed tick must read differently from a quiet one.
_COLORS = {"posted": 0x2ECC71, "silence": 0x95A5A6, "failed": 0xE74C3C}
_OUTCOME_LABEL = {"posted": "posted", "silence": "silence", "failed": "TICK FAILED"}


def _summarize_read(read: dict) -> str:
    read = read or {}
    lanes = read.get("signals_by_lane") or {}
    lane_counts = (
        ", ".join(f"{lane} {len(sigs)}" for lane, sigs in lanes.items() if sigs) or "no signals"
    )

    time_block = read.get("time") or {}
    if time_block:
        moment = time_block.get("phase_display") or time_block.get("phase") or "?"
        season = time_block.get("season_id")
        week = time_block.get("week")
        war_moment = f"{moment}"
        if season is not None:
            war_moment += f" (S{season}"
            if week is not None:
                war_moment += f" W{week}"
            war_moment += ")"
    else:
        war_moment = "no active war"

    hard = read.get("hard_post_signals") or []
    board = read.get("leader_action_board") or {}
    open_cards = len(board.get("open") or [])
    revisits = len(read.get("due_revisits") or [])
    degraded = read.get("_degraded") or []
    mgmt = read.get("management") or {}
    act = mgmt.get("actionable") or {}
    mgmt_line = (
        f"engine actionable: kick {len(act.get('kick') or [])} · "
        f"promote {len(act.get('promote') or [])} · demote {len(act.get('demote') or [])}"
    )

    lines = [
        f"**lanes:** {lane_counts}",
        f"**war:** {war_moment}",
        f"hard-post: {len(hard)} · open cards: {open_cards} · due revisits: {revisits}",
        mgmt_line,
    ]
    if degraded:
        lines.append(f"⚠️ degraded: {', '.join(degraded)}")
    return "\n".join(lines)


def _thinking_brief(tool_trace: list | None) -> str:
    """One-line tool summary for the embed (the full trace goes in the thread)."""
    trace = tool_trace or []
    if not trace:
        return "no tools called (decided from the read alone)"
    counts: dict[str, int] = {}
    for t in trace:
        name = t.get("tool") or "?"
        counts[name] = counts.get(name, 0) + 1
    parts = [f"{n}×{c}" if c > 1 else n for n, c in counts.items()]
    return f"{len(trace)} tool call(s): " + ", ".join(parts)


def _decision_brief(outcome: str, reason: str | None, posts: list) -> str:
    if outcome == "failed":
        return f"⚠️ TICK FAILED — {reason or 'nothing decided'}"
    if outcome == "silence":
        return f"silence — {reason or '(no reason given)'}"
    channels = ", ".join(f"#{p.get('channel') or '?'}" for p in posts) or "—"
    return f"{len(posts)} intended post(s): {channels}"


def _summarize_plan(plan: dict) -> str:
    """Full decision detail (the complete intended posts) for the thread."""
    from runtime.awareness.store import classify_plan

    plan = plan or {}
    outcome, reason = classify_plan(plan)
    if outcome == "failed":
        return f"**Decision** — ⚠️ TICK FAILED (nothing decided): {reason}"
    if outcome == "silence":
        return f"**Decision** — silence: {reason or '(no reason given)'}"

    posts = plan.get("posts") or []
    lines = [f"**Decision** — {len(posts)} intended post(s):"]
    for i, post in enumerate(posts, start=1):
        channel = post.get("channel") or "?"
        content = post.get("content")
        if isinstance(content, list):
            content = "\n\n".join(str(c) for c in content)
        content = (content or "").strip()
        covers = ", ".join(post.get("covers_signal_keys") or []) or "—"
        # Full intended post — the thread exists so it can be reviewed verbatim;
        # _chunk() splits the whole render across messages so nothing is cut off.
        lines.append(f"{i}. #{channel} — covers: {covers}")
        lines.append(content)
    return "\n".join(lines)


def _chunk(body: str, limit: int = _MAX_LEN) -> list[str]:
    """Split a long body into <=limit pieces on line boundaries so nothing —
    especially the decision — is silently dropped."""
    chunks: list[str] = []
    current = ""
    for line in body.split("\n"):
        if len(line) > limit:  # a single oversized line — hard-split it
            line = line[: limit - 1] + "…"
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def observatory_url(loop_number=None) -> str:
    """Link to the Observatory LLM view filtered to the awareness workflow —
    the loop's rounds sit at the top, each drillable for the full prompt +
    response. (loop_number is accepted for call-site symmetry; the LLM view is
    per-call, not per-loop.)"""
    del loop_number
    return f"{OBSERVATORY_URL.rstrip('/')}/llm?workflow=awareness"


def read_summary(read: dict) -> str:
    """The read summary shown when a loop opens (public wrapper)."""
    return _summarize_read(read)


def format_live_event(event: dict) -> str:
    """One line for a live tick event streamed into the #thinking thread as it
    happens — tool calls, truncation, retry. This is the runtime train of
    thought (vs the end-of-tick summary in ``build_diagnostic_render``)."""
    etype = event.get("type")
    if etype == "tool":
        name = event.get("tool") or "?"
        args = event.get("args") or ""
        res = event.get("result") or ""
        head = f"🔧 `{name}`"
        if args:
            head += f"({args})"
        return f"{head}\n   → {res}"
    if etype == "truncation":
        return (
            f"⚠️ **Response truncated** at max_tokens={event.get('max_tokens')} "
            f"(phase: {event.get('phase')})"
        )
    if etype == "retry":
        return (
            f"🔁 **Retrying** with {event.get('max_tokens')} tokens of headroom "
            f"({event.get('reason') or 'truncation'})"
        )
    return f"· {etype}"


def build_diagnostic_render(
    read: dict,
    plan: dict,
    *,
    tool_trace: list | None = None,
    loop_number=None,
) -> dict:
    """Turn (read, plan, tool_trace) into a structured render for #thinking.

    Returns a dict the poster consumes:
      - ``header``/``outcome``/``color`` — embed title + accent
      - ``fields`` — {Read, Thinking, Decision} embed field values (bounded)
      - ``thread_name`` — the per-loop thread title
      - ``thread_chunks`` — the verbatim decision only, pre-split to
        Discord-sized messages. The tool calls are NOT repeated here: they
        already streamed live into the thread as they happened (the end-of-loop
        re-dump was pure duplication). Full detail lives in the Observatory.
      - ``observatory_url`` — convenience deep-link to the loop-detail page
        (full read + tool trace + exact prompts/responses).
    Pure — no I/O. The caller does the bot-native posting.
    """
    from runtime.awareness.store import classify_plan

    plan = plan or {}
    outcome, reason = classify_plan(plan)
    posts = plan.get("posts") or []
    label = f"Loop #{loop_number}" if loop_number is not None else "Awareness Loop"

    fields = {
        "Read": _summarize_read(read)[:_FIELD_MAX],
        "Thinking": _thinking_brief(tool_trace)[:_FIELD_MAX],
        "Decision": _decision_brief(outcome, reason, posts)[:_FIELD_MAX],
    }

    return {
        "header": f"🧠 {_HEADER} · {label}",
        "outcome": outcome,
        "color": _COLORS.get(outcome, _COLORS["silence"]),
        "fields": fields,
        "thread_name": f"{label} · {_OUTCOME_LABEL.get(outcome, outcome)}"[:100],
        "thread_chunks": _chunk(_summarize_plan(plan)),
        "observatory_url": observatory_url(loop_number),
    }


__all__ = [
    "build_diagnostic_render",
    "read_summary",
    "format_live_event",
    "observatory_url",
]
