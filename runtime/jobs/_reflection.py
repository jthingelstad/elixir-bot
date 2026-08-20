"""Nightly reflection: a day of Elixir's own output becomes lessons.

Agentic Loop v2, Phase 4 (docs/plans/agentic-loop.md). Every other feeder in
``engine/editor.py`` fires on an unambiguous human act — an admin deleted a post,
a leader rewrote copy. This one reads a whole day at once, because the signals it
is after are only visible in aggregate: three welcomes that opened the same way,
a reaction removed after a correction, a wake that chose silence four times for
the same reason.

**Three properties this job must keep.**

1. **Lessons are capped, hard, in code.** The chassis injects at most 12 and a
   model asked for lessons will always find some. Three a night is the ceiling
   here, and it is enforced after the model returns rather than requested in the
   prompt, because a request is a suggestion.
2. **Every lesson carries its evidence.** A lesson reaches every chassis turn
   from the moment it is written. One that cannot name the post it came from is
   an unfalsifiable instruction with a permanent audience.
3. **It proposes; Jamie disposes.** Lessons are visible, evidence-linked, and
   removable with one delete. Nothing here changes wake policy, cadence, or what
   Elixir posts about — only how it writes.
"""

from __future__ import annotations

__all__ = ["MAX_LESSONS_PER_NIGHT", "_reflection_cycle", "build_reflection_context"]

import asyncio
import json
import logging

import db
import elixir_agent
from runtime import status as runtime_status

log = logging.getLogger("elixir")

# Enforced in code, after the model answers. See property 1 above.
MAX_LESSONS_PER_NIGHT = 3

# A lesson the model is unsure of still reaches every turn, so the bar to be
# written down at all is higher than "possible".
MIN_LESSON_CONFIDENCE = 0.5


def build_reflection_context(conn, hours: int = 24) -> dict:
    """The day, as evidence. Reads only; decides nothing."""
    from agent import chassis
    from engine import editor

    intents = conn.execute(
        "SELECT intent_key, lane, content, covers_json, fulfilled_at "
        "FROM awareness_delivery_intents "
        "WHERE status = 'fulfilled' AND datetime(fulfilled_at) >= datetime('now', ?) "
        "ORDER BY fulfilled_at",
        (f"-{int(hours)} hours",),
    ).fetchall()

    # A wake that produced nothing is a decision, and the reason it recorded is
    # often the more interesting half of the day.
    silences = conn.execute(
        "SELECT thought_id, at, skipped_reason, post_count FROM awareness_thoughts "
        "WHERE chose_silence = 1 AND datetime(at) >= datetime('now', ?) "
        "ORDER BY at",
        (f"-{int(hours)} hours",),
    ).fetchall()

    # Only member-authored Ask Elixir/deck-review turns are dossier evidence.
    # A roster statistic is not something a person told us, and a clanops turn
    # can mention another person while being linked to the leader who spoke.
    conversations = conn.execute(
        "SELECT message_id, member_id, workflow, content, created_at FROM messages "
        "WHERE author_type = 'user' AND member_id IS NOT NULL "
        "AND workflow IN ('interactive', 'deck_review') "
        "AND datetime(created_at) >= datetime('now', ?) "
        "ORDER BY created_at, message_id LIMIT 100",
        (f"-{int(hours)} hours",),
    ).fetchall()

    reactions = editor.recent_reaction_feeders(conn, hours=hours)
    intent_rows = [
        {
            "evidence_ref": f"intent:{r['intent_key']}",
            "intent_key": r["intent_key"],
            "lane": r["lane"],
            "posted_at": r["fulfilled_at"],
            "covers": json.loads(r["covers_json"] or "[]"),
            "content": (r["content"] or "")[:900],
        }
        for r in intents
    ]
    silence_rows = [
        {
            "evidence_ref": f"silence:{r['thought_id']}",
            "at": r["at"],
            "reason": (r["skipped_reason"] or "")[:300],
        }
        for r in silences
    ]
    conversation_rows = [
        {
            "evidence_ref": f"message:{r['message_id']}",
            "member_tag": r["member_id"],
            "workflow": r["workflow"],
            "at": r["created_at"],
            "content": (r["content"] or "")[:900],
        }
        for r in conversations
    ]
    evidence_index = [
        {
            "ref": item["evidence_ref"],
            **({"member_tag": item["member_tag"]} if item.get("member_tag") else {}),
        }
        for item in [*intent_rows, *silence_rows, *reactions, *conversation_rows]
    ]

    return {
        "window_hours": int(hours),
        "intents": intent_rows,
        "silences": silence_rows,
        "reactions": reactions,
        "member_conversations": conversation_rows,
        "evidence_index": evidence_index,
        "current_lessons": chassis._editorial_lessons(),
    }


