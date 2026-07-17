"""Elixir's editorial-learning loop: feeders that turn human actions into
editorial rubric memories.

Human actions become composition guidance on the next awareness read:
``record_deleted_post`` (an admin deletes an Elixir post),
``record_copy_edit_pair`` (a leader rewrites action-card copy), immediate
repetition feedback from the awareness delivery path
(``record_active_awareness_quality``), and retrospective feedback from
``scripts/eval_post_quality.py`` (``record_post_quality_feedback``).

(A post-compose LLM critic once lived here too — wired into delivery 2026-07-16,
backed out the same day for holding the delivery write lock and false-grounding
on thin facts, and removed entirely 2026-07-17 along with its
``ELIXIR_EDITOR_GATE`` flag and the ``editor_verdicts`` ledger. The feeders were
always the durable, decision-safe half.)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from difflib import SequenceMatcher

log = logging.getLogger("elixir.engine.editor")


# ------------------------------------------------- editorial rubric memories


def _editorial_memory_exists(conn, event_key: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM memories WHERE source_event_key = ?", (event_key,)).fetchone()
        is not None
    )


def _add_editorial_memory(
    conn,
    *,
    title: str,
    body: str,
    kind_tag: str,
    event_key: str,
    confidence: float,
    created_by: str,
    extra_tags: tuple[str, ...] = (),
    source_type: str = "inference",
) -> int | None:
    """One rubric entry, deduped on its event key. Returns memory_id or None."""
    import memory_store

    if _editorial_memory_exists(conn, event_key):
        return None
    et, _, eid = event_key.partition(":")
    mem = memory_store.create_memory(
        body=body,
        source_type=source_type,
        is_inference=(source_type == "inference"),
        confidence=confidence,
        created_by=created_by,
        scope="leadership",
        title=title,
        event_type=et,
        event_id=eid or None,
        conn=conn,
    )
    memory_store.attach_tags(
        mem["memory_id"],
        ["editorial", kind_tag, *extra_tags],
        actor=created_by,
        conn=conn,
    )
    return mem["memory_id"]


def record_deleted_post(
    conn, discord_message_id: str, channel_name: str, content: str
) -> int | None:
    """Deletion feeder: an admin deleting an Elixir post in one of its lanes is
    the strongest anti-pattern signal we get."""
    post = conn.execute(
        "SELECT lane, covers_json, loop_number, posted_at FROM awareness_posts "
        "WHERE discord_message_id = ?",
        (str(discord_message_id),),
    ).fetchone()
    facts_note = ""
    if post:
        facts_note = (
            f"\n\nAwareness loop: L{post['loop_number']}\n"
            f"Lane: {post['lane']}\n"
            f"Evidence covered: {(post['covers_json'] or '[]')[:500]}"
        )
    mid = _add_editorial_memory(
        conn,
        title="Anti-pattern: post deleted by an admin",
        body=(
            f"An admin deleted this Elixir post in #{channel_name}:\n\n"
            f"{(content or '(content unavailable)')[:600]}{facts_note}"
        ),
        kind_tag="anti-pattern",
        event_key=f"editorial_deletion:{discord_message_id}",
        confidence=0.75,
        created_by="editor-deletion",
    )
    conn.commit()
    return mid


def record_copy_edit_pair(conn, action_id: int, before: str, after: str) -> int | None:
    """Copy-edit feeder: a leader rewriting action-card copy is a paired
    before/after exemplar — the after is what good looks like."""
    before, after = (before or "").strip(), (after or "").strip()
    if not before or not after or before == after:
        return None
    mid = _add_editorial_memory(
        conn,
        title="Exemplar pair: leader copy-edit on an action card",
        body=(
            f"A leader rewrote Elixir's action-card copy — the AFTER is the "
            f"standard to emulate.\n\nBEFORE (Elixir): {before[:400]}\n\n"
            f"AFTER (leader): {after[:400]}"
        ),
        kind_tag="exemplar",
        event_key=f"editorial_copy_edit:{action_id}",
        confidence=0.8,
        created_by="editor-copy-edit",
    )
    conn.commit()
    return mid


def _normalized_copy(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _similar_copy(conn, lane: str, content: str, message_id: str | None) -> dict | None:
    current = _normalized_copy(content)
    if len(current) < 40:
        return None
    rows = conn.execute(
        "SELECT discord_message_id, content_preview, posted_at FROM awareness_posts "
        "WHERE lane = ? AND (? IS NULL OR discord_message_id != ?) "
        "ORDER BY posted_at DESC LIMIT 12",
        (lane, message_id, message_id),
    ).fetchall()
    best = None
    for row in rows:
        prior = _normalized_copy(row["content_preview"])
        if len(prior) < 40:
            continue
        ratio = SequenceMatcher(None, current, prior).ratio()
        if ratio >= 0.82 and (best is None or ratio > best["similarity"]):
            best = {
                "message_id": row["discord_message_id"],
                "copy": row["content_preview"],
                "posted_at": row["posted_at"],
                "similarity": round(ratio, 3),
            }
    return best


def _recent_solo_spotlight(conn, post: dict) -> dict | None:
    tags = [str(tag) for tag in (post.get("member_tags") or []) if tag]
    if len(tags) != 1 or post.get("leads_with") not in {"milestone", "clan_event"}:
        return None
    target = tags[0]
    rows = conn.execute(
        "SELECT at, plan_json FROM awareness_thoughts "
        "WHERE chose_silence = 0 AND at >= datetime('now', '-48 hour') "
        "ORDER BY at DESC LIMIT 50"
    ).fetchall()
    for row in rows:
        try:
            plan = json.loads(row["plan_json"] or "{}")
        except TypeError, ValueError:
            continue
        for prior in plan.get("posts") or []:
            if not isinstance(prior, dict):
                continue
            prior_tags = [str(tag) for tag in (prior.get("member_tags") or []) if tag]
            if prior.get("leads_with") in {
                "milestone",
                "clan_event",
            } and prior_tags == [target]:
                return {
                    "at": row["at"],
                    "summary": prior.get("summary"),
                    "member_tag": target,
                }
    return None


def record_active_awareness_quality(
    conn,
    *,
    post: dict,
    evidence: dict,
    lane: str,
    content: str,
    discord_message_id: str | int | None,
) -> list[int]:
    """Immediate deterministic feedback from the production awareness path.

    This does not grade style with another LLM.  It catches two high-confidence
    editorial failures as soon as a post lands: near-duplicate copy and a
    routine solo spotlight inside the existing 48-hour cooldown.  The resulting
    editorial memory is available on the very next awareness read.
    """
    message_id = str(discord_message_id) if discord_message_id is not None else None
    similar = _similar_copy(conn, lane, content, message_id)
    prior_spotlight = None
    if not evidence.get("notable_moment"):
        prior_spotlight = _recent_solo_spotlight(conn, post or {})
    if not similar and not prior_spotlight:
        return []

    reasons = []
    if similar:
        reasons.append(
            f"Its wording is {similar['similarity']:.0%} similar to the {similar['posted_at']} post: "
            f"{similar['copy'][:300]}"
        )
    if prior_spotlight:
        reasons.append(
            f"It solo-spotlights {prior_spotlight['member_tag']} again inside 48 hours; "
            f"the earlier beat was {prior_spotlight.get('summary') or '(no summary)'} at "
            f"{prior_spotlight['at']}."
        )
    stable_id = message_id or hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
    mid = _add_editorial_memory(
        conn,
        title="Anti-pattern: active awareness repetition",
        body=(
            f"A live #{lane} awareness post repeated a recent beat. "
            "Future composition should change the subject or angle, combine it into a roundup, "
            "or stay silent unless the new moment is explicitly notable.\n\n"
            + "\n".join(f"- {reason}" for reason in reasons)
            + f"\n\nPOST: {content[:600]}"
        ),
        kind_tag="anti-pattern",
        event_key=f"active_post_quality:{stable_id}",
        confidence=0.85,
        created_by="editor-active-quality",
        extra_tags=("quality-eval", f"lane:{lane}"),
    )
    return [mid] if mid is not None else []


def record_post_quality_feedback(
    conn,
    *,
    message_id: str | None,
    lane: str,
    source: str,
    copy: str,
    findings: list[dict] | None = None,
    depth: int | None = None,
    depth_reason: str | None = None,
    repetition: dict | None = None,
) -> int | None:
    """Persist one idempotent editorial lesson from the retrospective eval."""
    concerns = []
    for finding in findings or []:
        concerns.append(f"Game accuracy: {finding.get('issue')}")
    if depth is not None and depth <= 2:
        concerns.append(f"Depth {depth}/5: {depth_reason or 'generic or thin'}")
    if repetition:
        concerns.append(
            f"Repetition: {repetition.get('similarity', 0):.0%} similar to "
            f"message {repetition.get('message_id') or '?'}"
        )
    if not concerns:
        return None
    stable_id = (
        message_id or hashlib.sha1(f"{lane}|{source}|{copy}".encode("utf-8")).hexdigest()[:16]
    )
    return _add_editorial_memory(
        conn,
        title="Anti-pattern: live output quality finding",
        body=(
            f"The active {source} path produced a #{lane} message that should shape future "
            "composition.\n\n"
            + "\n".join(f"- {concern}" for concern in concerns)
            + f"\n\nPOST: {copy[:700]}"
        ),
        kind_tag="anti-pattern",
        event_key=f"post_quality:{stable_id}",
        confidence=0.75,
        created_by="editor-post-quality",
        extra_tags=("quality-eval", f"lane:{lane}", f"source:{source}"),
    )
