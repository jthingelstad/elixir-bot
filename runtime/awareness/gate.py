"""Pre-LLM cost gate for the awareness loop.

The awareness brain runs on the expensive chat model (Sonnet). Most ticks
resolve to silence — either nothing happened at all, or the only signals are
routine milestones already inside a spotlight cooldown. Paying full Sonnet
price just to conclude "stay quiet" is the dominant cost of the system.

This gate sits between the (cheap, deterministic) read and the (expensive)
brain and classifies each read into one of three tiers:

  - ``deliberate`` — a mandatory or explicit trigger is present: a hard-post
    signal (join / verified-leave / war-reset / season-close / week-finish) or
    an explicit due revisit. The full brain MUST run to compose + deliver.
    Never gated away.
  - ``triage`` — soft signals are present (routine milestones, cake days,
    standing management candidates) but nothing mandatory. A
    cheap lightweight-model (Haiku) triage makes the post-vs-silence call; only
    a ``post`` verdict escalates to the full brain to compose.
  - ``skip`` — nothing actionable at all. Deterministic silence, no LLM.

Persistent full-state fields (``cake_days_today``, ``management.actionable``)
are NOT independent *deliberate* triggers: they are
populated on every tick and would defeat the gate. They route to ``triage`` so
a genuinely new/actionable one is still caught cheaply, while stale standing
state costs at most a Haiku call, never Sonnet.

Safety property that makes this a cost change and not a quality change: **the
triage model can only gate, never post.** A ``post`` verdict merely escalates
to the same Sonnet brain that ran before; Sonnet remains the sole author and
final authority on anything that reaches a member channel. The worst a bad
triage call can do is waste one Sonnet run (false ``post``) or skip a routine
soft post (false ``silent`` — indistinguishable from the restraint the
milestone-tightening rules already enforce). Hard-posts never touch triage.

On any triage failure (API error, unparseable verdict) the gate **fails safe
to ``deliberate``** — a failed gate costs a normal Sonnet tick, never a dropped
post. Set ``ELIXIR_AWARENESS_GATE=0`` to disable the gate entirely (every tick
deliberates, pre-gate behaviour).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from engine.event_contracts import BADGE_EVENT_TYPES

log = logging.getLogger("elixir")


def gate_enabled() -> bool:
    return os.getenv("ELIXIR_AWARENESS_GATE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


# ---------------------------------------------------------------------------
# Deterministic trigger classification
# ---------------------------------------------------------------------------


def _hard_post(read: dict) -> list:
    return [s for s in (read.get("hard_post_signals") or []) if s]


def _due_revisits(read: dict) -> list:
    return [r for r in (read.get("due_revisits") or []) if r]


_CAKE_EVENT_TYPES = {"join_anniversary", "member_birthday", "cr_account_anniversary"}


def _soft_lane_signals(read: dict) -> list:
    """Lane signals that are not themselves hard-post signals.

    Hard-post events are also mirrored into ``signals_by_category``; a hard-post is
    already handled by the deliberate tier, so here we count only the soft
    remainder (milestones, non-mandatory clan/battle events, etc.). An anniversary
    already celebrated in a post today is dropped too — it also lands in the
    clan_event lane, and left in it keeps re-nudging the gate (over-escalation leak).
    """
    lanes = read.get("signals_by_category") or {}
    hard_keys = {s.get("signal_key") for s in _hard_post(read) if isinstance(s, dict)}
    posted_cake = _posted_cake_tags(read)
    out = []
    for sigs in lanes.values():
        if not isinstance(sigs, list):
            continue
        for s in sigs:
            if isinstance(s, dict) and s.get("signal_key") in hard_keys:
                continue
            if (
                isinstance(s, dict)
                and s.get("event_type") in _CAKE_EVENT_TYPES
                and s.get("subject_tag") in posted_cake
            ):
                continue
            out.append(s)
    return out


_ARENA_EVENT_TYPES = {"arena_changed", "arena_up"}


def _notable_soft_signals(read: dict) -> list:
    """Soft signals that are notable enough to always deserve the full brain —
    never gated to silence by the cheap triage. A Legendary badge (one-off) or an
    arena climb is rare and cool; route it to deliberate so the brain composes it
    per the milestone-discipline rules, rather than risk a triage mute."""
    out = []
    for s in _soft_lane_signals(read):
        if not isinstance(s, dict):
            continue
        # Since 2026-08-04 a Legendary badge has its own event type; the tier
        # check still stands so historical `badge_earned` rows (which carry every
        # tier) are classified the same way.
        if s.get("event_type") in BADGE_EVENT_TYPES and s.get("badge_tier") == "legendary":
            out.append(s)
        elif s.get("event_type") in _ARENA_EVENT_TYPES:
            out.append(s)
    return out


def _is_quiet_stretch(read: dict) -> bool:
    return bool((read.get("posting_pulse") or {}).get("is_quiet_stretch"))


def _cake_day_already_posted(read: dict, cake: dict) -> bool:
    """True if this cake day's member has already been named in a fulfilled
    awareness post on its own date. Anniversaries sit in ``cake_days_today`` for
    the whole UTC day, so once posted they keep re-triggering the gate — a cheap
    triage that then over-escalates to a full Sonnet run just to re-decide "no,
    already posted" (the cake-day over-escalation leak). Detect it deterministically
    from channel_memory instead of trusting the triage model to remember."""
    name = (cake.get("name") or "").strip().lower()
    key = cake.get("signal_key") or ""
    # the anniversary date is the last colon-field of the signal_key (tags carry
    # no colon), e.g. join_anniversary:#TAG:2026-07-14 -> 2026-07-14.
    date = key.rsplit(":", 1)[-1] if ":" in key else None
    if not name:
        return False
    for lane in (read.get("channel_memory") or {}).values():
        for post in (lane.get("recent_posts") or []) if isinstance(lane, dict) else []:
            if not post.get("posted"):
                continue
            created = post.get("posted_at") or ""
            if date and not created.startswith(date):
                continue
            if name in (post.get("preview") or "").lower():
                return True
    return False


def _posted_cake_tags(read: dict) -> set:
    """Subject tags whose cake day has already been celebrated in a post today."""
    return {
        c.get("subject_tag")
        for c in (read.get("cake_days_today") or [])
        if c and c.get("subject_tag") and _cake_day_already_posted(read, c)
    }


def _fresh_cake_days(read: dict) -> list:
    """Cake days that have NOT already been celebrated in a post today — the only
    ones that should still nudge the gate toward a look."""
    posted = _posted_cake_tags(read)
    return [
        c for c in (read.get("cake_days_today") or []) if c and c.get("subject_tag") not in posted
    ]


def _soft_context(read: dict) -> dict:
    """Standing, non-mandatory context that can justify a triage look but never
    an unconditional Sonnet run (each is populated on ~every tick)."""
    cake = _fresh_cake_days(read)
    mgmt = (read.get("management") or {}).get("actionable") or {}
    mgmt_n = sum(len(mgmt.get(k) or []) for k in ("kick", "promote", "demote"))
    return {"cake_days": cake, "management_actionable": mgmt_n}


def classify(read: dict) -> dict:
    """Deterministically classify a read into a gate tier.

    Returns ``{"tier": "deliberate"|"triage"|"skip", "reason": str,
    "triggers": {...}}``. Pure and side-effect free — the unit-testable core.
    """
    hard = _hard_post(read)
    revisits = _due_revisits(read)
    soft_lanes = _soft_lane_signals(read)
    ctx = _soft_context(read)

    triggers = {
        "hard_post": len(hard),
        "due_revisits": len(revisits),
        "soft_lane_signals": len(soft_lanes),
        **ctx,
    }

    notable = _notable_soft_signals(read)
    quiet = _is_quiet_stretch(read)
    triggers["notable_soft_signals"] = len(notable)
    triggers["quiet_stretch"] = quiet

    if hard:
        kinds = sorted({s.get("event_type") for s in hard if isinstance(s, dict)})
        return {
            "tier": "deliberate",
            "reason": f"hard-post signal(s): {', '.join(k for k in kinds if k)}",
            "triggers": triggers,
        }
    if revisits:
        return {
            "tier": "deliberate",
            "reason": f"{len(revisits)} due revisit(s)",
            "triggers": triggers,
        }
    # Notable soft signals (Legendary badge, arena climb) always reach the brain —
    # too rare/cool to risk a cheap-triage mute.
    if notable:
        kinds = sorted({s.get("event_type") for s in notable if isinstance(s, dict)})
        return {
            "tier": "deliberate",
            "reason": f"notable signal(s): {', '.join(k for k in kinds if k)}",
            "triggers": triggers,
        }
    # Heartbeat: a long quiet stretch WITH something real to say → let the brain
    # compose a warm roundup rather than flat-lining.
    if quiet and soft_lanes:
        return {
            "tier": "deliberate",
            "reason": f"quiet stretch ({len(soft_lanes)} soft signal(s) to surface)",
            "triggers": triggers,
        }

    has_soft = bool(soft_lanes) or bool(ctx["cake_days"]) or ctx["management_actionable"]
    if has_soft:
        bits = []
        if soft_lanes:
            bits.append(f"{len(soft_lanes)} soft signal(s)")
        if ctx["cake_days"]:
            bits.append(f"{len(ctx['cake_days'])} cake day(s)")
        if ctx["management_actionable"]:
            bits.append(f"{ctx['management_actionable']} mgmt candidate(s)")
        return {"tier": "triage", "reason": "; ".join(bits), "triggers": triggers}

    return {
        "tier": "skip",
        "reason": "no signals in any lane and no standing context",
        "triggers": triggers,
    }


# ---------------------------------------------------------------------------
# Lightweight (Haiku) triage of soft-signal ticks
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """You are the cost-triage step for Elixir, a Clash Royale clan agent. \
Elixir posts sparingly to member channels and strongly prefers restraint. \
A separate, more capable model composes the actual post — your ONLY job is a binary decision: \
is there anything here worth Elixir speaking up about right now, or should it stay silent this hour?

