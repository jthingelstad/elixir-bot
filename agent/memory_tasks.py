"""LLM-based memory post-processing: summary distillation and inference extraction."""

from __future__ import annotations

import json
import logging

from agent.core import _create_chat_completion

log = logging.getLogger("elixir_agent.memory_tasks")

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ── Summary distillation ───────────────────────────────────────────────────

_DISTILL_SYSTEM = (
    "Summarize the following message in 1-2 concise sentences. "
    "Capture the key intent or information. Output only the summary, nothing else."
)


def distill_summary(text: str) -> str | None:
    """Generate a 1-2 sentence summary of the given text via a lightweight LLM call."""
    text = (text or "").strip()
    if not text:
        return None
    # Very short messages are already their own summary
    if len(text) <= 120:
        return text

    try:
        resp = _create_chat_completion(
            workflow="memory_distill",
            model=HAIKU_MODEL,
            messages=[
                {"role": "system", "content": _DISTILL_SYSTEM},
                {"role": "user", "content": text[:2000]},
            ],
            temperature=0.3,
            max_tokens=100,
            timeout=15,
        )
        content = resp.choices[0].message.content
        if content and content.strip():
            return content.strip()
        return None
    except Exception:
        log.debug("distill_summary failed", exc_info=True)
        return None


# ── Inference fact extraction ──────────────────────────────────────────────

_INFERENCE_SYSTEM = """\
You are an analyst extracting durable facts from Clash Royale clan Discord messages.

Extract facts worth remembering long-term. Good examples:
- Member preferences ("king_thing prefers concise war summaries")
- Clan milestones ("reached 44 members in April 2026")
- Member roles/notes ("raquaza is primary war leader and founder")
- Leadership decisions ("Free Pass Royale awarded to top war contributor each season")
- Notable achievements ("Alpha hit 8000 trophies for the first time")
- Behavioral patterns ("Bravo consistently participates in every war race")

Do NOT extract:
- Routine greetings or small talk
- Temporary status that changes daily (current trophies, today's deck)
- Information that is just repeating game data without context
- Facts that are only relevant for the current conversation

Return a JSON array. Each element:
{"title": "short label", "body": "full fact text", "confidence": 0.5-0.95, "scope": "leadership"|"public", "tags": ["tag1"], "member_tag": "#TAG or null"}

confidence guide: 0.9+ for explicit statements, 0.7-0.9 for strong implications, 0.5-0.7 for weak inferences.
scope: use "leadership" for ops/decisions/personnel, "public" for achievements/milestones visible to all.

If nothing is worth extracting, return an empty array: []
Respond with ONLY the JSON array, no other text."""


def extract_inference_facts(content: str, context_label: str | None = None) -> list[dict]:
    """Extract durable facts from conversation or signal content."""
    content = (content or "").strip()
    if not content:
        return []

    user_msg = content[:3000]
    if context_label:
        user_msg = f"[Context: {context_label}]\n\n{user_msg}"

    try:
        resp = _create_chat_completion(
            workflow="memory_inference",
            model=HAIKU_MODEL,
            messages=[
                {"role": "system", "content": _INFERENCE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=500,
            timeout=20,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return []
        facts = json.loads(raw)
        if not isinstance(facts, list):
            return []
        valid = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if not fact.get("title") or not fact.get("body"):
                continue
            conf = float(fact.get("confidence", 0.7))
            conf = max(0.5, min(0.95, conf))
            valid.append({
                "title": str(fact["title"]).strip(),
                "body": str(fact["body"]).strip(),
                "confidence": conf,
                "scope": fact.get("scope", "leadership") if fact.get("scope") in ("leadership", "public") else "leadership",
                "tags": [str(t).strip().lower() for t in (fact.get("tags") or []) if t],
                "member_tag": str(fact["member_tag"]).strip() if fact.get("member_tag") else None,
            })
        return valid
    except (json.JSONDecodeError, ValueError, TypeError):
        log.debug("extract_inference_facts JSON parse failed", exc_info=True)
        return []
    except Exception:
        log.debug("extract_inference_facts failed", exc_info=True)
        return []


# ── Inference fact persistence ─────────────────────────────────────────────


def save_inference_facts(facts: list[dict], channel_id: str | int | None = None, conn=None) -> int:
    """De-duplicate and persist extracted inference facts. Returns count saved."""
    from memory_store import attach_tags, create_memory, search_memories

    saved = 0
    for fact in (facts or []):
        try:
            existing = search_memories(
                fact["title"],
                viewer_scope="system_internal",
                include_system_internal=True,
                filters={"source_type": "elixir_inference"},
                limit=3,
                conn=conn,
            )
            duplicate = False
            body_lower = fact["body"].lower()
            for result in existing:
                existing_body = (result.memory.get("body") or "").lower()
                # Simple overlap: skip if the existing body contains most of the new fact
                if body_lower in existing_body or existing_body in body_lower:
                    duplicate = True
                    break
                # Check title overlap too
                existing_title = (result.memory.get("title") or "").lower()
                if fact["title"].lower() == existing_title:
                    duplicate = True
                    break
            if duplicate:
                continue

            memory = create_memory(
                title=fact["title"],
                body=fact["body"],
                summary=fact["body"][:220],
                source_type="elixir_inference",
                is_inference=True,
                confidence=fact["confidence"],
                created_by="elixir:inference",
                scope=fact["scope"],
                channel_id=str(channel_id) if channel_id else None,
                member_tag=fact.get("member_tag"),
                conn=conn,
            )
            if memory and fact.get("tags"):
                attach_tags(memory["memory_id"], fact["tags"], actor="elixir:inference", conn=conn)
            saved += 1
        except Exception:
            log.warning("save_inference_facts: failed to save fact %r", fact.get("title"), exc_info=True)
    return saved


__all__ = [
    "distill_summary",
    "extract_inference_facts",
    "save_inference_facts",
]