def _valid_refs(entries: list[dict], valid_evidence_refs: set[str]) -> list[str]:
    refs = entries if isinstance(entries, list) else []
    return sorted(
        {
            str(ref).strip()
            for ref in refs
            if str(ref).strip() and str(ref).strip() in valid_evidence_refs
        }
    )


def _persist_lessons(
    conn,
    lessons: list[dict],
    notes: str,
    *,
    valid_evidence_refs: set[str] = frozenset(),
) -> list[int]:
    """Write the accepted lessons as editorial memories. Returns memory ids."""
    from engine import editor

    written: list[int] = []
    for lesson in lessons:
        if not isinstance(lesson, dict):
            continue
        title = str(lesson.get("title") or "").strip()
        body = str(lesson.get("body") or "").strip()
        evidence_refs = _valid_refs(lesson.get("evidence_refs"), valid_evidence_refs)
        # Property 2, enforced rather than requested. A lesson with no evidence
        # is dropped, not downgraded — there is nothing to review it against
        # later, and it would be injected into every turn regardless.
        if not title or not body or not evidence_refs:
            log.info("reflection: dropped a lesson with no title/body/evidence")
            continue
        try:
            confidence = float(lesson.get("confidence") or 0)
        except TypeError, ValueError:
            confidence = 0.0
        if confidence < MIN_LESSON_CONFIDENCE:
            log.info("reflection: dropped low-confidence lesson %r (%.2f)", title, confidence)
            continue
        memory_id = editor._add_editorial_memory(
            conn,
            title=title[:200],
            body=f"{body}\n\nEVIDENCE: {', '.join(evidence_refs)[:400]}",
            kind_tag="lesson",
            # Deduped on the evidence, not the wording: the same reaction
            # re-read tomorrow must not become a second copy of the same rule.
            event_key=f"reflection_lesson:{_evidence_key(evidence_refs)}",
            confidence=min(1.0, max(0.0, confidence)),
            created_by="reflection",
            extra_tags=("nightly",),
        )
        if memory_id:
            written.append(memory_id)
        if len(written) >= MAX_LESSONS_PER_NIGHT:
            break
    if notes:
        log.info("reflection notes: %s", notes[:800])
    conn.commit()
    return written


def _persist_dossiers(
    conn,
    dossiers: list[dict],
    *,
    evidence_members: dict[str, str] | None = None,
) -> int:
    """Write member dossiers. Returns how many were written.

    Only for members who actually exist: a dossier keyed on a tag the roster has
    never seen is either a hallucinated player or a typo, and the FK would raise
    into the job either way.
    """
    from storage.dossiers import upsert_dossier

    written = 0
    evidence_members = evidence_members or {}
    for entry in dossiers:
        if not isinstance(entry, dict):
            continue
        tag = str(entry.get("member_tag") or "").strip()
        body = str(entry.get("body") or "").strip()
        refs = _valid_refs(entry.get("evidence_refs"), set(evidence_members))
        # A dossier about a person must cite something that person said in a
        # linked conversation. Merely citing a valid row about someone else is
        # still an invented dossier.
        if not tag or not body or not refs or not any(evidence_members[r] == tag for r in refs):
            log.info("reflection: dropped a dossier without matching conversation evidence")
            continue
        known = conn.execute("SELECT 1 FROM players WHERE player_tag = ?", (tag,)).fetchone()
        if not known:
            log.info("reflection: dropped a dossier for unknown member %s", tag)
            continue
        if upsert_dossier(
            tag,
            body,
            updated_by="reflection",
            source_intent_key=refs[0],
            conn=conn,
        ):
            written += 1
    conn.commit()
    return written