Decide SILENT when the signals are routine or already handled:
- a member's milestone that falls inside their ~72h spotlight cooldown (they were already spotlighted recently)
- anything already posted/covered today (check recent posts and spotlights)
- a lone minor card-level or small trophy bump with nothing to group it with
- standing leadership state (promote/demote/kick candidates) that is not newly actionable — leadership has its own review cadence
- an anniversary/cake day that was already posted earlier today

Decide POST only when something is genuinely fresh and worth a member seeing it now:
- a **Legendary badge** (`badge_tier: "legendary"`) or an **arena climb** (`arena_name`) — these are notable, always worth a post
- a new, uncovered milestone worth celebrating (outside cooldown), or several that form a roundup
- a not-yet-posted cake day / anniversary
- a newly-actionable item that clearly warrants a member-facing note this hour
- it's been a long quiet stretch (`posting_pulse.is_quiet_stretch`) AND there's any real signal to surface — lean POST to keep a clan heartbeat

When it is a close call between a marginal post and silence, choose SILENT. Over-posting has a real cost and Elixir values restraint. \
The composing model will make the final call and may still stay silent — a POST from you only means "worth a closer look."

Respond with ONLY a JSON object: {"decision": "post" | "silent", "reason": "<one short sentence>"}"""


def _compact_read_for_triage(read: dict) -> str:
    """A small, cost-cheap serialization of just the parts the triage needs."""

    def _slim_sig(s):
        if not isinstance(s, dict):
            return str(s)
        return {
            k: s.get(k)
            for k in (
                "event_type",
                "subject_tag",
                "observed_at",
                "signal_key",
                "badge_tier",
                "badge_label",
                "card_name",
                "arena_name",
            )
            if s.get(k)
        }

    time_block = read.get("time") or {}
    payload = {
        "now": read.get("generated_at"),
        "war_phase": (time_block.get("war_day_label") or time_block.get("phase") or None),
        "posting_pulse": read.get("posting_pulse") or {},
        "soft_signals": [_slim_sig(s) for s in _soft_lane_signals(read)],
        # only cake days not yet celebrated today — an already-posted anniversary
        # must not tempt the triage into a re-post/escalation (over-escalation leak).
        "cake_days_today": _fresh_cake_days(read),
        "recent_member_spotlights": (read.get("recent_member_spotlights") or [])[:12],
        "recent_agent_writes": (read.get("recent_agent_writes") or [])[:10],
        "channel_memory": read.get("channel_memory") or {},
    }
    return json.dumps(payload, default=str, ensure_ascii=False)


def _default_generate(system_prompt: str, user_msg: str) -> str | None:
    # Imported lazily so the gate module stays import-cheap and testable without
    # the agent stack. Routes to the lightweight (Haiku) model via the workflow.
    from agent.workflows import _generate_simple_message

    return _generate_simple_message(
        system_prompt,
        user_msg,
        workflow="awareness_triage",
        temperature=0.0,
        max_tokens=120,
        error_label="awareness_triage",
    )


def triage(read: dict, *, generate: Callable[[str, str], str | None] | None = None) -> dict:
    """Run the lightweight post-vs-silence triage. Returns
    ``{"decision": "post"|"silent", "reason": str}``.

    ``generate(system, user) -> text|None`` is injectable for tests. On any
    failure or unparseable verdict this returns ``decision="post"`` so the
    caller fails safe by escalating to the full brain (never a dropped post).
    """
    gen = generate or _default_generate
    user_msg = (
        "Here is the current clan read (compact). Decide post vs silent for this hour.\n\n"
        + _compact_read_for_triage(read)
    )
    try:
        raw = gen(_TRIAGE_SYSTEM, user_msg)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("awareness gate: triage call raised (%s); failing safe to deliberate", exc)
        return {"decision": "post", "reason": f"triage error: {exc}"}

    if not raw:
        log.warning("awareness gate: triage returned empty; failing safe to deliberate")
        return {"decision": "post", "reason": "triage empty response"}

    decision, reason = _parse_verdict(raw)
    if decision is None:
        log.warning(
            "awareness gate: triage verdict unparseable (%r); failing safe to deliberate",
            raw[:200],
        )
        return {"decision": "post", "reason": "triage unparseable"}
    return {"decision": decision, "reason": reason or ""}


def _parse_verdict(raw: str):
    """Extract ``(decision, reason)`` from the triage text. Tolerant of code
    fences / stray prose around the JSON. Returns ``(None, None)`` if no clear
    post/silent verdict is found."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            d = str(obj.get("decision", "")).strip().lower()
            if d in ("post", "silent"):
                return d, str(obj.get("reason", "")).strip()
        except ValueError, TypeError:
            pass
    low = text.lower()
    # Fallback: unambiguous single-word signal in prose.
    has_post, has_silent = "post" in low, "silent" in low
    if has_post and not has_silent:
        return "post", ""
    if has_silent and not has_post:
        return "silent", ""
    return None, None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def decide(read: dict, *, triage_fn: Callable[[dict], dict] | None = None) -> dict:
    """Decide whether the expensive brain should run for this read.

    Returns a dict:
      - ``{"deliberate": True, "tier", "reason"}`` — the caller runs the full
        Sonnet brain (``run_awareness_tick``).
      - ``{"deliberate": False, "tier", "silence_reason", "decider"}`` — the
        caller synthesizes a deterministic silence (no Sonnet call). ``decider``
        is ``"gate"`` (no triggers) or ``"triage"`` (Haiku chose silence).

    Never raises: any internal failure falls back to ``deliberate`` so a broken
    gate degrades to pre-gate behaviour rather than dropping a tick.
    """
    try:
        if not gate_enabled():
            return {
                "deliberate": True,
                "tier": "disabled",
                "reason": "gate disabled via env",
            }

        cls = classify(read)
        tier = cls["tier"]

        if tier == "deliberate":
            return {"deliberate": True, "tier": "deliberate", "reason": cls["reason"]}

        if tier == "skip":
            return {
                "deliberate": False,
                "tier": "skip",
                "decider": "gate",
                "silence_reason": "Gate: "
                + cls["reason"]
                + " — deterministic silence, no deliberation needed.",
            }

        # tier == "triage": cheap model makes the post-vs-silence call.
        verdict = (triage_fn or triage)(read)
        if verdict.get("decision") == "silent":
            return {
                "deliberate": False,
                "tier": "triage",
                "decider": "triage",
                "silence_reason": "Triage (lightweight): "
                + (verdict.get("reason") or "nothing worth posting this hour")
                + f" [soft triggers: {cls['reason']}]",
            }
        # "post" (or fail-safe): escalate to the full brain to compose.
        return {
            "deliberate": True,
            "tier": "triage->deliberate",
            "reason": "triage escalated: "
            + (verdict.get("reason") or "worth a closer look")
            + f" [{cls['reason']}]",
        }
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("awareness gate: decide failed (%s); failing safe to deliberate", exc)
        return {"deliberate": True, "tier": "error", "reason": f"gate error: {exc}"}


__all__ = ["decide", "classify", "triage", "gate_enabled"]
