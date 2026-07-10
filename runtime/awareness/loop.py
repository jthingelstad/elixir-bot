"""The awareness loop — the heartbeat + deliberation.

``run_awareness_loop`` is the central deliberative turn: build the read, hand
it to the benched brain (``run_awareness_tick``), persist the train of thought,
and — in shadow mode — render a diagnostic to the #thinking channel via the
caller-supplied ``post_fn``. It posts NOTHING to any member-facing channel.

It never raises out of the scheduled path: every failure is logged and folded
into the returned counters so a bad tick can't crash the scheduler thread.
"""

from __future__ import annotations

import logging

log = logging.getLogger("elixir")


def run_awareness_loop(*, shadow: bool = True, progress_fn=None, deliver_fn=None) -> dict:
    """Run one awareness loop turn. Returns counters describing the outcome.

    ``shadow`` gates member-facing posting. When True (default) the brain runs
    and the only side effects are the persisted thought and the #thinking
    diagnostic — nothing member-facing. When False, ``deliver_fn`` is invoked
    with the plan to actually post to Discord.

    ``deliver_fn(read, plan, loop_number) -> dict`` (live only) sends the plan's
    posts and returns ``{"delivered", "failed", "reason", ...}``. A ``failed``
    delivery downgrades the tick to ``failed`` so the cursor doesn't advance and
    the signals re-surface next loop (fail-hard, catch-up). Absent/shadow, no
    posting happens.

    ``progress_fn`` (optional) streams the tick's train of thought to Discord as
    it happens — a ``start`` event opens the #thinking thread, ``tool`` /
    ``truncation`` / ``retry`` events append live, and an ``end`` event finalizes
    it with the outcome. It runs in this worker thread and must marshal to the
    bot's event loop itself (see runtime/app.py). When absent (CLI / tests), the
    tick still runs and the persisted thought remains the durable record.
    """
    from agent.workflows import run_awareness_tick
    from runtime.awareness import read as read_mod
    from runtime.awareness import shadow as shadow_mod
    from runtime.awareness import store

    counters = {
        "shadow": shadow,
        "posts_planned": 0,
        "chose_silence": False,
        "tick_failed": False,
        "degraded_blocks": 0,
        "error": None,
    }

    try:
        read = read_mod.build_read()
    except Exception as exc:
        log.exception("awareness loop: build_read failed")
        counters["error"] = f"build_read: {exc}"
        return counters

    counters["degraded_blocks"] = len(read.get("_degraded") or [])

    def _progress(event):
        if progress_fn is None:
            return
        try:
            progress_fn(event)
        except Exception:
            log.debug("awareness loop: progress observer raised (ignored)", exc_info=True)

    # Open the live #thinking thread with the read before the brain deliberates,
    # so tool calls + any truncation stream in as they happen.
    _progress({"type": "start", "read_summary": shadow_mod.read_summary(read)})

    # tool_stats is mutated in place by the tool-calling loop; we read the
    # per-call trace back out for the log and the persisted thought. on_event
    # streams each tool call / truncation / retry into the open thread live.
    tool_stats: dict = {}
    try:
        plan = run_awareness_tick(
            read, shadow=True, tool_stats=tool_stats, on_event=_progress
        ) or {}
    except Exception as exc:
        log.exception("awareness loop: run_awareness_tick failed")
        counters["error"] = f"run_awareness_tick: {exc}"
        plan = {}
        # Still persist the thought so the degraded turn is visible.

    tool_trace = tool_stats.get("tool_trace") or []
    counters["tool_calls"] = len(tool_trace)

    if not isinstance(plan, dict):
        plan = {}

    outcome, reason = store.classify_plan(plan)
    counters["posts_planned"] = len(plan.get("posts") or [])

    # Live delivery: post the plan to Discord. Shadow keeps posting nothing.
    # A failed delivery (unroutable channel, send error, uncovered hard-post
    # floor) downgrades the tick to failed — we mark the plan so classify +
    # persist agree, the cursor (store.last_tick_at) doesn't advance, and the
    # signals re-surface next loop. Fail-hard, no fallback.
    if not shadow and outcome == "posted" and deliver_fn is not None:
        try:
            result = deliver_fn(read, plan) or {}
        except Exception as exc:
            log.exception("awareness loop: deliver_fn raised")
            result = {"failed": True, "reason": f"deliver_fn raised: {exc}"}
        counters["posts_delivered"] = result.get("delivered", 0)
        if result.get("failed"):
            plan["_error"] = {
                "kind": "delivery",
                "phase": "post",
                "detail": result.get("reason"),
            }
            outcome, reason = store.classify_plan(plan)
            counters["error"] = (
                counters["error"] or f"delivery_failed: {result.get('reason')}"
            )

    counters["chose_silence"] = outcome == "silence"
    counters["tick_failed"] = outcome == "failed"
    if outcome == "failed":
        # A failed tick was already logged with its stack (timeout/schema/etc.)
        # or is a silent None; record it so a bad tick isn't read as silence.
        counters["error"] = counters["error"] or f"tick_failed: {reason}"

    loop_number = None
    try:
        rec = store.persist_thought(read, plan, shadow=shadow, tool_trace=tool_trace)
        counters["thought_id"] = rec["thought_id"]
        counters["loop_number"] = loop_number = rec["loop_number"]
    except Exception as exc:
        log.exception("awareness loop: persist_thought failed")
        counters["error"] = counters["error"] or f"persist_thought: {exc}"

    # Finalize the #thinking diagnostic in BOTH modes — it's the observability
    # record of what the brain saw and decided (in live mode it additionally
    # posted). Rendering here never affects the member-facing outcome.
    try:
        render = shadow_mod.build_shadow_render(
            read, plan, tool_trace=tool_trace, loop_number=loop_number, shadow=shadow
        )
        # Finalize the live thread: outcome + verbatim decision, and stamp
        # the real loop number on the header/thread now that it's known.
        _progress({"type": "end", "render": render, "loop_number": loop_number})
        if progress_fn is None:
            log.info("awareness loop: no progress_fn; #thinking not delivered "
                     "(loop #%s, %s)", loop_number, render.get("outcome"))
    except Exception as exc:
        log.exception("awareness loop: thinking render/finalize failed")
        counters["error"] = counters["error"] or f"thinking: {exc}"

    return counters


__all__ = ["run_awareness_loop"]