def _evidence_key(evidence_refs: list[str]) -> str:
    import hashlib

    canonical = "\n".join(sorted(evidence_refs)).lower()
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


async def _reflection_cycle():
    """Nightly. Reads the day, writes at most three lessons, never posts."""
    from runtime.prompt_feedback import reflection_enabled

    if not reflection_enabled():
        log.info("reflection: disabled (ELIXIR_REFLECTION), skipping")
        return
    runtime_status.mark_job_start("reflection")

    def _read():
        conn = db.get_connection()
        try:
            return build_reflection_context(conn)
        finally:
            conn.close()

    try:
        context = await asyncio.to_thread(_read)
    except Exception as exc:
        log.error("reflection: context build failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("reflection", f"context build failed: {exc}")
        return

    # Nothing happened is a normal night, and paying for a model call to be told
    # so is not.
    if (
        not context.get("intents")
        and not context.get("reactions")
        and not context.get("member_conversations")
    ):
        log.info("reflection: no posts or reactions in the window; nothing to reflect on")
        runtime_status.mark_job_success("reflection", "quiet day, no call made")
        return

    try:
        plan = await asyncio.to_thread(elixir_agent.run_reflection, context)
    except Exception as exc:
        log.error("reflection: agent call failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("reflection", f"agent call failed: {exc}")
        return
    if not isinstance(plan, dict) or plan.get("_error"):
        detail = (plan or {}).get("_error") if isinstance(plan, dict) else "no plan"
        log.warning("reflection: agent returned no usable plan: %s", detail)
        runtime_status.mark_job_failure("reflection", f"no usable plan: {detail}")
        return

    lessons = plan.get("lessons")
    lessons = lessons if isinstance(lessons, list) else []
    if len(lessons) > MAX_LESSONS_PER_NIGHT:
        log.info(
            "reflection: model returned %d lessons; keeping %d",
            len(lessons),
            MAX_LESSONS_PER_NIGHT,
        )
        lessons = lessons[:MAX_LESSONS_PER_NIGHT]

    dossiers = plan.get("dossiers")
    dossiers = dossiers if isinstance(dossiers, list) else []
    valid_evidence_refs = {
        str(item.get("ref"))
        for item in (context.get("evidence_index") or [])
        if isinstance(item, dict) and item.get("ref")
    }
    evidence_members = {
        str(item["evidence_ref"]): str(item["member_tag"])
        for item in (context.get("member_conversations") or [])
        if isinstance(item, dict) and item.get("evidence_ref") and item.get("member_tag")
    }

    def _write():
        conn = db.get_connection()
        try:
            written = _persist_lessons(
                conn,
                lessons,
                str(plan.get("notes") or ""),
                valid_evidence_refs=valid_evidence_refs,
            )
            return written, _persist_dossiers(
                conn,
                dossiers,
                evidence_members=evidence_members,
            )
        finally:
            conn.close()

    try:
        written, dossiers_written = await asyncio.to_thread(_write)
    except Exception as exc:
        log.error("reflection: persisting lessons failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("reflection", f"persist failed: {exc}")
        return

    log.info(
        "reflection: %d intent(s), %d reaction(s), %d silence(s) -> %d lesson(s), %d dossier(s)",
        len(context["intents"]),
        len(context["reactions"]),
        len(context["silences"]),
        len(written),
        dossiers_written,
    )
    runtime_status.mark_job_success(
        "reflection", f"{len(written)} lesson(s), {dossiers_written} dossier(s)"
    )
