"""The awareness loop — the heartbeat + deliberation.

``run_awareness_loop`` is the central deliberative turn: build the read, hand
it to the brain (``run_awareness_tick``), persist the train of thought, deliver
the plan's posts to the member-facing channels, and render a diagnostic to the
leader-only #thinking channel. The brain is the clan's sole proactive poster.

It never raises out of the scheduled path: every failure is logged and folded
into the returned counters so a bad tick can't crash the scheduler thread.
"""

from __future__ import annotations

import logging

log = logging.getLogger("elixir")


def run_awareness_loop(*, progress_fn=None, deliver_fn=None) -> dict:
    """Run one awareness loop turn. Returns counters describing the outcome.

    The brain is the clan's sole proactive poster: it runs with its full read +
    write tool surface, and when ``deliver_fn`` is supplied its plan is posted to
    the member-facing channels.

    ``deliver_fn(read, plan) -> dict`` sends the plan's posts and returns
    ``{"delivered", "failed", "reason", ...}``. A ``failed`` delivery downgrades
    the tick to ``failed`` so the cursor doesn't advance and the signals
    re-surface next loop (fail-hard, catch-up). When ``deliver_fn`` is absent
    (CLI / tests) the brain still deliberates and the thought is persisted, but
    no posting happens.

    ``progress_fn`` (optional) streams the tick's train of thought to Discord as
    it happens — a ``start`` event opens the #thinking thread, ``tool`` /
    ``truncation`` / ``retry`` events append live, and an ``end`` event finalizes
    it with the outcome. It runs in this worker thread and must marshal to the
    bot's event loop itself (see runtime/app.py). When absent (CLI / tests), the
    tick still runs and the persisted thought remains the durable record.
    """
    from agent.workflows import run_awareness_tick
    from runtime.awareness import diagnostic as diag_mod
    from runtime.awareness import gate as gate_mod
    from runtime.awareness import policy as policy_mod
    from runtime.awareness import read as read_mod
    from runtime.awareness import store

    counters = {
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

    # Cost gate: decide whether this tick is worth the expensive brain BEFORE we
    # spend it. A gated silence (no signals, or a lightweight-triage "stay quiet"
    # verdict) synthesizes a deliberate-silence plan and skips the Sonnet call
    # entirely — the read already carries everything needed to record the
    # silence. Hard-posts and triage "post" verdicts fall through to the brain.
    # The gate never raises (fails safe to deliberate); see runtime/awareness/gate.py.
    gate_decision = gate_mod.decide(read)
    counters["gate_tier"] = gate_decision.get("tier")

    # Open the live #thinking thread with the read before the brain deliberates,
    # so tool calls + any truncation stream in as they happen.
    _progress({"type": "start", "read_summary": diag_mod.read_summary(read)})

    persist_model = None
    if not gate_decision.get("deliberate"):
        # Deterministic / triage silence — no brain call this tick.
        plan = {
            "posts": [],
            "skipped_reason": gate_decision.get("silence_reason") or "gated silence",
        }
        tool_trace: list = []
        persist_model = f"gate:{gate_decision.get('decider') or gate_decision.get('tier')}"
        log.info(
            "awareness gate: %s silence (tier=%s) — skipped brain: %s",
            gate_decision.get("decider") or "gate",
            gate_decision.get("tier"),
            gate_decision.get("silence_reason"),
        )
    else:
        if gate_decision.get("tier") not in ("deliberate", "disabled"):
            log.info(
                "awareness gate: escalating to brain (tier=%s): %s",
                gate_decision.get("tier"),
                gate_decision.get("reason"),
            )
        # tool_stats is mutated in place by the tool-calling loop; we read the
        # per-call trace back out for the log and the persisted thought. on_event
        # streams each tool call / truncation / retry into the open thread live.
        tool_stats: dict = {}
        try:
            plan = run_awareness_tick(read, tool_stats=tool_stats, on_event=_progress) or {}
        except Exception as exc:
            log.exception("awareness loop: run_awareness_tick failed")
            counters["error"] = f"run_awareness_tick: {exc}"
            plan = {}
            # Still persist the thought so the degraded turn is visible.

        tool_trace = tool_stats.get("tool_trace") or []
    counters["tool_calls"] = len(tool_trace)

    if not isinstance(plan, dict):
        plan = {}

    plan, suppressed = policy_mod.apply_editorial_admission(read, plan)
    counters["posts_suppressed"] = len(suppressed)

    outcome, reason = store.classify_plan(plan)
    counters["posts_planned"] = len(plan.get("posts") or [])

    # Live delivery: post the plan to Discord. A failed delivery (unroutable
    # channel, send error, uncovered hard-post floor) downgrades the tick to
    # failed — we mark the plan so classify + persist agree, the cursor
    # (store.last_tick_at) doesn't advance, and the signals re-surface next
    # loop. Fail-hard, no fallback.
    delivery_intent_keys: list[str] = []
    if outcome in {"posted", "silence"} and deliver_fn is not None:
        try:
            result = deliver_fn(read, plan) or {}
        except Exception as exc:
            log.exception("awareness loop: deliver_fn raised")
            result = {"failed": True, "reason": f"deliver_fn raised: {exc}"}
        counters["posts_delivered"] = result.get("delivered", 0)
        counters["posts_replayed"] = result.get("replayed", 0)
        delivery_intent_keys = [str(value) for value in (result.get("intent_keys") or []) if value]
        if result.get("failed"):
            plan["_error"] = {
                "kind": "delivery",
                "phase": "post",
                "detail": result.get("reason"),
            }
            outcome, reason = store.classify_plan(plan)
            counters["error"] = counters["error"] or f"delivery_failed: {result.get('reason')}"
        else:
            # Draining a prior pending outbox item can turn this turn's fresh
            # editorial silence into a real delivered plan.
            outcome, reason = store.classify_plan(plan)

    counters["chose_silence"] = outcome == "silence"
    counters["tick_failed"] = outcome == "failed"
    if outcome == "failed":
        # A failed tick was already logged with its stack (timeout/schema/etc.)
        # or is a silent None; record it so a bad tick isn't read as silence.
        counters["error"] = counters["error"] or f"tick_failed: {reason}"

    # QA H22: a revisit's job is to resurface a signal ONCE at due_at. Nothing
    # marked them done, so due revisits nagged the read every tick forever. On a
    # non-failed tick the brain HAS now seen them — mark them revisited so
    # they clear (if the brain wants another look it schedules a fresh revisit).
    if outcome != "failed":
        surfaced = [
            r.get("signal_key") for r in (read.get("due_revisits") or []) if r.get("signal_key")
        ]
        if surfaced:
            try:
                from storage import revisits

                counters["revisits_cleared"] = revisits.mark_revisited(surfaced)
            except Exception:
                log.warning("awareness loop: mark_revisited failed", exc_info=True)

    loop_number = None
    try:
        # A deliberate silence has fully disposed of the batch. A posted plan
        # consumes it only when live delivery was actually available and
        # succeeded; CLI/test deliberation without a deliver_fn must not make
        # member-facing signals disappear. Thought + cursor movement commit in
        # one store transaction, so a crash cannot acknowledge an unrecorded turn.
        cursor_positions = None
        if outcome == "silence" or (outcome == "posted" and deliver_fn is not None):
            cursor_positions = read.get("_event_cursor_checkpoints") or None
        rec = store.persist_thought(
            read,
            plan,
            model=persist_model,
            tool_trace=tool_trace,
            cursor_positions=cursor_positions,
        )
        counters["thought_id"] = rec["thought_id"]
        counters["loop_number"] = loop_number = rec["loop_number"]
        counters["event_cursors_advanced"] = bool(cursor_positions)
        if deliver_fn is not None:
            try:
                counters["post_receipts_linked"] = store.attach_awareness_posts_to_loop(
                    loop_number,
                    intent_keys=delivery_intent_keys,
                )
            except Exception as exc:
                log.warning("awareness loop: post receipt linking failed", exc_info=True)
                counters["error"] = counters["error"] or f"receipt_link: {exc}"
    except Exception as exc:
        log.exception("awareness loop: persist_thought failed")
        counters["error"] = counters["error"] or f"persist_thought: {exc}"

    # Finalize the #thinking diagnostic — the observability record of what the
    # brain saw, decided, and posted this tick. Rendering here never affects the
    # member-facing outcome.
    try:
        render = diag_mod.build_diagnostic_render(
            read, plan, tool_trace=tool_trace, loop_number=loop_number
        )
        # Finalize the live thread: outcome + verbatim decision, and stamp
        # the real loop number on the header/thread now that it's known.
        _progress({"type": "end", "render": render, "loop_number": loop_number})
        if progress_fn is None:
            log.info(
                "awareness loop: no progress_fn; #thinking not delivered (loop #%s, %s)",
                loop_number,
                render.get("outcome"),
            )
    except Exception as exc:
        log.exception("awareness loop: thinking render/finalize failed")
        counters["error"] = counters["error"] or f"thinking: {exc}"

    return counters


__all__ = ["run_awareness_loop"]
