"""Apply (and reverse) the structured effect an interpreted leader note carries.

A leader's free-text on an #actions card is captured deterministically, then
interpreted off the delivery transaction into exactly one effect:

- ``timing_hold`` — hold re-nomination for N days (writes ``expires_at``).
- ``invalidate_premise`` — the leader rejected the recommendation's premise; pin
  the current evidence fingerprint so the engine won't re-raise the same card on
  the same evidence (see ``engine.management._premise_still_rejected``).
- ``persist_context`` — a durable protective fact (alt / never-flag / LOA) that
  shields the member from future flags and feeds the awareness brain.
- ``none`` — annotation only; nothing to apply.

Every ``apply`` returns a result carrying a ``prior`` snapshot so ``revert`` (the
card's Undo button) restores the exact prior state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db
import memory_store
from engine.management import compute_premise_fingerprint

log = logging.getLogger("engine.leader_note_effects")

_ACTOR = "leader_note_interpret"


def _coerce_days(value) -> int:
    try:
        return max(0, int(round(float(value))))
    except TypeError, ValueError:
        return 0


def _utc_in_days(days: int) -> str:
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    return (base + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _hold_reading(days: int) -> str:
    if days % 30 == 0 and days >= 30:
        months = days // 30
        return f"hold {months} month{'s' if months > 1 else ''}"
    if days % 7 == 0 and days >= 7:
        weeks = days // 7
        return f"hold {weeks} week{'s' if weeks > 1 else ''}"
    return f"hold {days} day{'s' if days != 1 else ''}"


def _premise_reading(action_type: str) -> str:
    subject = {
        "kick_recommendation": "won't re-raise the kick on the same evidence",
        "promotion_recommendation": "won't re-raise the promotion until the role changes",
        "demotion_recommendation": "won't re-raise the demotion until the role changes",
    }.get(action_type, "won't re-raise on the same evidence")
    return f"premise rejected — {subject}"


def _context_reading(ctx_kind: str, name: str) -> str:
    return {
        "alt": f"logged {name} as an alt account — shielded from flags",
        "never_flag": f"{name} will not be flagged",
        "loa": f"{name} on leave — shielded until it lapses",
    }.get(ctx_kind, f"recorded leader context on {name}")


def apply_leader_note_effect(action_id: int, effect: dict | None) -> dict:
    """Apply one interpreted effect to (the card / the member). Returns a result
    dict: ``kind``, ``applied``, a human ``reading`` for the card echo, and a
    ``prior`` snapshot for Undo. Never raises — a bad/thin effect is a no-op."""
    action = db.get_leader_action_by_id(action_id)
    if not action:
        return {"kind": "none", "applied": False, "reading": "card not found"}
    kind = (effect or {}).get("kind") or "none"
    action_type = action.get("action_type") or ""
    raw_tag = action.get("target_player_tag")
    canon = db._canon_tag(raw_tag) if raw_tag else None
    name = action.get("target_player_name") or canon or "the member"

    try:
        if kind == "timing_hold":
            days = _coerce_days((effect or {}).get("hold_days"))
            if days <= 0:
                return {"kind": "none", "applied": False, "reading": "no hold duration"}
            prior = db.set_leader_action_suppression(action_id, _utc_in_days(days))
            return {
                "kind": "timing_hold",
                "applied": True,
                "hold_days": days,
                "reading": _hold_reading(days),
                "prior": {"expires_at": prior},
            }

        if kind == "invalidate_premise":
            if not canon or not action_type:
                return {"kind": "none", "applied": False, "reading": "no target member"}
            fingerprint = compute_premise_fingerprint(canon, action_type)
            prior = db.set_leader_action_premise(action_id, rejected=True, fingerprint=fingerprint)
            return {
                "kind": "invalidate_premise",
                "applied": True,
                "reading": _premise_reading(action_type),
                "prior": {"premise": prior},
            }

        if kind == "persist_context":
            if not canon:
                return {"kind": "none", "applied": False, "reading": "no target member"}
            ctx_kind = ((effect or {}).get("context_kind") or "note").strip().lower()
            fact = ((effect or {}).get("context_fact") or "").strip()
            tags: list[str] = []
            expires_at = None
            if ctx_kind == "alt":
                title = f"Alt account: {name}"
                tags = ["shield:alt"]
            elif ctx_kind == "never_flag":
                title = f"Never flag: {name}"
                tags = ["shield:never_flag"]
            elif ctx_kind == "loa":
                title = f"LOA: {name}"
                tags = ["loa"]
                days = _coerce_days((effect or {}).get("hold_days"))
                if days > 0:
                    expires_at = _utc_in_days(days)
            else:
                ctx_kind = "note"
                title = f"Leader note: {name}"
            body = f"{name}: {fact}" if fact else f"{name}: leader-recorded context."
            memory = memory_store.create_memory(
                body=body,
                source_type="leader_note",
                is_inference=False,
                confidence=1.0,
                created_by=_ACTOR,
                scope="leadership",
                title=title,
                member_tag=canon,
                expires_at=expires_at,
            )
            memory_id = memory.get("memory_id") if isinstance(memory, dict) else None
            if tags and memory_id:
                memory_store.attach_tags(memory_id, tags, actor=_ACTOR)
            return {
                "kind": "persist_context",
                "applied": True,
                "context_kind": ctx_kind,
                "reading": _context_reading(ctx_kind, name),
                "prior": {"memory_id": memory_id},
            }
    except Exception:
        log.warning(
            "apply_leader_note_effect failed action_id=%s kind=%s",
            action_id,
            kind,
            exc_info=True,
        )
        return {"kind": "none", "applied": False, "reading": "could not apply"}

    return {"kind": "none", "applied": False, "reading": "noted (no action taken)"}


def revert_leader_note_effect(action_id: int, stored: dict | None) -> None:
    """Reverse a previously-applied effect to its snapshotted prior state
    (the card's Undo button). Fail-open."""
    kind = (stored or {}).get("kind")
    prior = (stored or {}).get("prior") or {}
    try:
        if kind == "timing_hold":
            db.set_leader_action_suppression(action_id, prior.get("expires_at"))
        elif kind == "invalidate_premise":
            snap = prior.get("premise") or {}
            db.set_leader_action_premise(
                action_id,
                rejected=bool(snap.get("premise_rejected")),
                fingerprint=snap.get("premise_fingerprint"),
            )
        elif kind == "persist_context":
            memory_id = prior.get("memory_id")
            if memory_id:
                memory_store.archive_memory(memory_id, actor=_ACTOR)
    except Exception:
        log.warning(
            "revert_leader_note_effect failed action_id=%s kind=%s",
            action_id,
            kind,
            exc_info=True,
        )
