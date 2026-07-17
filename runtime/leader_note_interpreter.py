"""Async interpretation of a leader's free-text note on an #actions card.

The note is captured deterministically at submit time (no LLM in that path — a
prior inline "editor gate" was retired for holding the delivery write lock). This
module runs the interpretation OFF that transaction: a lightweight classifier
reads the note, maps it to one structured effect, the effect is applied
deterministically, and the card is refreshed to echo the reading back with
Undo / Fix-reading controls.

Fire-on-submit (mirrors ``runtime.leader_action_feedback``): the modal handlers
call ``queue_leader_note_interpretation(action_id, bot=...)`` right after they
persist the note. Gated by ``ELIXIR_NOTE_INTERPRET`` so it can be dark-launched.
"""

from __future__ import annotations

import asyncio
import logging
import os

import db

log = logging.getLogger("elixir.leader_note_interpreter")

# Behaviour-changing effects (premise rejection, durable member context) can hide
# a member who should be actioned, so they require a confident classification.
_MIN_CONFIDENCE = 0.6
_GUARDED_KINDS = ("invalidate_premise", "persist_context")


def _enabled() -> bool:
    return os.getenv("ELIXIR_NOTE_INTERPRET", "0").strip() == "1"


def _confident(effect: dict, kind: str) -> bool:
    if kind not in _GUARDED_KINDS:
        return True
    try:
        return float(effect.get("confidence") or 0.0) >= _MIN_CONFIDENCE
    except TypeError, ValueError:
        return False


def _interpret_and_persist(action_id: int):
    """Sync worker (runs in a thread): classify the note, apply the effect, and
    persist the interpretation. Returns the updated action dict for the card
    refresh, or None when there is nothing to do."""
    action = db.get_leader_action_by_id(action_id)
    if not action:
        return None
    note = action.get("decision_note")
    note_hash = db.note_text_hash(note)
    if not note_hash:
        return None  # note was cleared before we ran — nothing to interpret

    context = {
        "note": note,
        "card_type": action.get("action_type"),
        "card_status": action.get("status"),
        "member": action.get("target_player_name"),
        "recommendation": action.get("prompt_text"),
        "rationale": action.get("rationale"),
    }
    try:
        import elixir_agent

        result = elixir_agent.interpret_leader_note(context)
    except Exception:
        log.warning(
            "leader note interpretation call failed action_id=%s",
            action_id,
            exc_info=True,
        )
        result = None

    if not isinstance(result, dict) or result.get("_error"):
        return db.record_note_interpretation(action_id, status="failed", note_hash=note_hash)

    effect = result.get("effect") if isinstance(result.get("effect"), dict) else {}
    kind = (effect.get("kind") or "none").strip()

    from engine.leader_note_effects import apply_leader_note_effect

    to_apply = effect if _confident(effect, kind) else {"kind": "none"}
    applied = apply_leader_note_effect(action_id, to_apply)

    stored = {
        "kind": applied.get("kind"),
        "requested_kind": kind,
        "applied": bool(applied.get("applied")),
        "reading": applied.get("reading") or result.get("reading"),
        "hold_days": effect.get("hold_days"),
        "context_kind": applied.get("context_kind") or effect.get("context_kind"),
        "confidence": effect.get("confidence"),
        "prior": applied.get("prior"),
        "rationale": result.get("rationale"),
    }
    return db.record_note_interpretation(
        action_id, status="interpreted", effect=stored, note_hash=note_hash
    )


async def _interpret_async(action_id: int, bot) -> None:
    try:
        updated = await asyncio.to_thread(_interpret_and_persist, action_id)
    except Exception:
        log.warning("leader note interpretation failed action_id=%s", action_id, exc_info=True)
        return
    if updated is None or bot is None:
        return
    try:
        from runtime.leader_action_ui import refresh_leader_action_card

        await refresh_leader_action_card(bot, updated)
    except Exception:
        log.warning("leader note card refresh failed action_id=%s", action_id, exc_info=True)


def undo_note_interpretation(action_id: int):
    """Reverse the applied effect on a card (the Undo button) and mark it undone.
    Sync — call in a thread. Returns the updated action for the card refresh."""
    action = db.get_leader_action_by_id(action_id)
    if not action:
        return None
    stored = action.get("note_interpret") if isinstance(action.get("note_interpret"), dict) else {}
    try:
        from engine.leader_note_effects import revert_leader_note_effect

        revert_leader_note_effect(action_id, stored)
    except Exception:
        log.warning("revert during undo failed action_id=%s", action_id, exc_info=True)
    reverted = {**(stored or {}), "applied": False, "reading": "(undone)"}
    return db.record_note_interpretation(action_id, status="undone", effect=reverted)


def revert_and_reset_note(action_id: int, note: str, discord_user_id):
    """Fix-reading: reverse the current effect, replace the note text, and flag it
    for fresh interpretation. Sync — call in a thread. Returns the updated action."""
    action = db.get_leader_action_by_id(action_id)
    if action:
        try:
            from engine.leader_note_effects import revert_leader_note_effect

            revert_leader_note_effect(
                action_id,
                action.get("note_interpret")
                if isinstance(action.get("note_interpret"), dict)
                else {},
            )
        except Exception:
            log.warning("revert during fix failed action_id=%s", action_id, exc_info=True)
    return db.set_leader_action_note_text(action_id, note=note, discord_user_id=discord_user_id)


def queue_leader_note_interpretation(action_id: int | None, *, bot=None):
    """Fire-and-forget interpretation of the note on ``action_id``. No-op when the
    feature is disabled. In an async context it schedules a task (interpretation
    runs in a thread, the card refresh back on the loop); with no running loop
    (sync callers / tests) it runs synchronously without a card refresh."""
    if action_id is None or not _enabled():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _interpret_and_persist(int(action_id))
    return loop.create_task(_interpret_async(int(action_id), bot))


__all__ = [
    "queue_leader_note_interpretation",
    "revert_and_reset_note",
    "undo_note_interpretation",
]
